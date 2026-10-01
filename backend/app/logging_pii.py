"""PII redaction marker + structlog processor.

Operational logs must never carry customer data verbatim. Rather than
run every log field through Presidio (slow, and risks over-redacting
identifiers), call sites mark *only* user-sourced strings with
``user_content(...)``. The structlog processor below unwraps those
markers, runs them through the project's existing PII redactor, and
truncates to 4000 chars. All other fields pass through untouched.
"""
from __future__ import annotations

from typing import Any

from app.config import settings

MAX_FIELD_LEN = 4000
REDACTION_FAILED = "[REDACTION_FAILED]"


class _UserContent(str):
    """Marker subclass — behaves like ``str``, flagged for redaction in logs."""


def user_content(value: Any) -> _UserContent | None:
    """Wrap a value as user-originated content. ``None`` in → ``None`` out."""
    if value is None:
        return None
    return _UserContent(str(value))


def _redact_and_truncate(value: str) -> str:
    # Truncate *before* redacting so Presidio's NER stage does not chew
    # through megabytes of LLM response body on every log line.
    truncated = value[:MAX_FIELD_LEN]
    # Local import to avoid pulling Presidio at module import time.
    from app.services import pii_service

    redacted, _ = pii_service.redact(truncated)
    return redacted


def redact_user_content_processor(logger, method_name, event_dict):
    """Replace any ``_UserContent`` field with its redacted value.

    When ``settings.LOG_REDACT_PII`` is false, markers are still unwrapped
    to plain ``str`` so JSONRenderer serializes cleanly downstream, but no
    redaction runs.
    """
    redact = settings.LOG_REDACT_PII
    for key, value in list(event_dict.items()):
        if isinstance(value, _UserContent):
            if redact:
                try:
                    event_dict[key] = _redact_and_truncate(str(value))
                except Exception:
                    # Fail open: drop the field rather than block log emission
                    # or leak unredacted PII. Logging must never raise on
                    # account of the PII pipeline.
                    event_dict[key] = REDACTION_FAILED
            else:
                event_dict[key] = str(value)[:MAX_FIELD_LEN]
    return event_dict
