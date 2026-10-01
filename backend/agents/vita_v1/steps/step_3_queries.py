from __future__ import annotations

"""Steps 3 & 4 — Search query construction. Used for both vendor A and vendor B."""
from ..normalizers import SchemaDriftReport, normalize_step_3
from ..prompts import STEP_3_QUERIES, render
from ..llm_service import LLMService, call_with_retry


async def run(
    llm: LLMService, side: str, case: Run, refined: dict, step_id: str
) -> tuple[dict, list[SchemaDriftReport]]:
    if side == "a":
        name = case.vendor_a_name
        product = case.vendor_a_product or ""
        feature = case.vendor_a_feature or ""
    else:
        name = case.vendor_b_name
        product = case.vendor_b_product or ""
        feature = case.vendor_b_feature or ""

    variables = {
        "refined_problem_statement": refined.get("refined_problem_statement", ""),
        "research_focus_areas": refined.get("research_focus_areas", []),
        "vendor_name": name,
        "vendor_product": product,
        "vendor_feature": feature,
        "domain_hints": [],
    }
    prompt = render(STEP_3_QUERIES, variables)
    raw = await call_with_retry(llm, step_id, [{"role": "system", "content": prompt}])
    cfg = llm.get_step_config(step_id)
    result, reports = normalize_step_3(
        raw,
        step_id=step_id,
        provider=cfg.get("provider", ""),
        model=cfg.get("model", ""),
    )
    # _vendor_side is set by this module, not the LLM — downstream search steps
    # use it to route results into the right works_cited bucket.
    result["_vendor_side"] = side
    return result, reports
