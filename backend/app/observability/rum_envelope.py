"""LibreRun RUM v1 — the browser telemetry envelope.

The browser does NOT speak OTLP. It POSTs this small, closed, versioned
schema to the authenticated same-origin relay (``/api/v1/_o/e``), and the
server constructs OpenTelemetry signals from it (``web_telemetry.py``).
Two properties are load-bearing and must survive every edit:

* **Closed:** ``extra="forbid"`` everywhere, every string is pattern- and
  length-bounded, every number is range-bounded, every category is an
  enum. Arbitrary user text — pasted logs, error messages, URLs, query
  strings — is *unrepresentable*, not merely scrubbed. A key allowlist
  alone would still leak through allowed keys' values.
* **Untrusted:** a valid session JWT authenticates the *principal*, not
  the payload. Everything here is a claim; the relay decides identity
  (tenant, user pseudonym) server-side and authorizes surface claims
  (``agent_id``/``run_id``) against the registry and the tenant's own
  runs before anything is emitted.

Records that fail validation are dropped individually (the batch
survives); only structural violations of the envelope itself reject the
request. Bump ``SCHEMA_VERSION`` on any wire change — old clients keep
working only against the version they were built for.
"""

from __future__ import annotations

from typing import Annotated, Literal, Union
from uuid import UUID

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    model_validator,
)

SCHEMA_VERSION = 1

# Bounds shared by the relay. MAX_BODY_BYTES bounds the raw request read
# (before JSON parsing); MAX_RECORDS bounds validation work per request.
# Both are deliberately far below generic collector defaults — a browser
# batch that honors the shared 64 KiB fetch-keepalive budget never comes
# near either.
MAX_BODY_BYTES = 128 * 1024
MAX_RECORDS = 50
# Event time must sit inside [now - PAST, now + FUTURE]; anything else is
# clock poisoning (or a queue replayed far too late) and the record drops.
MAX_PAST_SKEW_MS = 60 * 60 * 1000
MAX_FUTURE_SKEW_MS = 2 * 60 * 1000

# Application route identity — a CLOSED SET, enforced here, not just a
# charset: the frontend's resolver only ever emits these templates, but
# the relay is the trust boundary and an authenticated caller can bypass
# the frontend entirely. Shape-only validation would let any
# lowercase-dash string ride `librerun.route.template` as free text and
# unbounded cardinality. Keep this set in lockstep with
# `frontend/src/lib/telemetry/schema.ts` ROUTE_PATTERNS — a frontend
# route added without its template here drops to the `schema` reason in
# the relay's response counters, which is the visible drift signal.
ROUTE_TEMPLATES = frozenset(
    {
        "root",
        "login",
        "dashboard",
        "run.new",
        "run.detail",
        "admin.home",
        "admin.agent_config",
        "admin.audit_log",
        "admin.auth_config",
        "admin.run_detail",
        "admin.feedback",
        "admin.settings",
        "admin.users",
        "other",
    }
)


def _known_route(value: str) -> str:
    if value not in ROUTE_TEMPLATES:
        raise ValueError("not a known route template")
    return value


RouteTemplate = Annotated[str, AfterValidator(_known_route)]
# Agent ids follow the registry's manifest id shape.
_AGENT_ID = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,49}$")
# web-vitals metric instance ids — the EXACT generated shape, not a
# charset: the library's generateUniqueID() emits
# `v{major}-{Date.now()}-{13-digit random}` and nothing else, and the
# translator exports this value verbatim as `browser.web_vital.id`. A
# bare identifier charset would let an authenticated caller who skips
# the frontend ride free text ("customer-acme-production-secret")
# through the no-free-text boundary; digits cannot spell anything.
_METRIC_ID = Field(pattern=r"^v[0-9]{1,2}-[0-9]{13}-[0-9]{13}$")
# JS error class names (Error, TypeError, app classes). No spaces — a
# free-text message can never masquerade as a type.
_ERROR_TYPE = Field(pattern=r"^[A-Za-z0-9_.$]{1,64}$")
# Client-computed grouping hash, hex only.
_FINGERPRINT = Field(pattern=r"^[a-f0-9]{8,64}$")
# Static bundle path — MUST begin with the bundle prefix, mirroring the
# frontend's same-origin frame extraction: without the prefix this field
# would be a free-text channel for any authenticated caller who skips
# the frontend. The charset has no "?" or "#", so query strings and
# fragments are unrepresentable.
_BUNDLE_MODULE_PATTERN = r"^/_next/static/[A-Za-z0-9_@\[\]./-]{1,110}$"
_APP_VERSION = Field(pattern=r"^[A-Za-z0-9._-]{1,32}$")

# navigation_type values as the web-vitals library reports them, plus
# "soft-navigation" (Chrome 151+ soft-nav reporting) for forward compat.
NavigationType = Literal[
    "navigate",
    "reload",
    "back-forward",
    "back-forward-cache",
    "prerender",
    "restore",
    "soft-navigation",
]


class _RecordBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    occurred_at_ms: int = Field(ge=1, lt=2**53)
    page_id: UUID
    route: RouteTemplate
    ui_owner: Literal["platform", "agent"]
    agent_id: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9_-]{0,49}$")
    run_id: UUID | None = None

    @model_validator(mode="after")
    def _surface_shape(self) -> "_RecordBase":
        """Platform records carry no agent claims; agent records must name
        the agent. Existence/ownership of the claims is the relay's job —
        this only rejects shapes that could never be authorized."""
        if self.ui_owner == "platform" and (self.agent_id or self.run_id):
            raise ValueError("platform records must not carry agent_id/run_id")
        if self.ui_owner == "agent" and not self.agent_id:
            raise ValueError("agent records must carry agent_id")
        return self


class WebVitalRecord(_RecordBase):
    type: Literal["web_vital"]
    name: Literal["lcp", "cls", "inp", "ttfb", "fcp"]
    # CLS is a small unitless score; the rest are milliseconds. One
    # generous finite bound covers both without admitting nonsense.
    value: float = Field(ge=0, le=1e7, allow_inf_nan=False)
    rating: Literal["good", "needs-improvement", "poor"]
    metric_id: str = _METRIC_ID
    navigation_type: NavigationType | None = None


class JsExceptionRecord(_RecordBase):
    type: Literal["js_exception"]
    error_type: str = _ERROR_TYPE
    mechanism: Literal["window.error", "unhandledrejection"]
    fingerprint: str = _FINGERPRINT
    # First own-bundle stack frame's static asset path, if same-origin.
    # Never a message, never a full stack, never a URL with query/hash —
    # and never anything outside /_next/static/ (see _BUNDLE_MODULE_PATTERN).
    bundle_module: str | None = Field(default=None, pattern=_BUNDLE_MODULE_PATTERN)


class RouteChangeRecord(_RecordBase):
    type: Literal["route_change"]
    from_route: RouteTemplate
    trigger: Literal["link", "push", "replace", "traverse", "initial", "unknown"]
    # Present only when the client observed the navigation *intent* (link
    # click, back/forward) — then the translator makes a real span.
    duration_ms: int | None = Field(default=None, ge=0, le=120_000)
    navigation_id: UUID | None = None


class PageViewRecord(_RecordBase):
    type: Literal["page_view"]
    navigation_kind: Literal["hard", "bfcache_restore"]
    referrer_route: RouteTemplate | None = None


RumRecord = Annotated[
    Union[WebVitalRecord, JsExceptionRecord, RouteChangeRecord, PageViewRecord],
    Field(discriminator="type"),
]

# Records are validated one by one so a single bad record drops alone
# instead of rejecting the batch; the envelope stays structural.
record_adapter: TypeAdapter[RumRecord] = TypeAdapter(RumRecord)


class RumEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    session_id: UUID
    app_version: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._-]{1,32}$")
    # Raw dicts on purpose — see record_adapter above.
    records: list[dict] = Field(min_length=1, max_length=MAX_RECORDS)


__all__ = [
    "ROUTE_TEMPLATES",
    "SCHEMA_VERSION",
    "MAX_BODY_BYTES",
    "MAX_RECORDS",
    "MAX_PAST_SKEW_MS",
    "MAX_FUTURE_SKEW_MS",
    "NavigationType",
    "WebVitalRecord",
    "JsExceptionRecord",
    "RouteChangeRecord",
    "PageViewRecord",
    "RumRecord",
    "record_adapter",
    "RumEnvelope",
]
