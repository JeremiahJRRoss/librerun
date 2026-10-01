"""Per-step LLM output normalizers.

Every step that consumes LLM output funnels through one of these functions.
The contract is uniform:

    normalize_step_N(raw, *, step_id, provider, model)
        -> tuple[dict, list[SchemaDriftReport]]

The returned dict matches the canonical shape each step's callers rely on
(see ``backend/tests/fixtures/`` for canonical and observed-drift samples,
and the blueprint's Appendix A for the full schema list). Drift reports
describe every coercion that had to happen — the caller persists them to
the audit log so operators can see model drift without diffing DB rows.

Normalizers never raise on malformed input. ``None``, ``""``, ``[]``, and
arbitrary garbage all produce a canonical empty shape with a
``missing_response`` or ``wrong_type`` drift report.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class SchemaDriftReport:
    step_id: str
    provider: str
    model: str
    drift_type: str
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _empty_report(
    *, step_id: str, provider: str, model: str, reason: str = "missing_response"
) -> SchemaDriftReport:
    return SchemaDriftReport(
        step_id=step_id, provider=provider, model=model, drift_type=reason
    )


def _coerce_str_list(value: Any) -> tuple[list[str], bool]:
    """Return ``(list[str], coerced)``. Wraps bare strings, drops junk."""
    if isinstance(value, list):
        out: list[str] = []
        coerced = False
        for item in value:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, dict):
                # Pull a plausible string field out of dict-shaped items
                for k in ("hypothesis", "description", "text", "value", "name"):
                    if isinstance(item.get(k), str):
                        out.append(item[k])
                        coerced = True
                        break
            elif item is not None:
                out.append(str(item))
                coerced = True
        return out, coerced
    if isinstance(value, str):
        return [value], True
    if value is None:
        return [], False
    return [], True


# ---------------------------------------------------------------------------
# Step 0 — validate_and_classify_inputs
# ---------------------------------------------------------------------------

_STEP0_KEYS = (
    "valid",
    "rejection_reason",
    "primary_vendor",
    "secondary_vendor",
    "integration_type",
    "error_signals",
    "input_quality_score",
)


def _step0_empty() -> dict:
    return {
        "valid": False,
        "rejection_reason": None,
        "primary_vendor": "",
        "secondary_vendor": "",
        "integration_type": "",
        "error_signals": [],
        "input_quality_score": 0.0,
    }


def normalize_step_0(
    raw: Any, *, step_id: str, provider: str, model: str
) -> tuple[dict, list[SchemaDriftReport]]:
    reports: list[SchemaDriftReport] = []

    if raw is None or raw == "":
        return _step0_empty(), [_empty_report(step_id=step_id, provider=provider, model=model)]

    if not isinstance(raw, dict):
        reports.append(
            SchemaDriftReport(
                step_id=step_id,
                provider=provider,
                model=model,
                drift_type="wrong_type",
                details={"observed_type": type(raw).__name__},
            )
        )
        return _step0_empty(), reports

    # Some models wrap the answer under "classified". Flatten.
    if "classified" in raw and isinstance(raw["classified"], dict):
        reports.append(
            SchemaDriftReport(
                step_id=step_id,
                provider=provider,
                model=model,
                drift_type="key_rename",
                details={"unwrapped": "classified"},
            )
        )
        inner = raw["classified"]
        merged = {k: raw[k] for k in ("valid", "rejection_reason") if k in raw}
        merged.update(inner)
        raw = merged

    out = _step0_empty()
    out["valid"] = bool(raw.get("valid", True))
    rr = raw.get("rejection_reason")
    out["rejection_reason"] = rr if isinstance(rr, str) and rr else None

    for key in ("primary_vendor", "secondary_vendor", "integration_type"):
        v = raw.get(key, "")
        if not isinstance(v, str):
            reports.append(
                SchemaDriftReport(
                    step_id=step_id, provider=provider, model=model,
                    drift_type="wrong_type",
                    details={"field": key, "observed_type": type(v).__name__},
                )
            )
            v = str(v) if v is not None else ""
        out[key] = v

    signals, coerced = _coerce_str_list(raw.get("error_signals"))
    if coerced:
        reports.append(
            SchemaDriftReport(
                step_id=step_id, provider=provider, model=model,
                drift_type="wrong_type", details={"field": "error_signals"},
            )
        )
    out["error_signals"] = signals

    iq = raw.get("input_quality_score", 0.0)
    try:
        iq_f = float(iq)
    except (TypeError, ValueError):
        reports.append(
            SchemaDriftReport(
                step_id=step_id, provider=provider, model=model,
                drift_type="wrong_type", details={"field": "input_quality_score"},
            )
        )
        iq_f = 0.0
    out["input_quality_score"] = max(0.0, min(1.0, iq_f))

    return out, reports


# ---------------------------------------------------------------------------
# Step 2 — refine_problem_statement
# ---------------------------------------------------------------------------

_STEP2_SUBKEY_ORDER = (
    "integration_setup",
    "observed_failure_mode",
    "error_codes_log_entries_metrics",
    "asymmetry",
    "suspected_layer",
)


def _step2_empty() -> dict:
    return {
        "refined_problem_statement": "",
        "key_signals": [],
        "suspected_root_causes": [],
        "research_focus_areas": [],
    }


def _flatten_problem_statement_dict(d: dict) -> str:
    parts: list[str] = []
    for k in _STEP2_SUBKEY_ORDER:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            parts.append(v.strip())
    extras = sorted(k for k in d.keys() if k not in _STEP2_SUBKEY_ORDER)
    for k in extras:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            parts.append(v.strip())
    return "\n\n".join(parts)


def _flatten_focus_areas_dict(d: dict) -> list[str]:
    out: list[str] = []
    for vendor, topics in d.items():
        if isinstance(topics, list):
            for t in topics:
                if isinstance(t, str):
                    out.append(f"{vendor}: {t}")
        elif isinstance(topics, str):
            out.append(f"{vendor}: {topics}")
    return out


def normalize_step_2(
    raw: Any, *, step_id: str, provider: str, model: str
) -> tuple[dict, list[SchemaDriftReport]]:
    reports: list[SchemaDriftReport] = []

    if raw is None or raw == "":
        return _step2_empty(), [_empty_report(step_id=step_id, provider=provider, model=model)]

    if isinstance(raw, list):
        # Bare list: best effort — concat string items into the statement.
        strings = [x for x in raw if isinstance(x, str)]
        reports.append(
            SchemaDriftReport(
                step_id=step_id, provider=provider, model=model,
                drift_type="wrong_type",
                details={"observed_type": "list", "item_count": len(raw)},
            )
        )
        out = _step2_empty()
        out["refined_problem_statement"] = "\n\n".join(strings)
        return out, reports

    if not isinstance(raw, dict):
        reports.append(
            SchemaDriftReport(
                step_id=step_id, provider=provider, model=model,
                drift_type="wrong_type", details={"observed_type": type(raw).__name__},
            )
        )
        return _step2_empty(), reports

    out = _step2_empty()

    # refined_problem_statement — canonical key, or renamed from problem_statement,
    # flattened if nested.
    stmt = raw.get("refined_problem_statement")
    if stmt is None and "problem_statement" in raw:
        src = raw["problem_statement"]
        if isinstance(src, dict):
            out["refined_problem_statement"] = _flatten_problem_statement_dict(src)
            reports.append(
                SchemaDriftReport(
                    step_id=step_id, provider=provider, model=model,
                    drift_type="object_to_string_flattening",
                    details={"field": "problem_statement",
                             "sub_keys": sorted(src.keys())},
                )
            )
        elif isinstance(src, str):
            out["refined_problem_statement"] = src
            reports.append(
                SchemaDriftReport(
                    step_id=step_id, provider=provider, model=model,
                    drift_type="key_rename",
                    details={"from": "problem_statement", "to": "refined_problem_statement"},
                )
            )
        else:
            out["refined_problem_statement"] = ""
            reports.append(
                SchemaDriftReport(
                    step_id=step_id, provider=provider, model=model,
                    drift_type="wrong_type",
                    details={"field": "problem_statement",
                             "observed_type": type(src).__name__},
                )
            )
    elif isinstance(stmt, str):
        out["refined_problem_statement"] = stmt
    elif isinstance(stmt, dict):
        out["refined_problem_statement"] = _flatten_problem_statement_dict(stmt)
        reports.append(
            SchemaDriftReport(
                step_id=step_id, provider=provider, model=model,
                drift_type="object_to_string_flattening",
                details={"field": "refined_problem_statement",
                         "sub_keys": sorted(stmt.keys())},
            )
        )
    elif stmt is None:
        out["refined_problem_statement"] = ""
    else:
        out["refined_problem_statement"] = str(stmt)
        reports.append(
            SchemaDriftReport(
                step_id=step_id, provider=provider, model=model,
                drift_type="wrong_type",
                details={"field": "refined_problem_statement",
                         "observed_type": type(stmt).__name__},
            )
        )

    # key_signals
    signals, coerced = _coerce_str_list(raw.get("key_signals"))
    if coerced:
        reports.append(
            SchemaDriftReport(
                step_id=step_id, provider=provider, model=model,
                drift_type="wrong_type", details={"field": "key_signals"},
            )
        )
    out["key_signals"] = signals

    # suspected_root_causes — strings or dicts with hypothesis/description
    causes, coerced = _coerce_str_list(raw.get("suspected_root_causes"))
    if coerced:
        reports.append(
            SchemaDriftReport(
                step_id=step_id, provider=provider, model=model,
                drift_type="wrong_type", details={"field": "suspected_root_causes"},
            )
        )
    out["suspected_root_causes"] = causes

    # research_focus_areas — canonical is list[str]; drift: dict keyed by vendor.
    rfa = raw.get("research_focus_areas")
    if isinstance(rfa, dict):
        out["research_focus_areas"] = _flatten_focus_areas_dict(rfa)
        reports.append(
            SchemaDriftReport(
                step_id=step_id, provider=provider, model=model,
                drift_type="dict_to_list_flattening",
                details={"field": "research_focus_areas",
                         "vendors": sorted(rfa.keys())},
            )
        )
    else:
        rfa_list, coerced = _coerce_str_list(rfa)
        if coerced:
            reports.append(
                SchemaDriftReport(
                    step_id=step_id, provider=provider, model=model,
                    drift_type="wrong_type", details={"field": "research_focus_areas"},
                )
            )
        out["research_focus_areas"] = rfa_list

    return out, reports


# ---------------------------------------------------------------------------
# Step 3 — construct_search_queries_vendor_{a,b}
# ---------------------------------------------------------------------------

_STEP3_WEB_SYNONYMS = ("search_queries", "web_search_queries", "queries", "web")
_STEP3_VECTOR_SYNONYMS = ("semantic_queries", "vector_search_queries", "similarity_queries", "vector")
_STEP3_DOMAIN_SYNONYMS = ("domains", "domain_list", "suggested_domains")


def _step3_empty() -> dict:
    return {"web_queries": [], "vector_queries": [], "domain_hints": []}


def _resolve_synonym(
    raw: dict,
    canonical: str,
    synonyms: tuple[str, ...],
    step_id: str,
    provider: str,
    model: str,
    reports: list[SchemaDriftReport],
) -> list[str]:
    if canonical in raw and isinstance(raw[canonical], list):
        items, _ = _coerce_str_list(raw[canonical])
        return items
    for alt in synonyms:
        if alt in raw and isinstance(raw[alt], list):
            reports.append(
                SchemaDriftReport(
                    step_id=step_id, provider=provider, model=model,
                    drift_type="key_rename", details={"from": alt, "to": canonical},
                )
            )
            items, _ = _coerce_str_list(raw[alt])
            return items
    return []


def normalize_step_3(
    raw: Any, *, step_id: str, provider: str, model: str
) -> tuple[dict, list[SchemaDriftReport]]:
    reports: list[SchemaDriftReport] = []

    if raw is None or raw == "":
        return _step3_empty(), [_empty_report(step_id=step_id, provider=provider, model=model)]

    if isinstance(raw, list):
        # Bare list = treat items as web queries.
        items, _ = _coerce_str_list(raw)
        reports.append(
            SchemaDriftReport(
                step_id=step_id, provider=provider, model=model,
                drift_type="wrong_type",
                details={"observed_type": "list", "assumed_field": "web_queries"},
            )
        )
        out = _step3_empty()
        out["web_queries"] = items
        return out, reports

    if not isinstance(raw, dict):
        reports.append(
            SchemaDriftReport(
                step_id=step_id, provider=provider, model=model,
                drift_type="wrong_type", details={"observed_type": type(raw).__name__},
            )
        )
        return _step3_empty(), reports

    out = _step3_empty()
    out["web_queries"] = _resolve_synonym(
        raw, "web_queries", _STEP3_WEB_SYNONYMS, step_id, provider, model, reports
    )
    out["vector_queries"] = _resolve_synonym(
        raw, "vector_queries", _STEP3_VECTOR_SYNONYMS, step_id, provider, model, reports
    )
    out["domain_hints"] = _resolve_synonym(
        raw, "domain_hints", _STEP3_DOMAIN_SYNONYMS, step_id, provider, model, reports
    )
    return out, reports


# ---------------------------------------------------------------------------
# Step 7 — assess_skills
# ---------------------------------------------------------------------------

_STEP7_SKILL_SOURCES = ("vendor_docs", "internal_kb", "public_web", "llm_knowledge")


def _step7_empty() -> dict:
    return {"skills": []}


def _normalize_skill_item(item: Any) -> tuple[dict | None, bool]:
    """Return (normalized_skill, coerced). ``None`` means drop this entry."""
    if not isinstance(item, dict):
        return None, True
    coerced = False
    out: dict[str, Any] = {}

    name = item.get("name") or item.get("skill") or item.get("title")
    if not isinstance(name, str) or not name.strip():
        return None, True
    if name != item.get("name"):
        coerced = True
    out["name"] = name

    desc = item.get("description") or item.get("summary") or ""
    if not isinstance(desc, str):
        desc = str(desc)
        coerced = True
    out["description"] = desc

    weight = item.get("relevance_weight")
    if weight is None:
        weight = item.get("weight") or item.get("relevance")
        if weight is not None:
            coerced = True
    try:
        weight_f = float(weight) if weight is not None else 0.5
    except (TypeError, ValueError):
        weight_f = 0.5
        coerced = True
    out["relevance_weight"] = max(0.0, min(1.0, weight_f))

    source = item.get("source")
    if source not in _STEP7_SKILL_SOURCES:
        coerced = True
        source = "llm_knowledge"
    out["source"] = source

    return out, coerced


def normalize_step_7(
    raw: Any, *, step_id: str, provider: str, model: str
) -> tuple[dict, list[SchemaDriftReport]]:
    reports: list[SchemaDriftReport] = []

    if raw is None or raw == "":
        return _step7_empty(), [_empty_report(step_id=step_id, provider=provider, model=model)]

    items: list[Any]
    if isinstance(raw, list):
        items = raw
        reports.append(
            SchemaDriftReport(
                step_id=step_id, provider=provider, model=model,
                drift_type="wrong_type",
                details={"observed_type": "list", "assumed_field": "skills"},
            )
        )
    elif isinstance(raw, dict):
        cand = raw.get("skills")
        if isinstance(cand, list):
            items = cand
        else:
            # accept synonyms
            for alt in ("skill_list", "assessment"):
                if isinstance(raw.get(alt), list):
                    reports.append(
                        SchemaDriftReport(
                            step_id=step_id, provider=provider, model=model,
                            drift_type="key_rename", details={"from": alt, "to": "skills"},
                        )
                    )
                    items = raw[alt]
                    break
            else:
                reports.append(
                    SchemaDriftReport(
                        step_id=step_id, provider=provider, model=model,
                        drift_type="missing_key", details={"field": "skills"},
                    )
                )
                return _step7_empty(), reports
    else:
        reports.append(
            SchemaDriftReport(
                step_id=step_id, provider=provider, model=model,
                drift_type="wrong_type", details={"observed_type": type(raw).__name__},
            )
        )
        return _step7_empty(), reports

    out_skills: list[dict] = []
    any_coerced = False
    for item in items:
        norm, coerced = _normalize_skill_item(item)
        if norm is None:
            any_coerced = True
            continue
        if coerced:
            any_coerced = True
        out_skills.append(norm)

    if any_coerced:
        reports.append(
            SchemaDriftReport(
                step_id=step_id, provider=provider, model=model,
                drift_type="wrong_type", details={"field": "skills[*]"},
            )
        )

    return {"skills": out_skills}, reports


# ---------------------------------------------------------------------------
# Step 8 — generate_resolution_plan (incl. phantom-citation repair)
# ---------------------------------------------------------------------------

_STEP8_SECTIONS = ("mitigation", "resolution", "avoidance")
_ALLOWED_DOC_TYPES = {
    "troubleshooting_guide",
    "api_reference",
    "kb_article",
    "community_post",
    "release_notes",
}
import re as _re  # noqa: E402  # kept local to this package

_CITATION_TOKEN_RE = _re.compile(r"\s*\[(\d+)\]")


def _step8_empty() -> dict:
    return {
        "mitigation": {"text": "", "citations": []},
        "resolution": {"text": "", "citations": []},
        "avoidance": {"text": "", "citations": []},
        "works_cited": {"vendor_a": [], "vendor_b": []},
    }


def _normalize_citation(c: Any) -> dict | None:
    if not isinstance(c, dict):
        return None
    out: dict[str, Any] = dict(c)

    if "relevance_score" not in out:
        for alt in ("relevance", "score"):
            if alt in out:
                try:
                    v = float(out[alt])
                    out["relevance_score"] = max(0.0, min(1.0, v))
                    break
                except (TypeError, ValueError):
                    pass

    if "doc_type" not in out:
        candidate = out.get("type")
        if isinstance(candidate, str) and candidate in _ALLOWED_DOC_TYPES:
            out["doc_type"] = candidate
        else:
            out["doc_type"] = "kb_article"

    out.setdefault("title", "")
    out.setdefault("url", "")
    out.setdefault("summary", "")
    out.setdefault("relevance_score", 0.0)
    return out


def _normalize_works_cited(
    wc: Any, step_id: str, provider: str, model: str, reports: list[SchemaDriftReport]
) -> dict:
    if not isinstance(wc, dict):
        reports.append(
            SchemaDriftReport(
                step_id=step_id, provider=provider, model=model,
                drift_type="wrong_type",
                details={"field": "works_cited", "observed_type": type(wc).__name__},
            )
        )
        return {"vendor_a": [], "vendor_b": []}

    out: dict[str, list] = {"vendor_a": [], "vendor_b": []}
    for side in ("vendor_a", "vendor_b"):
        items = wc.get(side)
        if items is None:
            reports.append(
                SchemaDriftReport(
                    step_id=step_id, provider=provider, model=model,
                    drift_type="missing_key", details={"field": f"works_cited.{side}"},
                )
            )
            continue
        if not isinstance(items, list):
            reports.append(
                SchemaDriftReport(
                    step_id=step_id, provider=provider, model=model,
                    drift_type="wrong_type",
                    details={"field": f"works_cited.{side}",
                             "observed_type": type(items).__name__},
                )
            )
            continue
        for c in items:
            nc = _normalize_citation(c)
            if nc is not None:
                out[side].append(nc)
    return out


def _strip_phantom_tokens(text: str, valid_ids: set[int]) -> tuple[str, list[int]]:
    """Remove ``[N]`` tokens referencing IDs not in ``valid_ids``.

    Returns ``(cleaned_text, phantom_ids)``. Leading whitespace before the
    stripped token is consumed (``"do X [99] then Y"`` -> ``"do X then Y"``).
    """
    phantoms: list[int] = []

    def repl(m: "_re.Match[str]") -> str:
        cid = int(m.group(1))
        if cid in valid_ids:
            return m.group(0)
        phantoms.append(cid)
        return ""

    return _CITATION_TOKEN_RE.sub(repl, text), phantoms


def _normalize_section(
    raw: Any, valid_ids: set[int]
) -> tuple[dict, list[int]]:
    """Return ``({"text": ..., "citations": [...]}, phantom_ids)``."""
    if not isinstance(raw, dict):
        return {"text": "", "citations": []}, []
    text = raw.get("text", "")
    if not isinstance(text, str):
        text = str(text) if text is not None else ""
    raw_citations = raw.get("citations") or []
    if not isinstance(raw_citations, list):
        raw_citations = []

    cleaned_text, phantoms_in_text = _strip_phantom_tokens(text, valid_ids)

    citations: list[int] = []
    phantoms_in_list: list[int] = []
    for c in raw_citations:
        try:
            cid = int(c)
        except (TypeError, ValueError):
            continue
        if cid in valid_ids:
            if cid not in citations:
                citations.append(cid)
        else:
            phantoms_in_list.append(cid)

    all_phantoms = list({*phantoms_in_text, *phantoms_in_list})
    return {"text": cleaned_text, "citations": citations}, all_phantoms


def normalize_step_8(
    raw: Any, *, step_id: str, provider: str, model: str
) -> tuple[dict, list[SchemaDriftReport]]:
    reports: list[SchemaDriftReport] = []

    if raw is None or raw == "":
        return _step8_empty(), [_empty_report(step_id=step_id, provider=provider, model=model)]

    if not isinstance(raw, dict):
        reports.append(
            SchemaDriftReport(
                step_id=step_id, provider=provider, model=model,
                drift_type="wrong_type", details={"observed_type": type(raw).__name__},
            )
        )
        return _step8_empty(), reports

    works_cited = _normalize_works_cited(
        raw.get("works_cited"), step_id, provider, model, reports
    )
    valid_ids: set[int] = set()
    for side in ("vendor_a", "vendor_b"):
        for c in works_cited[side]:
            cid = c.get("id")
            if isinstance(cid, int):
                valid_ids.add(cid)

    out: dict[str, Any] = {"works_cited": works_cited}
    phantoms_by_section: dict[str, list[int]] = {}
    for section in _STEP8_SECTIONS:
        section_out, phantoms = _normalize_section(raw.get(section), valid_ids)
        out[section] = section_out
        if phantoms:
            phantoms_by_section[section] = sorted(phantoms)

    if phantoms_by_section:
        reports.append(
            SchemaDriftReport(
                step_id=step_id, provider=provider, model=model,
                drift_type="phantom_citation",
                details={"phantoms_by_section": phantoms_by_section,
                         "valid_ids": sorted(valid_ids)},
            )
        )

    return out, reports


# ---------------------------------------------------------------------------
# Step 9 — generate_followup_questions
# ---------------------------------------------------------------------------

def _step9_empty() -> dict:
    return {"questions": []}


def _normalize_followup(item: Any) -> dict | None:
    if not isinstance(item, dict):
        return None
    q = item.get("question") or item.get("text")
    if not isinstance(q, str) or not q.strip():
        return None
    return {
        "question": q,
        "rationale": item.get("rationale", "") if isinstance(item.get("rationale"), str) else "",
        "expected_impact": (
            item.get("expected_impact", "")
            if isinstance(item.get("expected_impact"), str)
            else ""
        ),
    }


def normalize_step_9(
    raw: Any, *, step_id: str, provider: str, model: str
) -> tuple[dict, list[SchemaDriftReport]]:
    reports: list[SchemaDriftReport] = []

    if raw is None or raw == "":
        return _step9_empty(), [_empty_report(step_id=step_id, provider=provider, model=model)]

    items: list[Any]
    if isinstance(raw, list):
        items = raw
        reports.append(
            SchemaDriftReport(
                step_id=step_id, provider=provider, model=model,
                drift_type="wrong_type",
                details={"observed_type": "list", "assumed_field": "questions"},
            )
        )
    elif isinstance(raw, dict):
        cand = raw.get("questions")
        if isinstance(cand, list):
            items = cand
        else:
            for alt in ("followups", "followup_questions", "next_questions"):
                if isinstance(raw.get(alt), list):
                    reports.append(
                        SchemaDriftReport(
                            step_id=step_id, provider=provider, model=model,
                            drift_type="key_rename",
                            details={"from": alt, "to": "questions"},
                        )
                    )
                    items = raw[alt]
                    break
            else:
                reports.append(
                    SchemaDriftReport(
                        step_id=step_id, provider=provider, model=model,
                        drift_type="missing_key", details={"field": "questions"},
                    )
                )
                return _step9_empty(), reports
    else:
        reports.append(
            SchemaDriftReport(
                step_id=step_id, provider=provider, model=model,
                drift_type="wrong_type", details={"observed_type": type(raw).__name__},
            )
        )
        return _step9_empty(), reports

    out: list[dict] = []
    any_coerced = False
    for item in items:
        norm = _normalize_followup(item)
        if norm is None:
            any_coerced = True
            continue
        out.append(norm)

    if any_coerced:
        reports.append(
            SchemaDriftReport(
                step_id=step_id, provider=provider, model=model,
                drift_type="wrong_type", details={"field": "questions[*]"},
            )
        )

    return {"questions": out}, reports
