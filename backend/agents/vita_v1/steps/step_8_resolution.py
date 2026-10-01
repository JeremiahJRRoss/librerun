"""Step 8 — Resolution plan generation with citation assignment."""
from ..normalizers import SchemaDriftReport, normalize_step_8
from ..prompts import STEP_8_RESOLUTION, render
from ..llm_service import LLMService, call_with_retry


def _assign_ids(results: list[dict], start: int) -> tuple[list[dict], int]:
    out = []
    for r in results:
        out.append({**r, "id": start})
        start += 1
    return out, start


async def run(
    llm: LLMService, refined: dict, skills: dict, web_results: dict, kb_results: dict
) -> tuple[dict, list[SchemaDriftReport]]:
    # Assign sequential IDs across vendor A, vendor B, internal KB, public
    next_id = 1
    vendor_a, next_id = _assign_ids(web_results.get("vendor_a_results", []), next_id)
    vendor_b, next_id = _assign_ids(web_results.get("vendor_b_results", []), next_id)
    internal, next_id = _assign_ids(kb_results.get("results", []), next_id)
    public, next_id = _assign_ids(web_results.get("public_results", []), next_id)

    variables = {
        "refined_problem_statement": refined.get("refined_problem_statement", ""),
        "skills_assessment": skills.get("skills", []),
        "vendor_a_results_with_ids": vendor_a,
        "vendor_b_results_with_ids": vendor_b,
        "internal_kb_results_with_ids": internal,
        "public_results_with_ids": public,
        "config_summaries": "",
    }
    step_id = "generate_resolution_plan"
    prompt = render(STEP_8_RESOLUTION, variables)
    raw = await call_with_retry(llm, step_id, [{"role": "system", "content": prompt}])
    cfg = llm.get_step_config(step_id)
    return normalize_step_8(
        raw,
        step_id=step_id,
        provider=cfg.get("provider", ""),
        model=cfg.get("model", ""),
    )
