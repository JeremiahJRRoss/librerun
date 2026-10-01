from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.manifest import AgentManifest
from app.agents.registry import get_agent, get_manifest
from app.models import Run, RunSnapshot
from app.schemas.run import VendorInput
from app.services import run_errors
from app.services.intake import approval_summary, first_string_in, run_title

logger = structlog.get_logger(__name__)


async def allocate_run_number(db: AsyncSession, tenant_id: UUID) -> str:
    result = await db.execute(
        text("SELECT allocate_run_number(:tid)"), {"tid": str(tenant_id)}
    )
    return result.scalar_one()


def summary_text(problem_statement: str | None, length: int = 200) -> str:
    # Nullable since migration 010 — generic agents may not use the
    # problem-statement vocabulary at all.
    if not problem_statement:
        return ""
    s = problem_statement.strip().replace("\n", " ")
    return s[:length] + ("…" if len(s) > length else "")


def display_title(run: Run) -> str | None:
    """``run.title``, or — for a row persisted before blueprint S2 computed
    titles at intake, so the column is NULL — the same rule applied now to
    the stored inputs (Codex P2 on PR #50): the installed agent's
    ``ui.list.title_path`` and input schema, or, with the agent gone, the
    first string in the payload. Derived on every read and never written
    back — a read path does not persist."""
    if run.title:
        return run.title
    payload = run.user_inputs
    if not isinstance(payload, dict) or not payload:
        return None
    agent = get_agent(run.agent_id) if run.agent_id else None
    if agent is not None:
        manifest = get_manifest(run.agent_id)
        title_path = manifest.ui.list.title_path if manifest is not None else None
        try:
            schema = agent.input_schema()
        except Exception:  # noqa: BLE001 — a misbehaving agent must not break the run list
            schema = None
        derived = run_title(title_path, schema, payload)
        if derived is not None:
            return derived
    return first_string_in(payload)


def parked_output_phase(manifest: AgentManifest | None, run: Run) -> str | None:
    """The manifest phase whose output ``snapshot.analysis`` holds — the
    producer of the approval payload, not the execution cursor (Codex P2
    on PR #50). Parked at a gate, that is the phase that just ran
    (``current_phase``; the first phase for a pre-009 row that never had
    one). Once approved and resumed, the runner has moved ``current_phase``
    on, and ``analysis`` is the output of the last non-final phase — every
    non-final phase writes it in turn — so with the final phase running or
    done it is ``phases[-2]``. ``None`` when nothing can be said."""
    if run.status == "awaiting_approval":
        if run.current_phase:
            return run.current_phase
        return manifest.phases[0].name if manifest is not None and manifest.phases else None
    if manifest is not None and len(manifest.phases) >= 2:
        return manifest.phases[-2].name
    return None


def build_run_detail_fields(run: Run, snapshot: RunSnapshot | None) -> dict[str, Any]:
    """Build the kwargs dict shared by the customer and admin run-detail
    responses.

    Reads only from the generic agent columns (``snapshot.analysis`` and
    ``snapshot.structured_data``) — the chassis knows no agent's result
    vocabulary (blueprint B9). ``output_mode`` comes from the agent's
    manifest; ``structured_output`` carries the final structured payload
    for ``structured``-mode agents so the UI can render it generically
    (``html_report`` agents ship rendered HTML via the report endpoints
    instead).

    Returns only the fields common to both ``RunDetail`` and
    ``AdminRunDetail``. Admin-only observability fields (trace_id,
    phase2_span_id, trace_url) are layered on by the admin handler.
    """
    manifest = get_manifest(run.agent_id) if run.agent_id else None
    # The approval summary (blueprint S2): the manifest names the string
    # inside the parked output; nothing agent-shaped is read here.
    summary: str | None = None
    if snapshot is not None and isinstance(snapshot.analysis, dict) and snapshot.analysis:
        summary_path = manifest.ui.approval.summary_path if manifest is not None else None
        summary = approval_summary(summary_path, snapshot.analysis)
    output_mode = manifest.output.mode if manifest is not None else "html_report"
    structured_output: dict | None = None
    if (
        output_mode == "structured"
        and snapshot is not None
        and isinstance(snapshot.structured_data, dict)
    ):
        structured_output = snapshot.structured_data
    # Run-scoped feedback vocabulary: exactly the sections POST /feedback
    # will accept for this run, so the UI never renders a control that can
    # only 422 (empty when the agent is unresolvable — the UI hides the
    # panel then).
    feedback_sections = (
        [{"id": s.id, "label": s.label} for s in manifest.feedback_sections]
        if manifest is not None
        else []
    )

    # The title once, for every field that carries it.
    title = display_title(run)

    def _vendor(name, product, feature, observation) -> VendorInput | None:
        # Vendor columns are nullable since migration 010 — a generic
        # agent's run simply has no vendor block.
        if not name:
            return None
        return VendorInput(
            name=name, product=product, feature=feature, observation=observation
        )

    return {
        "id": run.id,
        "run_number": run.run_number,
        "title": title,
        "approval_summary": summary,
        "vendor_a": _vendor(
            run.vendor_a_name,
            run.vendor_a_product,
            run.vendor_a_feature,
            run.vendor_a_observation,
        ),
        "vendor_b": _vendor(
            run.vendor_b_name,
            run.vendor_b_product,
            run.vendor_b_feature,
            run.vendor_b_observation,
        ),
        "vendor_a_name": run.vendor_a_name,
        "vendor_b_name": run.vendor_b_name,
        "problem_summary": summary_text(title),
        "severity": run.severity,
        "status": run.status,
        "created_at": run.created_at,
        "updated_at": run.updated_at,
        "use_case": run.use_case,
        # Deprecated duplicate of ``title`` (identical by contract, so the
        # derived value too — Codex P2 on PR #50), not the raw column.
        "problem_statement": title,
        "impact_statement": run.impact_statement,
        "refined_problem_statement": summary,
        "agent_id": run.agent_id,
        "user_inputs": run.user_inputs,
        "output_mode": output_mode,
        "structured_output": structured_output,
        "feedback_sections": feedback_sections,
        # Why the run ended ``error`` (blueprint S7): the code and the
        # chassis's sentence for it. The operator-facing ``error_detail``
        # is NOT here — the admin handler layers it on, like the raw ids.
        "error_code": run.error_code,
        "error_message": run_errors.user_message(run.error_code),
    }
