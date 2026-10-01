from __future__ import annotations

from ..normalizers import SchemaDriftReport, normalize_step_2
from ..prompts import STEP_2_REFINE, render
from ..llm_service import LLMService, call_with_retry


async def run(
    llm: LLMService,
    case: Run,
    classified_inputs: dict,
    customer_edit: str | None = None,
) -> tuple[dict, list[SchemaDriftReport]]:
    variables = {
        "classified_inputs_json": classified_inputs,
        "vendor_a_name": case.vendor_a_name,
        "vendor_a_product": case.vendor_a_product or "",
        "vendor_a_feature": case.vendor_a_feature or "",
        "vendor_a_observation": case.vendor_a_observation or "",
        "vendor_b_name": case.vendor_b_name,
        "vendor_b_product": case.vendor_b_product or "",
        "vendor_b_feature": case.vendor_b_feature or "",
        "vendor_b_observation": case.vendor_b_observation or "",
        "logs_a": (case.logs_a or "")[:4000],
        "logs_b": (case.logs_b or "")[:4000],
        "use_case": case.use_case,
        "problem_statement": case.problem_statement,
        "impact_statement": case.impact_statement or "",
        "severity": case.severity or "",
        "config_summaries": "",
    }
    step_id = "refine_problem_statement"
    prompt = render(STEP_2_REFINE, variables)
    messages = [{"role": "system", "content": prompt}]
    if customer_edit:
        messages.append(
            {
                "role": "user",
                "content": f"Customer edited the prior refinement to the following; use this as the authoritative input:\n\n{customer_edit}",
            }
        )
    raw = await call_with_retry(llm, step_id, messages)
    cfg = llm.get_step_config(step_id)
    return normalize_step_2(
        raw,
        step_id=step_id,
        provider=cfg.get("provider", ""),
        model=cfg.get("model", ""),
    )
