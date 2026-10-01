"""Why a run ended ``error`` — the chassis's closed vocabulary (blueprint S7).

Two readers, two fields on the run row (migration 017):

* ``error_code`` is one of the constants below. It is what the CUSTOMER
  run page renders — through :func:`user_message`, a sentence the
  chassis wrote — and a value here never carries agent text. The page
  can therefore show a reason and offer "Run again" without the chassis
  deciding whether a given agent's failure text is safe to show, which
  it cannot decide: the Run Contract makes ``failed.error`` and ``log``
  lines operator-facing (``docs/authoring/Run_Contract_v1.md``), and an in-process
  agent's exception may quote anything.
* ``error_detail`` is that operator-facing text — redacted before it is
  stored (:func:`safe_detail`) and served on the ADMIN run view only.

The vocabulary is closed on purpose: a new kind of failure is a new
constant with a new sentence, added here, never a string a caller
invents at the site that fails.
"""
from __future__ import annotations

from typing import Final

# The backend process was restarted (or died) while this run's phase was
# in flight; the phase ran as a background task and died with it
# (gap H16). Marked at the next boot by ``agent_runner.reconcile_orphaned_runs``.
BACKEND_RESTARTED: Final = "backend_restarted"
# One invocation of a phase outran ``phases[].deadline_seconds`` /
# ``LIBRERUN_MAX_PHASE_SECONDS`` (blueprint S4).
DEADLINE_EXCEEDED: Final = "deadline_exceeded"
# The agent said so: a non-success result status, or a container's
# ``failed`` event.
AGENT_FAILED: Final = "agent_failed"
# The phase raised something the runner did not expect.
PHASE_FAILED: Final = "phase_failed"
# The agent's output carried personal data the boundary could not redact
# (``pii_in_output``), so nothing of it was stored.
OUTPUT_REFUSED: Final = "output_refused"
# The run's agent is not registered in this process.
AGENT_UNAVAILABLE: Final = "agent_unavailable"
# The run's phase cursor names a phase its manifest no longer declares.
PHASE_INVALID: Final = "phase_invalid"

# Longest ``error_detail`` the row keeps. The container runtime already
# caps ``failed.error`` at 2000 characters; the same bound applies to
# every other source so a stack trace cannot become a blob.
MAX_DETAIL_CHARS: Final = 2000

_MESSAGES: Final[dict[str, str]] = {
    BACKEND_RESTARTED: (
        "The platform restarted while this run was in the middle of a "
        "phase, so the phase could not finish. Run it again to start over."
    ),
    DEADLINE_EXCEEDED: (
        "A phase ran past its time budget and was stopped. Run it again; "
        "if it happens again, ask your administrator about the phase deadline."
    ),
    AGENT_FAILED: (
        "The agent reported a failure and could not finish this run."
    ),
    PHASE_FAILED: (
        "A phase of this run ended with an unexpected error."
    ),
    OUTPUT_REFUSED: (
        "The agent's output contained personal data the platform could not "
        "redact, so the run was stopped rather than stored."
    ),
    AGENT_UNAVAILABLE: (
        "The agent this run belongs to is not installed on this platform."
    ),
    PHASE_INVALID: (
        "This run's saved position does not match the agent's current "
        "phases, so it could not continue."
    ),
}

CODES: Final = frozenset(_MESSAGES)


def user_message(code: str | None) -> str | None:
    """The sentence the customer page shows for ``code`` — chassis-written,
    never agent text. ``None`` for no code, and for a code this build does
    not know (a row written by a newer build), so the page falls back to
    its generic line rather than printing the code."""
    if not code:
        return None
    return _MESSAGES.get(code)


def safe_detail(text: object) -> str | None:
    """``error_detail`` as it may be stored: a string, capped, and REDACTED
    through the run boundary — the same walk every other agent-influenced
    string takes before persistence. A detector that cannot walk it
    withholds the text rather than storing it unredacted, and no failure
    of the redaction may turn into a failure to mark the run at all,
    because this runs on the error path."""
    if text is None:
        return None
    from app.services import run_boundary

    raw = str(text)[:MAX_DETAIL_CHARS]
    try:
        return run_boundary.redact_text(
            raw, argument="error_detail", reason=run_boundary.REASON_OUTPUT
        )
    except run_boundary.PiiRefused as exc:
        return f"<withheld: {exc.reason}>"
    except Exception as exc:  # noqa: BLE001 — the error path must not raise
        return f"<withheld: {type(exc).__name__}>"
