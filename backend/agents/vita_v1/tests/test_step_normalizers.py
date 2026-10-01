"""Tests for per-step LLM output normalizers.

Fixtures are observed JSON pulled from a real case's ``run_snapshots`` row
(see ``backend/tests/fixtures/``). Synthetic inputs appear only where no
observed drift sample exists for that scenario (notably Step 8 phantom
citations, which the observed case does not exhibit).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agents.vita_v1.normalizers import (
    SchemaDriftReport,
    normalize_step_0,
    normalize_step_2,
    normalize_step_3,
    normalize_step_7,
    normalize_step_8,
    normalize_step_9,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str):
    return json.loads((FIXTURES / name).read_text())


_META = {"step_id": "t", "provider": "openai", "model": "gpt-4o"}


# ---------------------------------------------------------------------------
# Step 0
# ---------------------------------------------------------------------------

def test_step0_clean_input_produces_no_drift():
    raw = _load("classified_inputs_normal.json")
    out, reports = normalize_step_0(raw, **_META)
    assert reports == []
    assert out["valid"] is True
    assert out["primary_vendor"] == "Elastic"
    assert out["input_quality_score"] == pytest.approx(0.8)


def test_step0_unwraps_classified_nested():
    raw = {"valid": True, "classified": {"primary_vendor": "X", "input_quality_score": 0.5}}
    out, reports = normalize_step_0(raw, **_META)
    assert out["primary_vendor"] == "X"
    assert any(r.drift_type == "key_rename" for r in reports)


# ---------------------------------------------------------------------------
# Step 2 — the bug that prompted this remediation
# ---------------------------------------------------------------------------

def test_step2_gpt4o_drift_is_normalized():
    raw = _load("step2_gpt4o_drift.json")
    expected = _load("step2_expected_shape.json")

    out, reports = normalize_step_2(raw, **_META)

    assert isinstance(out["refined_problem_statement"], str)
    assert out["refined_problem_statement"] == expected["refined_problem_statement"]
    assert out["research_focus_areas"] == expected["research_focus_areas"]
    assert out["key_signals"] == expected["key_signals"]
    assert out["suspected_root_causes"] == expected["suspected_root_causes"]

    drift_types = {r.drift_type for r in reports}
    assert "object_to_string_flattening" in drift_types
    assert "dict_to_list_flattening" in drift_types


def test_step2_clean_input_produces_no_drift():
    raw = _load("step2_expected_shape.json")
    out, reports = normalize_step_2(raw, **_META)
    assert reports == []
    assert out == raw


def test_step2_bare_list_is_handled():
    out, reports = normalize_step_2([{"a": 1}, "hello"], **_META)
    assert out["refined_problem_statement"] == "hello"
    assert any(r.drift_type == "wrong_type" for r in reports)


def test_step2_none_input_is_handled():
    out, reports = normalize_step_2(None, **_META)
    assert out["refined_problem_statement"] == ""
    assert out["research_focus_areas"] == []
    assert len(reports) == 1 and reports[0].drift_type == "missing_response"


def test_step2_renames_flat_problem_statement():
    raw = {"problem_statement": "flat string", "key_signals": ["a"]}
    out, reports = normalize_step_2(raw, **_META)
    assert out["refined_problem_statement"] == "flat string"
    assert any(r.drift_type == "key_rename" for r in reports)


def test_step2_dict_cause_items_get_unwrapped():
    raw = {
        "refined_problem_statement": "ok",
        "suspected_root_causes": [
            {"hypothesis": "h1"},
            {"description": "d1"},
            "s1",
        ],
    }
    out, _ = normalize_step_2(raw, **_META)
    assert out["suspected_root_causes"] == ["h1", "d1", "s1"]


# ---------------------------------------------------------------------------
# Step 3
# ---------------------------------------------------------------------------

def test_step3_clean_input_produces_no_drift():
    raw = {"web_queries": ["a"], "vector_queries": ["b"], "domain_hints": ["c"]}
    out, reports = normalize_step_3(raw, **_META)
    assert out == raw
    assert reports == []


def test_step3_synonym_rename_is_reported():
    raw = {"search_queries": ["a"], "semantic_queries": ["b"]}
    out, reports = normalize_step_3(raw, **_META)
    assert out["web_queries"] == ["a"]
    assert out["vector_queries"] == ["b"]
    renames = {r.details.get("from") for r in reports if r.drift_type == "key_rename"}
    assert renames == {"search_queries", "semantic_queries"}


def test_step3_bare_list_becomes_web_queries():
    out, reports = normalize_step_3(["q1", "q2"], **_META)
    assert out["web_queries"] == ["q1", "q2"]
    assert any(r.drift_type == "wrong_type" for r in reports)


# ---------------------------------------------------------------------------
# Step 7
# ---------------------------------------------------------------------------

def test_step7_clean_input_produces_no_drift():
    skills = _load("skills_cited_normal.json")
    out, reports = normalize_step_7({"skills": skills}, **_META)
    assert reports == []
    assert out["skills"] == skills


def test_step7_bare_list_is_wrapped():
    skills = _load("skills_cited_normal.json")
    out, reports = normalize_step_7(skills, **_META)
    assert out["skills"] == skills
    assert any(r.drift_type == "wrong_type" for r in reports)


def test_step7_drops_items_missing_name():
    raw = {"skills": [{"description": "no name"}, {"name": "ok"}]}
    out, reports = normalize_step_7(raw, **_META)
    assert [s["name"] for s in out["skills"]] == ["ok"]
    assert any(r.drift_type == "wrong_type" for r in reports)


def test_step7_unknown_source_coerced_to_llm_knowledge():
    raw = {"skills": [{"name": "n", "source": "twitter"}]}
    out, _ = normalize_step_7(raw, **_META)
    assert out["skills"][0]["source"] == "llm_knowledge"


# ---------------------------------------------------------------------------
# Step 8 — normal case and phantom citations
# ---------------------------------------------------------------------------

def test_step8_normal_passes_through_with_observed_citations():
    plan = _load("step8_normal.json")
    works_cited_a = _load("works_cited_a_normal.json")
    works_cited_b = _load("works_cited_b_normal.json")
    raw = {**plan, "works_cited": {"vendor_a": works_cited_a, "vendor_b": works_cited_b}}

    out, reports = normalize_step_8(raw, **_META)

    # No phantom reports — every cited ID exists in works_cited.
    assert not any(r.drift_type == "phantom_citation" for r in reports)
    # Text and citation lists preserved.
    assert out["mitigation"]["citations"] == [11, 12]
    assert out["resolution"]["citations"] == [6, 24]
    assert out["avoidance"]["citations"] == [39, 31]
    assert "[39]" in out["avoidance"]["text"]
    assert "[31]" in out["avoidance"]["text"]


def test_step8_phantom_citations_are_stripped():
    raw = {
        "works_cited": {
            "vendor_a": [
                {"id": 1, "title": "t1", "url": "u1"},
                {"id": 2, "title": "t2", "url": "u2"},
            ],
            "vendor_b": [
                {"id": 3, "title": "t3", "url": "u3"},
                {"id": 4, "title": "t4", "url": "u4"},
                {"id": 5, "title": "t5", "url": "u5"},
            ],
        },
        "mitigation": {
            "text": "do X [1] then do Y [9], also [99] and [2].",
            "citations": [1, 9, 99, 2],
        },
        "resolution": {"text": "plain [3]", "citations": [3]},
        "avoidance": {"text": "nope [77]", "citations": [77]},
    }
    out, reports = normalize_step_8(raw, **_META)

    assert "[1]" in out["mitigation"]["text"]
    assert "[2]" in out["mitigation"]["text"]
    assert "[9]" not in out["mitigation"]["text"]
    assert "[99]" not in out["mitigation"]["text"]
    assert out["mitigation"]["citations"] == [1, 2]

    assert out["avoidance"]["text"] == "nope"
    assert out["avoidance"]["citations"] == []

    phantom_report = next(r for r in reports if r.drift_type == "phantom_citation")
    phantoms = phantom_report.details["phantoms_by_section"]
    assert phantoms["mitigation"] == [9, 99]
    assert phantoms["avoidance"] == [77]
    assert "resolution" not in phantoms


def test_step8_missing_works_cited_sides_emit_drift():
    out, reports = normalize_step_8(
        {"works_cited": {}, "mitigation": {"text": "ok", "citations": []}}, **_META
    )
    assert out["works_cited"] == {"vendor_a": [], "vendor_b": []}
    missing = [r for r in reports if r.drift_type == "missing_key"]
    assert {r.details["field"] for r in missing} == {
        "works_cited.vendor_a",
        "works_cited.vendor_b",
    }


# ---------------------------------------------------------------------------
# Step 9
# ---------------------------------------------------------------------------

def test_step9_clean_input_produces_no_drift():
    qs = _load("followups_normal.json")
    out, reports = normalize_step_9({"questions": qs}, **_META)
    assert reports == []
    assert out["questions"] == qs


def test_step9_bare_list_is_wrapped():
    qs = _load("followups_normal.json")
    out, reports = normalize_step_9(qs, **_META)
    assert out["questions"] == qs
    assert any(r.drift_type == "wrong_type" for r in reports)


def test_step9_drops_items_without_question():
    raw = {"questions": [{"rationale": "r"}, {"question": "q?"}]}
    out, _ = normalize_step_9(raw, **_META)
    assert [q["question"] for q in out["questions"]] == ["q?"]


# ---------------------------------------------------------------------------
# Fuzz: no normalizer may raise on arbitrary garbage
# ---------------------------------------------------------------------------

_NORMALIZERS = [
    normalize_step_0,
    normalize_step_2,
    normalize_step_3,
    normalize_step_7,
    normalize_step_8,
    normalize_step_9,
]

_GARBAGE = [
    None,
    "",
    "just a string",
    42,
    3.14,
    True,
    [],
    {},
    [None, {}, ""],
    {"random": {"nested": ["garbage"]}},
    {"valid": "not a bool", "skills": "not a list"},
]


@pytest.mark.parametrize("normalizer", _NORMALIZERS)
@pytest.mark.parametrize("garbage", _GARBAGE)
def test_no_normalizer_raises_on_garbage(normalizer, garbage):
    out, reports = normalizer(garbage, **_META)
    assert isinstance(out, dict)
    assert all(isinstance(r, SchemaDriftReport) for r in reports)
