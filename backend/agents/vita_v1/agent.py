"""VITA agent — Vendor Interoperability Troubleshooter.

Implements ``AgentProtocol`` over its manifest (``agent.yaml``) and the
pipeline step modules in this package. Discovered at startup by
``app.agents.registry.discover_agents``.

Phase 4: ``analyze`` and ``investigate`` now drive the VITA step modules
via the shell's ``PipelineOrchestrator`` (progress + timing + step spans).
All snapshot writes happen in the runner — the agent only returns pure
results and persists drift audit rows in its own session.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import NamedTuple

import structlog
from openinference.semconv.trace import OpenInferenceSpanKindValues

from app.agents.protocol import (
    AgentInput,
    AgentProtocol,
    AnalysisResult,
    InvestigationResult,
)
from app.logging_context import log_context
from app.logging_pii import user_content

# Per-step OpenInference span kind. CHAIN is the default for steps that
# wrap an LLM call plus normalisation; the two search steps are pure
# retrievers and get the RETRIEVER badge so RAG-aware trace views surface
# their results. Adding a new step means adding an entry here so it
# doesn't fall back to the orchestrator's CHAIN default by accident.
_CHAIN = OpenInferenceSpanKindValues.CHAIN.value
_RETRIEVER = OpenInferenceSpanKindValues.RETRIEVER.value
_STEP_KINDS: dict[str, str] = {
    "validate_and_classify_inputs": _CHAIN,
    "refine_problem_statement": _CHAIN,
    "construct_search_queries_vendor_a": _CHAIN,
    "construct_search_queries_vendor_b": _CHAIN,
    "search_internal_kb": _RETRIEVER,
    "search_public_resources": _RETRIEVER,
    "assess_skills": _CHAIN,
    "generate_resolution_plan": _CHAIN,
    "generate_followup_questions": _CHAIN,
}

logger = structlog.get_logger(__name__)

_REPORT_TEMPLATE = Path(__file__).resolve().parent / "report.html"


# The three settings a run reads (K5b, D31), declared in agent.yaml's
# settings[] and valued per tenant. Each integer is clamped where it is
# read, because settings[] declares no bounds: a negative retry count
# would call no model at all, and top_k takes the range the MCP
# kb_search tool clamps to. The clamped value is the one a run uses and
# the one its step spans show.
_TOP_K_RANGE = (1, 50)
_MAX_RETRIES_RANGE = (0, 5)


class _RunSettings(NamedTuple):
    # A NamedTuple, not a dataclass: a dataclass resolves its annotations
    # through sys.modules, and test_bundled_agent_phases loads this file
    # from source without registering it there.
    search_depth: str
    top_k: int
    max_retries: int


def _clamp(value: int, bounds: tuple[int, int]) -> int:
    low, high = bounds
    return max(low, min(high, value))


async def _read_settings(caps) -> _RunSettings:
    """This tenant's values, read once when a phase starts.

    ``caps.config.settings()`` answers every key the manifest declares —
    this tenant's value, else the declared default — so a tenant that
    never chose one reads what the code hard-coded before K5b.
    """
    values = await caps.config.settings()
    return _RunSettings(
        search_depth=values["tavily_search_depth"],
        top_k=_clamp(values["pinecone_top_k"], _TOP_K_RANGE),
        max_retries=_clamp(values["max_retries_per_step"], _MAX_RETRIES_RANGE),
    )


def _legacy_overlay() -> Path:
    """Where the retired config store kept this agent's settings (D18).

    Resolved as the store resolved it — ``LIBRERUN_STATE_DIR``, else
    ``$XDG_STATE_HOME/librerun``, else ``~/.local/state/librerun`` — and
    from ``os.environ``, since this package imports no chassis
    configuration.
    """
    configured = os.environ.get("LIBRERUN_STATE_DIR")
    if configured:
        root = Path(configured).expanduser()
    else:
        xdg = os.environ.get("XDG_STATE_HOME")
        base = Path(xdg).expanduser() if xdg else Path("~/.local/state").expanduser()
        root = base / "librerun"
    return root / "agents" / "vita_v1" / "config.json"


def _safe_extract(value, key: str, default=None):
    """Extract ``key`` from an LLM response that may be a dict, bare list, or None."""
    if default is None:
        default = []
    if isinstance(value, dict):
        return value.get(key, default)
    if isinstance(value, list):
        return value
    return default


async def _persist_drifts(caps, run_id, reports) -> None:
    """Persist drift reports through the audit capability (blueprint
    B13) — its own session, so a write failure can't poison the
    pipeline's transaction."""
    if not reports:
        return
    logger.info(
        "schema_drift",
        run_id=str(run_id),
        reports=[r.to_dict() for r in reports],
    )
    await caps.audit.log_schema_drift(reports)


class VitaAgent(AgentProtocol):
    agent_id = "vita-v1"
    display_name = "VITA Vendor Troubleshooter"
    description = "Investigates interoperability issues between two vendor products"

    def __init__(self) -> None:
        # D18: an installation upgraded from before K5b may still hold the
        # retired overlay. Nothing reads it and its values are not carried
        # over, so say so once, at load, and say where the settings live
        # now. A diagnostic must never stop the agent loading.
        try:
            overlay = _legacy_overlay()
            found = overlay.is_file()
        except (OSError, ValueError):
            return
        if found:
            logger.warning(
                "vita_legacy_settings_overlay",
                path=str(overlay),
                hint="this file is no longer read and its values were not "
                "carried over: set the agent's settings for each tenant on "
                "the agent page's Settings tab (Admin → Agents)",
            )

    # --- input schema ------------------------------------------------------

    @staticmethod
    def _vendor_schema(side: str) -> dict:
        return {
            "type": "object",
            "title": f"Vendor {side}",
            "required": ["name"],
            "additionalProperties": False,
            "properties": {
                "name": {
                    "type": "string",
                    "title": "Name",
                    "minLength": 1,
                    "maxLength": 255,
                },
                "product": {
                    "type": "string",
                    "title": "Product",
                    "maxLength": 255,
                },
                "feature": {
                    "type": "string",
                    "title": "Feature",
                    "maxLength": 255,
                },
                "observation": {
                    "type": "string",
                    "title": "Observation",
                    "x-ui-widget": "textarea",
                },
            },
        }

    def input_schema(self) -> dict:
        """VITA's full intake schema (blueprint B8).

        The exact shape ``POST /cases`` validates and persists as
        ``user_inputs`` — the retired ``RunCreate`` body, expressed as
        JSON Schema. The manifest's ``ui.intake.steps`` group these fields
        into the same seven-step wizard the bespoke intake used to
        hard-code; ``x-pii`` marks the log fields for the redaction-preview
        upload affordance and server-side redaction before persist.
        """
        return {
            "type": "object",
            "required": ["vendor_a", "vendor_b", "use_case", "problem_statement"],
            "additionalProperties": False,
            "properties": {
                "vendor_a": self._vendor_schema("A"),
                "vendor_b": self._vendor_schema("B"),
                "logs_a": {
                    "type": "string",
                    "title": "Vendor A logs",
                    "description": "Paste log content or upload a file for a redaction preview.",
                    "x-ui-widget": "textarea",
                    "x-pii": True,
                    "x-upload-kind": "log",
                },
                "logs_b": {
                    "type": "string",
                    "title": "Vendor B logs",
                    "description": "Paste log content or upload a file for a redaction preview.",
                    "x-ui-widget": "textarea",
                    "x-pii": True,
                    "x-upload-kind": "log",
                },
                "use_case": {
                    "type": "string",
                    "title": "Use case",
                    "description": "What the integration is supposed to accomplish (min 50 chars).",
                    "minLength": 50,
                    "maxLength": 5000,
                    "x-ui-widget": "textarea",
                },
                "problem_statement": {
                    "type": "string",
                    "title": "Problem statement",
                    "description": "What is going wrong (min 20 chars).",
                    "minLength": 20,
                    "maxLength": 5000,
                    "x-ui-widget": "textarea",
                },
                "impact_statement": {
                    "type": "string",
                    "title": "Impact",
                    "description": "Business / user impact of the problem.",
                    "x-ui-widget": "textarea",
                },
                "severity": {
                    "type": "string",
                    "title": "Severity",
                    "enum": ["critical", "high", "medium", "low"],
                },
            },
        }

    # --- report template --------------------------------------------------

    def report_template_path(self) -> str | None:
        return str(_REPORT_TEMPLATE)

    def render_report_document(self, case, structured: dict) -> str | None:
        """Full standalone HTML document for download / PDF export.

        Runs VITA's own Jinja template (``report.html``) — the shell's
        report service calls this hook instead of importing agent modules
        (blueprint B9).
        """
        # Deferred import: keeps registry discovery independent of the
        # renderer's dependencies (jinja2/markupsafe).
        from .report import build_context_from_structured, render_full

        ctx = build_context_from_structured(case, structured)
        return render_full(ctx)

    # --- pipeline execution ------------------------------------------------

    async def analyze(self, inp: AgentInput, on_progress) -> AnalysisResult:  # noqa: ARG002
        # Imports deferred so registry discovery doesn't require every step
        # module to import cleanly — keeps agent loading resilient.
        from .llm_service import for_run as llm_for_run
        from .steps import step_0_validate, step_2_refine

        caps = inp.capabilities
        with log_context(run_id=str(inp.run_id), agent="vita-v1", phase="analyze"):
            try:
                settings = await _read_settings(caps)
                llm = llm_for_run(caps.llm, max_retries=settings.max_retries)
                async with caps.progress.pipeline(llm, _STEP_KINDS) as (case, orch):
                    if case is None:
                        return AnalysisResult(
                            display={"error": "run_not_found"},
                            structured={},
                            status="error",
                        )

                    await orch.reset_progress(inp.run_id)

                    drifts_all = []

                    validation, drifts_0 = await orch.run_step(
                        inp.run_id,
                        "validate_and_classify_inputs",
                        lambda: step_0_validate.run(llm, case),
                        input_payload={
                            "vendor_a_name": case.vendor_a_name,
                            "vendor_b_name": case.vendor_b_name,
                            "use_case": case.use_case,
                            # Free-form fields land in the LLM auto-instrumented
                            # spans; on the parent CHAIN span we record sizes
                            # so the trace is readable without re-exposing PII.
                            "problem_statement.chars": len(
                                case.problem_statement or ""
                            ),
                            "max_retries_per_step": settings.max_retries,
                        },
                    )
                    drifts_all.extend(drifts_0)
                    await _persist_drifts(caps, inp.run_id, drifts_0)

                    if not validation.get("valid", True):
                        # Attribution is the chassis's (blueprint S4): the
                        # capability stamps the run owner's id and email.
                        await caps.audit.log(
                            "blocked_request",
                            {
                                "run_id": str(inp.run_id),
                                "reason": validation.get("rejection_reason"),
                            },
                        )
                        return AnalysisResult(
                            display={
                                "rejected": True,
                                "reason": validation.get("rejection_reason"),
                            },
                            structured={
                                "classified_inputs": validation,
                                "_drifts": [r.to_dict() for r in drifts_all],
                            },
                            status="blocked",
                        )

                    refined, drifts_2 = await orch.run_step(
                        inp.run_id,
                        "refine_problem_statement",
                        lambda: step_2_refine.run(
                            llm, case, validation, customer_edit=inp.user_edits
                        ),
                        input_payload={
                            "validation_keys": sorted(list(validation.keys()))
                            if isinstance(validation, dict)
                            else None,
                            "customer_edit.chars": len(inp.user_edits or ""),
                            "max_retries_per_step": settings.max_retries,
                        },
                    )
                    drifts_all.extend(drifts_2)
                    await _persist_drifts(caps, inp.run_id, drifts_2)

                    return AnalysisResult(
                        display=refined,
                        structured={
                            "refined_problem": refined,
                            "classified_inputs": validation,
                            "_drifts": [r.to_dict() for r in drifts_all],
                        },
                        status="awaiting_approval",
                    )
            except Exception as e:
                logger.exception(
                    "vita_analyze_failed", error=user_content(str(e))
                )
                return AnalysisResult(
                    display={"error": user_content(str(e))},
                    structured={},
                    status="error",
                )

    async def investigate(self, inp: AgentInput, on_progress) -> InvestigationResult:  # noqa: ARG002
        from .llm_service import for_run as llm_for_run
        from .report import build_context_from_structured, render_embedded
        from .search_service import SearchService
        from .steps import (
            step_3_queries,
            step_5_internal_kb,
            step_6_public_search,
            step_7_skills,
            step_8_resolution,
            step_9_followups,
        )

        caps = inp.capabilities
        prior = inp.prior_analysis or {}
        refined = prior.get("refined_problem") or {}
        classified = prior.get("classified_inputs") or {}

        with log_context(run_id=str(inp.run_id), agent="vita-v1", phase="investigate"):
            try:
                settings = await _read_settings(caps)
                llm = llm_for_run(caps.llm, max_retries=settings.max_retries)
                async with caps.progress.pipeline(llm, _STEP_KINDS) as (case, orch):
                    if case is None:
                        return InvestigationResult(
                            status="error", error="run_not_found"
                        )

                    search_svc = SearchService(
                        caps,
                        search_depth=settings.search_depth,
                        top_k=settings.top_k,
                    )
                    drifts_all = []

                    (queries_a, drifts_a), (queries_b, drifts_b) = await asyncio.gather(
                        orch.run_step(
                            inp.run_id,
                            "construct_search_queries_vendor_a",
                            lambda: step_3_queries.run(
                                llm,
                                "a",
                                case,
                                refined,
                                "construct_search_queries_vendor_a",
                            ),
                            input_payload={
                                "vendor": "a",
                                "vendor_name": case.vendor_a_name,
                                "vendor_product": case.vendor_a_product,
                                "max_retries_per_step": settings.max_retries,
                            },
                        ),
                        orch.run_step(
                            inp.run_id,
                            "construct_search_queries_vendor_b",
                            lambda: step_3_queries.run(
                                llm,
                                "b",
                                case,
                                refined,
                                "construct_search_queries_vendor_b",
                            ),
                            input_payload={
                                "vendor": "b",
                                "vendor_name": case.vendor_b_name,
                                "vendor_product": case.vendor_b_product,
                                "max_retries_per_step": settings.max_retries,
                            },
                        ),
                    )
                    drifts_all.extend(drifts_a + drifts_b)
                    await _persist_drifts(caps, inp.run_id, drifts_a + drifts_b)

                    vector_queries = _safe_extract(
                        queries_a, "vector_queries"
                    ) + _safe_extract(queries_b, "vector_queries")

                    kb_results, web_results = await asyncio.gather(
                        orch.run_step(
                            inp.run_id,
                            "search_internal_kb",
                            lambda: step_5_internal_kb.run(
                                search_svc, vector_queries, inp.tenant_id
                            ),
                            # The value this run searched with, stamped
                            # even when the step is skipped (keyless).
                            input_payload={
                                "queries": vector_queries,
                                "pinecone_top_k": settings.top_k,
                            },
                        ),
                        orch.run_step(
                            inp.run_id,
                            "search_public_resources",
                            lambda: step_6_public_search.run(
                                search_svc, queries_a, queries_b
                            ),
                            input_payload={
                                "vendor_a_queries": _safe_extract(
                                    queries_a, "web_queries"
                                ),
                                "vendor_b_queries": _safe_extract(
                                    queries_b, "web_queries"
                                ),
                                "tavily_search_depth": settings.search_depth,
                            },
                        ),
                    )

                    skills, drifts_s = await orch.run_step(
                        inp.run_id,
                        "assess_skills",
                        lambda: step_7_skills.run(
                            llm, refined, web_results, kb_results
                        ),
                        input_payload={
                            "kb_result_count": len(
                                kb_results.get("results", [])
                                if isinstance(kb_results, dict)
                                else []
                            ),
                            "web_result_count": (
                                len(web_results.get("vendor_a_results", []))
                                + len(web_results.get("vendor_b_results", []))
                                if isinstance(web_results, dict)
                                else 0
                            ),
                            "max_retries_per_step": settings.max_retries,
                        },
                    )
                    drifts_all.extend(drifts_s)
                    await _persist_drifts(caps, inp.run_id, drifts_s)

                    plan, drifts_p = await orch.run_step(
                        inp.run_id,
                        "generate_resolution_plan",
                        lambda: step_8_resolution.run(
                            llm, refined, skills, web_results, kb_results
                        ),
                        input_payload={
                            "skills_count": len(
                                skills.get("skills", [])
                                if isinstance(skills, dict)
                                else []
                            ),
                            "max_retries_per_step": settings.max_retries,
                        },
                    )
                    drifts_all.extend(drifts_p)
                    await _persist_drifts(caps, inp.run_id, drifts_p)

                    resolution_plan = {
                        "mitigation": plan["mitigation"],
                        "resolution": plan["resolution"],
                        "avoidance": plan["avoidance"],
                    }

                    followups, drifts_f = await orch.run_step(
                        inp.run_id,
                        "generate_followup_questions",
                        lambda: step_9_followups.run(
                            llm, refined, resolution_plan, classified
                        ),
                        input_payload={
                            "resolution_plan_keys": sorted(
                                list(resolution_plan.keys())
                            ),
                            "max_retries_per_step": settings.max_retries,
                        },
                    )
                    drifts_all.extend(drifts_f)
                    await _persist_drifts(caps, inp.run_id, drifts_f)

                    structured = {
                        "refined_problem": refined,
                        "classified_inputs": classified,
                        "works_cited_a": plan["works_cited"]["vendor_a"],
                        "works_cited_b": plan["works_cited"]["vendor_b"],
                        "skills_cited": skills["skills"],
                        "resolution_plan": resolution_plan,
                        "followup_questions": followups["questions"],
                    }
                    report_html = render_embedded(
                        build_context_from_structured(case, structured)
                    )
                    structured["_drifts"] = [r.to_dict() for r in drifts_all]

                    return InvestigationResult(
                        status="complete",
                        report_html=report_html,
                        structured=structured,
                    )
            except Exception as e:
                logger.exception(
                    "vita_investigate_failed", error=user_content(str(e))
                )
                return InvestigationResult(
                    status="error", error=user_content(str(e))
                )
