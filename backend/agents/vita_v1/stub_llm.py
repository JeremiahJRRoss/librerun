"""Keyless per-step fixtures for the CI proving ground (blueprint B14).

The smoke workflow has to drive a REAL run — intake, both phases, the
approval gate, the report, the trace — on a runner with no provider
keys. Rather than mock the chassis (which would prove nothing about the
chassis), the chassis runs for real and only the provider boundary is
answered from fixtures: ``LLMService.call`` returns one of these dicts
when the ``llm`` capability reports stub mode.

The fixtures are deliberately shaped like *well-behaved* provider
output — they satisfy each step's schema so the normalizers pass them
through without drift — because the point of the smoke is to prove the
pipeline wiring, not to exercise the drift machinery (which has its own
unit tests).

Every text field says it is a fixture. A report generated this way is a
smoke artifact and must never be mistaken for an investigation.
"""
from __future__ import annotations

from typing import Any

STUB_MARKER = "[stub-llm fixture — no provider was called]"


def _citation(idx: int, vendor: str) -> dict[str, Any]:
    return {
        "id": idx,
        "title": f"{vendor} documentation excerpt {idx} {STUB_MARKER}",
        "url": f"https://example.invalid/{vendor.lower()}/doc-{idx}",
        "doc_type": "vendor_doc",
        "relevance_score": 0.5,
        "summary": f"Fixture citation {idx} for {vendor}.",
    }


# Keyed by pipeline step_id — the same ids agent.yaml's llm.steps[] declares.
_FIXTURES: dict[str, Any] = {
    "validate_and_classify_inputs": {
        "valid": True,
        "rejection_reason": None,
        "primary_vendor": "Vendor A",
        "secondary_vendor": "Vendor B",
        "integration_type": "sso",
        "error_signals": ["signature_validation_failed"],
        "input_quality_score": 0.9,
    },
    "generate_log_hints": {
        "hints": [f"Collect the identity provider's SSO logs. {STUB_MARKER}"],
    },
    "refine_problem_statement": {
        "refined_problem_statement": (
            f"Federated sign-in fails after a signing-certificate rotation. {STUB_MARKER}"
        ),
        "key_signals": ["signature validation failure", "certificate rotation"],
        "suspected_root_causes": ["stale signing certificate in the service provider"],
        "research_focus_areas": ["certificate rotation procedure", "metadata refresh"],
    },
    "construct_search_queries_vendor_a": {
        "web_queries": ["vendor a signing certificate rotation"],
        "vector_queries": ["certificate rotation runbook"],
        "domain_hints": ["example.invalid"],
    },
    "construct_search_queries_vendor_b": {
        "web_queries": ["vendor b signature validation failure"],
        "vector_queries": ["signature validation troubleshooting"],
        "domain_hints": ["example.invalid"],
    },
    "assess_skills": {
        "skills": [
            {
                "name": "Certificate lifecycle management",
                "description": f"Rotating and publishing signing certificates. {STUB_MARKER}",
                "relevance_weight": 0.9,
                "source": "llm_knowledge",
            },
            {
                "name": "Federated identity troubleshooting",
                "description": f"Reading SSO assertion failures. {STUB_MARKER}",
                "relevance_weight": 0.7,
                "source": "llm_knowledge",
            },
        ]
    },
    "generate_resolution_plan": {
        "mitigation": {
            "text": f"Temporarily restore the previous signing certificate. {STUB_MARKER}",
            "citations": [1],
        },
        "resolution": {
            "text": (
                "Publish the rotated certificate to the service provider and "
                f"refresh federation metadata. {STUB_MARKER}"
            ),
            "citations": [1, 2],
        },
        "avoidance": {
            "text": f"Automate metadata refresh before each rotation. {STUB_MARKER}",
            "citations": [2],
        },
        "works_cited": {
            "vendor_a": [_citation(1, "VendorA")],
            "vendor_b": [_citation(2, "VendorB")],
        },
    },
    "generate_followup_questions": {
        "questions": [
            {
                "question": f"When was the certificate last rotated? {STUB_MARKER}",
                "rationale": "Establishes the change window.",
                "expected_impact": "Confirms or rules out the suspected cause.",
            }
        ]
    },
}


def fixture_for(step_id: str) -> Any:
    """The canned response for ``step_id``.

    Unknown steps get an empty dict rather than a KeyError: a new step
    that nobody has written a fixture for should degrade to that step's
    normalizer-defined empty shape (and show up as drift), not crash the
    smoke run.
    """
    return _FIXTURES.get(step_id, {})


def has_fixture(step_id: str) -> bool:
    return step_id in _FIXTURES
