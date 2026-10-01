from ..normalizers import SchemaDriftReport, normalize_step_9
from ..prompts import STEP_9_FOLLOWUPS, render
from ..llm_service import LLMService, call_with_retry


async def run(
    llm: LLMService, refined: dict, plan: dict, classified: dict
) -> tuple[dict, list[SchemaDriftReport]]:
    variables = {
        "refined_problem_statement": refined.get("refined_problem_statement", ""),
        "resolution_plan_json": plan,
        "input_quality_score": classified.get("input_quality_score", 0.5),
    }
    step_id = "generate_followup_questions"
    prompt = render(STEP_9_FOLLOWUPS, variables)
    raw = await call_with_retry(llm, step_id, [{"role": "system", "content": prompt}])
    cfg = llm.get_step_config(step_id)
    return normalize_step_9(
        raw,
        step_id=step_id,
        provider=cfg.get("provider", ""),
        model=cfg.get("model", ""),
    )
