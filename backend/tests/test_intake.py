"""Unit tests for the generic intake helpers (blueprint B8).

``POST /runs`` bodies are per-agent JSON-Schema-validated payloads; these
pin the three chassis pieces: schema validation, x-pii redaction before
persist, and the well-known-key lift into the legacy VITA-shaped columns.
"""
from __future__ import annotations

import pytest

from app.services.intake import (
    extract_legacy_columns,
    redact_pii_fields,
    validate_user_inputs,
)

_SCHEMA = {
    "type": "object",
    "required": ["vendor_a", "use_case"],
    "additionalProperties": False,
    "properties": {
        "vendor_a": {
            "type": "object",
            "required": ["name"],
            "properties": {
                "name": {"type": "string", "minLength": 1},
                "notes": {"type": "string", "x-pii": True},
            },
        },
        "use_case": {"type": "string", "minLength": 10},
        "logs_a": {"type": "string", "x-pii": True},
        "count": {"type": "integer", "x-pii": True},  # non-string: never redacted
        "severity": {"type": "string", "enum": ["critical", "high", "medium", "low"]},
    },
}


# --------------------------- validate_user_inputs ----------------------------


def test_valid_payload_passes():
    payload = {"vendor_a": {"name": "A"}, "use_case": "long enough here"}
    assert validate_user_inputs(_SCHEMA, payload) == []


def test_non_dict_body_rejected():
    assert validate_user_inputs(_SCHEMA, ["not", "a", "dict"]) == [
        "body: must be a JSON object"
    ]


def test_missing_required_reported_with_path():
    errors = validate_user_inputs(_SCHEMA, {"use_case": "long enough here"})
    assert any(e.startswith("body: ") and "vendor_a" in e for e in errors)


def test_nested_required_reported_with_path():
    errors = validate_user_inputs(
        _SCHEMA, {"vendor_a": {}, "use_case": "long enough here"}
    )
    assert any(e.startswith("vendor_a: ") and "name" in e for e in errors)


def test_min_length_reported():
    errors = validate_user_inputs(
        _SCHEMA, {"vendor_a": {"name": "A"}, "use_case": "short"}
    )
    assert any(e.startswith("use_case: ") for e in errors)


def test_additional_property_rejected():
    errors = validate_user_inputs(
        _SCHEMA,
        {"vendor_a": {"name": "A"}, "use_case": "long enough here", "extra": 1},
    )
    assert errors


# --------------------------- redact_pii_fields -------------------------------


def test_pii_fields_redacted_top_level_and_nested():
    payload = {
        "vendor_a": {"name": "A", "notes": "reach me at pii-test@example.com"},
        "use_case": "long enough here",
        "logs_a": "user pii-test2@example.com logged in",
    }
    out, count = redact_pii_fields(_SCHEMA, payload)
    assert count >= 2
    assert "pii-test@example.com" not in out["vendor_a"]["notes"]
    assert "pii-test2@example.com" not in out["logs_a"]
    # Non-PII fields untouched.
    assert out["use_case"] == payload["use_case"]
    # The caller's payload was not mutated.
    assert payload["logs_a"] == "user pii-test2@example.com logged in"
    assert payload["vendor_a"]["notes"] == "reach me at pii-test@example.com"


def test_clean_payload_zero_redactions():
    payload = {"vendor_a": {"name": "A"}, "use_case": "long enough here"}
    out, count = redact_pii_fields(_SCHEMA, payload)
    assert count == 0
    assert out == payload


def test_non_string_pii_field_ignored():
    payload = {"vendor_a": {"name": "A"}, "use_case": "long enough here", "count": 7}
    out, count = redact_pii_fields(_SCHEMA, payload)
    assert out["count"] == 7
    assert count == 0


# --------------------------- extract_legacy_columns --------------------------


def test_full_vita_payload_lifted():
    payload = {
        "vendor_a": {"name": "A", "product": "P", "feature": "F", "observation": "O"},
        "vendor_b": {"name": "B"},
        "logs_a": "log line",
        "use_case": "u" * 50,
        "problem_statement": "p" * 20,
        "impact_statement": "impact",
        "severity": "high",
    }
    cols = extract_legacy_columns(payload)
    assert cols["vendor_a_name"] == "A"
    assert cols["vendor_a_product"] == "P"
    assert cols["vendor_b_name"] == "B"
    assert cols["logs_a"] == "log line"
    assert cols["severity"] == "high"
    assert "vendor_b_product" not in cols


def test_generic_payload_lifts_nothing():
    assert extract_legacy_columns({"description": "printer on fire", "urgency": 3}) == {}


def test_unknown_severity_not_lifted():
    """A toy agent's own severity vocabulary must not hit the runs table
    CHECK constraint — it stays in user_inputs only."""
    cols = extract_legacy_columns({"severity": "urgent"})
    assert "severity" not in cols


@pytest.mark.parametrize("vendor", ["a string", 42, None, ["list"]])
def test_non_dict_vendor_skipped(vendor):
    assert "vendor_a_name" not in extract_legacy_columns({"vendor_a": vendor})


# --------------------------- run title / approval summary (S2) --------------


def test_run_title_resolves_the_manifest_path_first():
    from app.services.intake import run_title

    schema = {"properties": {"logs": {"type": "string", "x-pii": True}, "summary": {"type": "string"}}}
    payload = {"logs": "L", "summary": "S", "incident": {"title": "  Pager\n  fired  "}}
    # Exactly as submitted (Codex P1 on PR #50): the column this fills is
    # the bundled agent's problem statement, so no whitespace normalisation.
    assert run_title("incident.title", schema, payload) == "  Pager\n  fired  "
    # A path that resolves to nothing usable falls through to the default.
    assert run_title("incident.missing", schema, payload) == "S"
    assert run_title("incident", schema, payload) == "S"  # not a string


def test_run_title_defaults_to_the_first_non_pii_string_input_in_schema_order():
    from app.services.intake import run_title

    schema = {
        "properties": {
            "logs": {"type": "string", "x-pii": True},
            "vendor": {"type": "object"},
            "count": {"type": "integer"},
            "headline": {"type": "string"},
            "details": {"type": "string"},
        }
    }
    assert run_title(None, schema, {"logs": "L", "headline": "H", "details": "D"}) == "H"
    # Empty strings do not qualify; the next string input does.
    assert run_title(None, schema, {"headline": "   ", "details": "D"}) == "D"
    # Nothing usable: no title, never an exception.
    assert run_title(None, schema, {"logs": "L", "count": 3}) is None
    assert run_title(None, None, {}) is None


def test_langgraph_example_runs_get_a_title_without_declaring_one():
    """Blueprint S2 Accept: the LangGraph example's runs show a title in
    the dashboard. Its manifest declares no ``ui.list.title_path``, so the
    default applies against its real input schema — the first string input
    not marked ``x-pii`` — which is the incident ``title`` field."""
    import json
    from pathlib import Path

    from app.agents.manifest import load_manifest
    from app.services.intake import run_title

    example = Path(__file__).resolve().parents[1] / "agents" / "_examples" / "langgraph_triage"
    manifest = load_manifest(example)
    assert manifest.ui.list.title_path is None
    schema = json.loads((example / "input_schema.json").read_text())
    payload = {
        "title": "Checkout returns 502 after the 14:05 deploy",
        "service": "checkout-api",
        "description": "Every request fails at the gateway.",
        "logs": "2026-09-11T14:05:01Z upstream timed out (jane@example.com)",
    }
    assert run_title(manifest.ui.list.title_path, schema, payload) == payload["title"]
    # With the title left blank the next non-PII string input is used; the
    # redacted log never becomes a title.
    assert run_title(None, schema, {**payload, "title": " "}) == "checkout-api"


def test_union_typed_strings_count_as_strings_for_titles_and_redaction():
    """Codex P2 on PR #50: ``"type": ["string", "null"]`` is valid JSON
    Schema for a nullable string. It must qualify as a title candidate and,
    marked ``x-pii``, must be redacted like a plain string."""
    from app.services.intake import run_title

    schema = {
        "properties": {
            "logs": {"type": ["string", "null"], "x-pii": True},
            "headline": {"type": ["string", "null"]},
            "count": {"type": ["integer", "null"]},
        }
    }
    assert run_title(None, schema, {"logs": "L", "headline": "Pager fired", "count": 1}) == "Pager fired"
    assert run_title(None, schema, {"logs": "L", "headline": None}) is None
    out, count = redact_pii_fields(
        schema, {"logs": "user pii-test3@example.com logged in", "headline": "h"}
    )
    assert count >= 1 and "pii-test3@example.com" not in out["logs"]
    assert out["headline"] == "h"


def test_approval_summary_path_then_first_string():
    from app.services.intake import approval_summary, first_string_in, resolve_path

    parked = {"n": 3, "refined": {"empty": "  ", "statement": "the statement"}, "list": ["z"]}
    assert resolve_path(parked, "refined.statement") == "the statement"
    assert resolve_path(parked, "refined.nope") is None
    assert resolve_path(parked, "n.x") is None
    assert first_string_in(parked) == "the statement"
    assert approval_summary("refined.statement", parked) == "the statement"
    assert approval_summary("refined.empty", parked) == "the statement"  # falls back
    assert approval_summary(None, parked) == "the statement"
    assert approval_summary(None, {"a": 1, "b": [2, {"c": None}]}) is None
    # Bounded depth: a pathological nesting does not recurse forever.
    deep: dict = {"s": "found"}
    for _ in range(50):
        deep = {"k": deep}
    assert first_string_in(deep) is None
