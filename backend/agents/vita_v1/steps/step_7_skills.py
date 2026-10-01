from ..normalizers import SchemaDriftReport, normalize_step_7
from ..prompts import STEP_7_SKILLS, render
from ..llm_service import LLMService, call_with_retry


async def run(
    llm: LLMService, refined: dict, web_results: dict, kb_results: dict
) -> tuple[dict, list[SchemaDriftReport]]:
    variables = {
        "refined_problem_statement": refined.get("refined_problem_statement", ""),
        "vendor_a_search_results": web_results.get("vendor_a_results", []),
        "vendor_b_search_results": web_results.get("vendor_b_results", []),
        "internal_kb_results": kb_results.get("results", []),
        "public_search_results": web_results.get("public_results", []),
    }
    step_id = "assess_skills"
    prompt = render(STEP_7_SKILLS, variables)
    raw = await call_with_retry(llm, step_id, [{"role": "system", "content": prompt}])
    cfg = llm.get_step_config(step_id)
    return normalize_step_7(
        raw,
        step_id=step_id,
        provider=cfg.get("provider", ""),
        model=cfg.get("model", ""),
    )
