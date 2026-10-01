from __future__ import annotations

from ..normalizers import SchemaDriftReport, normalize_step_0
from ..prompts import STEP_0_VALIDATE, render
from ..llm_service import LLMService, call_with_retry


async def run(llm: LLMService, case: Run) -> tuple[dict, list[SchemaDriftReport]]:
    variables = {
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
        "config_file_summaries": "",
    }
    step_id = "validate_and_classify_inputs"
    prompt = render(STEP_0_VALIDATE, variables)
    raw = await call_with_retry(llm, step_id, [{"role": "system", "content": prompt}])
    cfg = llm.get_step_config(step_id)
    return normalize_step_0(
        raw,
        step_id=step_id,
        provider=cfg.get("provider", ""),
        model=cfg.get("model", ""),
    )
