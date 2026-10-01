"""Generic intake helpers (blueprint B8).

``POST /runs`` stopped being demo-agent-shaped in B8: the body is whatever the
selected agent's ``input_schema()`` declares. This module holds the three
chassis-side pieces of that contract:

- ``validate_user_inputs`` — JSON Schema validation of the submitted body
  (the server-side twin of the wizard's client-side checks).
- ``redact_pii_fields`` — PII redaction before persist for every string
  field the schema marks ``x-pii``. The pre-B8 flow only redacted file
  uploads via the preview endpoint; pasted text went in raw. Driving
  redaction from the schema closes that hole for every agent at once.
- ``run_title`` — the run's title (blueprint S2): the manifest's
  ``ui.list.title_path`` resolved against the payload, or the first
  string input not marked ``x-pii``. Stored in the ``title`` column and
  what the dashboard lists and searches, for every agent.
- ``approval_summary`` — the string the approval view shows for a parked
  phase output: the manifest's ``ui.approval.summary_path`` resolved
  against it, or the first non-empty string in it.
- ``extract_legacy_columns`` — lifts well-known keys into the legacy
  demo-agent-shaped ``runs`` columns (vendor names, use case, …) so the
  old detail surfaces keep working until those columns go.
"""
from __future__ import annotations

from typing import Any

import structlog
from jsonschema import Draft202012Validator

from app.services import pii_service
from app.services.pii_service import redact

logger = structlog.get_logger(__name__)

# Severity values the runs table CHECK constraint accepts. A schema that
# declares its own severity vocabulary still persists fine — the value
# just stays in ``user_inputs`` instead of the legacy column.
_LEGACY_SEVERITIES = frozenset({"critical", "high", "medium", "low"})

# ``problem_statement`` is no longer lifted here: since blueprint S2 that
# column holds the run's title, computed by ``run_title`` for every agent.
_LEGACY_STRING_KEYS = (
    "logs_a",
    "logs_b",
    "use_case",
    "impact_statement",
)

# Depth-first search for a default summary stops here: a parked output
# is a small structured object, not a document.
_FIRST_STRING_MAX_DEPTH = 6


def _is_string_property(prop: dict) -> bool:
    """True when the property admits strings: ``type`` absent, ``"string"``,
    or a JSON Schema type union that lists it (``["string", "null"]`` is
    the usual nullable-string spelling; Codex P2 on PR #50)."""
    declared = prop.get("type", "string")
    if isinstance(declared, list):
        return "string" in declared
    return declared == "string"


def resolve_path(obj: Any, dotted: str) -> Any:
    """Walk ``dotted`` ("a.b.c") through nested dicts; ``None`` when any
    segment is missing or the walk hits a non-dict."""
    cur = obj
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def first_string_in(obj: Any, *, depth: int = _FIRST_STRING_MAX_DEPTH) -> str | None:
    """The first non-empty string found depth-first, in insertion order.
    Dict values and list items are searched; keys are not."""
    if isinstance(obj, str):
        return obj if obj.strip() else None
    if depth <= 0:
        return None
    if isinstance(obj, dict):
        items = obj.values()
    elif isinstance(obj, list):
        items = obj
    else:
        return None
    for item in items:
        found = first_string_in(item, depth=depth - 1)
        if found is not None:
            return found
    return None


def run_title(title_path: str | None, schema: dict | None, payload: dict) -> str | None:
    """The run's title (blueprint S2), or ``None`` when nothing qualifies.

    ``title_path`` (the manifest's ``ui.list.title_path``) wins when it
    resolves to a non-empty string; when it does not (an optional field
    left blank, a path the payload never carried) the default applies
    rather than leaving the run untitled. The default is the first
    top-level string property of ``schema`` in declaration order whose
    value is a non-empty string — skipping properties marked ``x-pii``,
    because a redacted log is not a title.

    The value is returned exactly as submitted, never normalised: the
    column it fills is the one the bundled agent's own steps read as
    their problem statement, so line breaks and spacing must be the
    user's. The list form (``RunSummary.title``, via ``summary_text``)
    is the only trimmed, single-line view.
    """
    candidates: list[Any] = []
    if title_path:
        candidates.append(resolve_path(payload, title_path))
    for key, prop in ((schema or {}).get("properties") or {}).items():
        if not isinstance(prop, dict) or prop.get("x-pii"):
            continue
        if _is_string_property(prop):
            candidates.append(payload.get(key))
    for value in candidates:
        if isinstance(value, str) and value.strip():
            return value
    return None


def approval_summary(summary_path: str | None, payload: Any) -> str | None:
    """The string the approval view shows for a parked phase output."""
    if summary_path:
        value = resolve_path(payload, summary_path)
        if isinstance(value, str) and value.strip():
            return value
    return first_string_in(payload)


def validate_user_inputs(schema: dict, payload: Any) -> list[str]:
    """Validate ``payload`` against the agent's input JSON Schema.

    Returns human-readable error strings (empty list = valid), each
    prefixed with a JSON-pointer-ish path so the caller can surface
    field-level feedback: ``"vendor_a.name: 'name' is a required
    property"``.
    """
    if not isinstance(payload, dict):
        return ["body: must be a JSON object"]
    validator = Draft202012Validator(schema)
    errors: list[str] = []
    for err in sorted(validator.iter_errors(payload), key=lambda e: list(e.absolute_path)):
        path = ".".join(str(p) for p in err.absolute_path) or "body"
        errors.append(f"{path}: {err.message}")
    return errors


def _iter_pii_paths(schema: dict, prefix: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
    """Paths of every string property marked ``x-pii`` (top level and one
    object level down — mirroring how deep the form renders)."""
    paths: list[tuple[str, ...]] = []
    for key, prop in (schema.get("properties") or {}).items():
        if not isinstance(prop, dict):
            continue
        if prop.get("x-pii") and _is_string_property(prop):
            paths.append(prefix + (key,))
        if prop.get("type") == "object":
            paths.extend(_iter_pii_paths(prop, prefix + (key,)))
    return paths


def redact_pii_fields(schema: dict, payload: dict) -> tuple[dict, int]:
    """Return a copy of ``payload`` with every ``x-pii`` string field
    redacted, plus the total number of redactions applied.

    Raises :class:`~app.services.pii_service.PiiDetectorUnavailable`
    when the detector is not ready — ``POST /runs`` then answers 503 and
    no run is created (blueprint S4c).

    The gate is UNCONDITIONAL, before the first field is looked at, and
    that is the difference between "this body is safe" and "the platform
    is safe". A payload whose ``x-pii`` fields happen to be empty — or
    a schema that marks none at all — would otherwise sail past a
    broken detector and create a run, and the next thing that run does
    is hand agent output back across the boundary for persisting. The
    promise is about the run, not about this one body.
    """
    pii_service.require_ready(stage="intake")
    out = dict(payload)
    total = 0
    for path in _iter_pii_paths(schema):
        # Walk to the parent container, copying dicts on the way so the
        # caller's payload isn't mutated.
        parent = out
        for part in path[:-1]:
            child = parent.get(part)
            if not isinstance(child, dict):
                parent = None
                break
            child = dict(child)
            parent[part] = child
            parent = child
        if parent is None:
            continue
        leaf = path[-1]
        value = parent.get(leaf)
        if not isinstance(value, str) or not value:
            continue
        redacted, applied = redact(value)
        if applied:
            parent[leaf] = redacted
            total += len(applied)
    if total:
        logger.info("intake_pii_redacted", redactions=total)
    return out, total


def extract_legacy_columns(payload: dict) -> dict:
    """Well-known payload keys → legacy ``runs`` column kwargs.

    Purely best-effort: keys that are absent or the wrong shape are simply
    not lifted (the columns went nullable in migration 010), and the full
    payload always lands in ``user_inputs`` regardless.
    """
    out: dict[str, Any] = {}
    for side in ("a", "b"):
        vendor = payload.get(f"vendor_{side}")
        if isinstance(vendor, dict):
            for attr in ("name", "product", "feature", "observation"):
                value = vendor.get(attr)
                if isinstance(value, str) and value:
                    out[f"vendor_{side}_{attr}"] = value
    for key in _LEGACY_STRING_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value:
            out[key] = value
    severity = payload.get("severity")
    if isinstance(severity, str) and severity in _LEGACY_SEVERITIES:
        out["severity"] = severity
    return out
