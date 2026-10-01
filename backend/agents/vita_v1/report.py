"""VITA-side HTML renderer (embedded fragment + full PDF document).

Owns the Jinja env that points at ``agents/vita_v1/report.html``, the
citation linkifier, and the context builder that turns the agent's
``structured`` dict into the template context. Blueprint B9 moved the
linkifier in here from the shell ``report_service`` — citations are VITA
vocabulary, so the chassis no longer knows they exist. The shell reaches
this module only through ``VitaAgent.render_report_document``.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup

_TEMPLATE_DIR = Path(__file__).resolve().parent

_env = Environment(
    loader=FileSystemLoader(_TEMPLATE_DIR),
    autoescape=select_autoescape(["html", "xml"]),
)

_CITATION_RE = re.compile(r"\[(\d+)\]")


def linkify_citations(
    text: str,
    works_cited_a: list[dict] | None,
    works_cited_b: list[dict] | None,
) -> str:
    """Turn [N] into anchors pointing to the source URL, opening in a new tab.

    Only emits a link when N resolves to a citation in works_cited_a or
    works_cited_b. Unknown IDs remain as plain ``[N]`` text — Step 8's
    normalizer already strips phantom IDs pre-persist, so this is
    defensive for stale rows produced before the normalizer landed.
    """
    if not text:
        return ""

    by_id: dict[int, dict] = {}
    for c in (works_cited_a or []):
        cid = c.get("id")
        if isinstance(cid, int):
            by_id[cid] = c
    for c in (works_cited_b or []):
        cid = c.get("id")
        if isinstance(cid, int):
            by_id[cid] = c

    def _repl(m: "re.Match[str]") -> str:
        cid = int(m.group(1))
        c = by_id.get(cid)
        if not c or not c.get("url"):
            return m.group(0)
        url = str(c["url"]).replace('"', "&quot;")
        return (
            f'<a class="vita-citation-ref" href="{url}" '
            f'target="_blank" rel="noopener noreferrer">[{cid}]</a>'
        )

    return _CITATION_RE.sub(_repl, text)


def build_context_from_structured(case, structured: dict) -> dict:
    """Assemble the Jinja context for ``report.html`` from an agent result.

    ``structured`` is the dict returned by ``VitaAgent.investigate`` —
    specifically the legacy-shaped subset (``works_cited_a``,
    ``works_cited_b``, ``skills_cited``, ``resolution_plan``,
    ``followup_questions``, ``refined_problem``). Drift metadata
    (``_drifts``) is ignored here.
    """
    works_cited_a = structured.get("works_cited_a") or []
    works_cited_b = structured.get("works_cited_b") or []

    plan = dict(structured.get("resolution_plan") or {})
    for key in ("mitigation", "resolution", "avoidance"):
        section = plan.get(key)
        if section and isinstance(section, dict) and section.get("text"):
            linkified = linkify_citations(section["text"], works_cited_a, works_cited_b)
            plan[key] = {**section, "text": Markup(linkified)}

    return {
        "case": case,
        "refined": structured.get("refined_problem"),
        "works_cited_a": works_cited_a,
        "works_cited_b": works_cited_b,
        "skills": structured.get("skills_cited") or [],
        "plan": plan,
        "followups": structured.get("followup_questions") or [],
        "logs_a": getattr(case, "logs_a", None) or "",
        "logs_b": getattr(case, "logs_b", None) or "",
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def render_embedded(context: dict) -> str:
    """Inner-fragment HTML the frontend injects on the case page."""
    return _env.get_template("report.html").render(**context, embedded=True)


def render_full(context: dict) -> str:
    """Standalone HTML document used for PDF generation."""
    return _env.get_template("report.html").render(**context, embedded=False)
