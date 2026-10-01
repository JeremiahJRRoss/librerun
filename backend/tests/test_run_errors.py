"""Why a run ended ``error`` (blueprint S7): the chassis's closed
vocabulary, the sentence it writes for each code, and the detail that is
redacted before it may be stored.

The split is the point. The Run Contract makes an agent's ``failed.error``
and ``log`` lines operator-facing ("surfaced to operators (not end
users)"), and an in-process agent's exception may quote anything — so the
customer run page never sees either. It sees a CODE the chassis chose and
the SENTENCE the chassis wrote for it; the operator text goes to
``error_detail`` on the admin view, through the same redaction every
other agent-influenced string takes before persistence.
"""
from __future__ import annotations

import pytest

from app.services import run_boundary, run_errors

FIXTURE_EMAIL = "pii.fixture@example.com"


def test_every_code_has_a_sentence_and_the_sentence_is_the_chassis_own():
    assert run_errors.CODES == {
        "backend_restarted",
        "deadline_exceeded",
        "agent_failed",
        "phase_failed",
        "output_refused",
        "agent_unavailable",
        "phase_invalid",
    }
    for code in run_errors.CODES:
        message = run_errors.user_message(code)
        assert message and message[0].isupper() and message.endswith(".")
        # The sentence never carries the code itself: a customer page
        # printing "backend_restarted" would be printing an identifier.
        assert code not in message


def test_no_code_and_an_unknown_code_map_to_no_message():
    """A row written by a newer build may carry a code this one does not
    know; the page then falls back to its generic line rather than
    printing the code."""
    assert run_errors.user_message(None) is None
    assert run_errors.user_message("") is None
    assert run_errors.user_message("something_new") is None


def test_the_restart_code_says_to_run_again():
    assert "Run it again" in run_errors.user_message(run_errors.BACKEND_RESTARTED)


def test_safe_detail_redacts_before_it_may_be_stored():
    """The detail is the agent's text (or the exception quoting it), and
    it goes in a database column — so it takes the run boundary's walk.
    An address in it comes out as the placeholder, never the address."""
    out = run_errors.safe_detail(f"agent reported failure: mail {FIXTURE_EMAIL} bounced")
    assert out is not None
    assert FIXTURE_EMAIL not in out
    assert "REDACTED" in out
    assert out.startswith("agent reported failure: mail ")


def test_safe_detail_is_capped_and_none_stays_none():
    assert run_errors.safe_detail(None) is None
    out = run_errors.safe_detail("x" * (run_errors.MAX_DETAIL_CHARS + 500))
    assert out is not None and len(out) <= run_errors.MAX_DETAIL_CHARS


def test_safe_detail_withholds_rather_than_stores_when_the_walk_refuses(monkeypatch):
    """A detector that cannot walk the text (S4c: not ready) must not
    let the text through unredacted — and must not turn marking the run
    ``error`` into a failure of its own, because this runs on the error
    path. The reason is stored in the text's place."""

    def refuse(text, *, argument, reason):
        raise run_boundary.PiiRefused(
            "pii_detector_unavailable",
            argument,
            run_boundary.pii_service.Finding(
                path="$", kind="content", pii_type="detector", confidence=1.0, action="refused"
            ),
        )

    monkeypatch.setattr(run_boundary, "redact_text", refuse)
    out = run_errors.safe_detail(f"boom {FIXTURE_EMAIL}")
    assert out == "<withheld: pii_detector_unavailable>"
    assert FIXTURE_EMAIL not in out


def test_safe_detail_survives_any_other_failure_of_the_walk(monkeypatch):
    def explode(text, *, argument, reason):
        raise RuntimeError("the walker fell over")

    monkeypatch.setattr(run_boundary, "redact_text", explode)
    assert run_errors.safe_detail("anything") == "<withheld: RuntimeError>"
