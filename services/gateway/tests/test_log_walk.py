"""The gateway walks every log record, whoever logged it.

The first version of this put a :class:`logging.Filter` on the root
logger. A logger's filters run for records emitted *on that logger*;
propagation reaches an ancestor's **handlers**, never its filters — so
that arrangement walked nothing the gateway actually logs. It also could
not have: ``structlog`` with no ``configure()`` writes to stdout itself
and produces no record at all.

Each case below is paired with the shape that shipped, so a regression
that reintroduces either one fails here rather than in a trace viewer.
"""
from __future__ import annotations

import json
import logging

import pytest
import structlog

from gateway import logs


class _Capture(logging.Handler):
    """The only handler on the root for the duration of a test."""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def texts(self) -> list[str]:
        """What a sink would render, not what the record holds: a
        formatter interpolates ``record.args`` downstream of everything
        here, so a walk that left them alone must still show up."""
        out = []
        for record in self.records:
            msg = record.msg
            out.append(
                json.dumps(msg, default=str)
                if isinstance(msg, dict)
                else record.getMessage()
            )
            if record.exc_text:
                out.append(record.exc_text)
        return out

    def blob(self) -> str:
        return "\n".join(self.texts())


@pytest.fixture
def pipeline():
    """Install the real pipeline over a capturing root, and put back
    whatever the rest of the suite had. Every global this touches —
    dispatch, root handlers and level, the structlog configuration — is
    process-wide, so none of it may leak out of a test."""
    saved_dispatch = logging.Logger.callHandlers
    saved_handlers = list(logging.getLogger().handlers)
    saved_level = logging.getLogger().level
    saved_structlog = structlog.get_config()
    saved_flags = (logs._walk_installed, logs._configured)

    logs._walk_installed = False
    logs._configured = False
    logging.Logger.callHandlers = saved_dispatch  # a clean base to patch
    logging.getLogger().handlers = []
    logs.configure()

    capture = _Capture()
    logging.getLogger().handlers = [capture]
    logging.getLogger().setLevel(logging.INFO)
    try:
        yield capture
    finally:
        logging.Logger.callHandlers = saved_dispatch
        logging.getLogger().handlers = saved_handlers
        logging.getLogger().setLevel(saved_level)
        structlog.configure(**saved_structlog)
        logs._walk_installed, logs._configured = saved_flags


NAME = "Dr. Maria Gonzalez"
REDACTED = "[REDACTED_PERSON_1]"


def test_a_structlog_line_from_a_gateway_module_is_walked(pipeline):
    """The gateway's own logging. Unconfigured structlog would print this
    straight to stdout and never produce a record at all."""
    structlog.get_logger("gateway.egress").info("called", operator=NAME)

    assert NAME not in pipeline.blob()
    assert REDACTED in pipeline.blob()


def test_uvicorns_own_handlers_are_superseded(pipeline):
    """Uvicorn attaches handlers and sets ``propagate: False`` before it
    imports the app. Those records are walked either way — the walk is
    upstream of every handler — but left alone they come out in
    uvicorn's plain format beside the gateway's JSON."""
    logs._configured = False
    for name in logs._UVICORN_LOGGERS:
        logger = logging.getLogger(name)
        logger.handlers = [logging.StreamHandler()]
        logger.propagate = False
    logs.configure()

    logging.getLogger("uvicorn.error").warning("serving for %s", NAME)

    for name in logs._UVICORN_LOGGERS:
        assert logging.getLogger(name).handlers == []
    # …and the record reached the one sink there is.
    assert REDACTED in pipeline.blob()


def test_a_descendant_logger_is_walked(pipeline):
    """The case a root-logger filter cannot reach: nothing in this
    process logs on the root, so a filter there sees nothing."""
    logging.getLogger("uvicorn.error").warning("serving for %s", NAME)

    assert NAME not in pipeline.blob()
    assert REDACTED in pipeline.blob()


def test_a_root_logger_filter_would_not_have_caught_the_descendant(pipeline):
    """The shipped defect, injected: with the walk expressed as a filter
    on the root, the same record arrives whole. If this ever stops
    failing to redact, logger filters have started being inherited and
    the test above is no longer proving anything."""
    logs.uninstall_walk()
    walked: list[str] = []

    class _AsAFilter(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            walked.append(record.name)
            return True

    logging.getLogger().addFilter(_AsAFilter())
    try:
        logging.getLogger("uvicorn.error").warning("serving for %s", NAME)
    finally:
        logging.getLogger().filters.clear()

    assert walked == []
    assert NAME in pipeline.blob()


def test_positional_args_are_walked(pipeline):
    """``log.warning("%s", name)`` leaves the name in ``record.args``,
    which no walk of the record's strings touches and which the
    formatter interpolates afterwards."""
    logging.getLogger("LiteLLM").warning("retrying for %s after %d tries", NAME, 3)

    blob = pipeline.blob()
    assert NAME not in blob
    assert REDACTED in blob
    assert "after 3 tries" in blob


def test_a_traceback_is_walked(pipeline):
    """``exc_info`` is a live exception, not a string: rendered after the
    walk, it reaches the sink whole."""
    try:
        raise ValueError(f"upstream said {NAME}")
    except ValueError:
        structlog.get_logger("gateway.egress").exception("call_failed")

    blob = pipeline.blob()
    assert "Traceback" in blob
    assert NAME not in blob
    assert REDACTED in blob


def test_a_refused_record_is_dropped(pipeline):
    """Fail closed. A number is not rewritable — there is no placeholder
    that is still the same number — so the walk refuses it and the whole
    record goes, for one warning that names the logger and never the
    text."""
    structlog.get_logger("gateway.egress").info("kept", account=4111111111111111)

    blob = pipeline.blob()
    assert "4111111111111111" not in blob
    assert "kept" not in blob
    assert "log_record_dropped" in blob
    assert "CREDIT_CARD" in blob
    assert "gateway.egress" in blob


def test_the_walk_does_not_feed_itself(pipeline):
    """Presidio logs on every analyze call. Walking that line would
    analyze again; the guard is what stops the process spiralling. The
    line still arrives — it is the redactor talking about recognizers."""
    inner = logging.getLogger("presidio-analyzer")
    inner.setLevel(logging.INFO)
    seen: list[str] = []

    original = logs.walkers.walk_log_record

    def _logs_while_walking(record):
        seen.append(record.name)
        if record.name != "presidio-analyzer":
            inner.info("recognizer loaded for %s", NAME)
        return original(record)

    logs.walkers.walk_log_record = _logs_while_walking
    try:
        structlog.get_logger("gateway.egress").info("outer")
    finally:
        logs.walkers.walk_log_record = original

    # The inner line was never handed to the walk, and both arrived.
    assert seen == ["gateway.egress"]
    assert "recognizer loaded" in pipeline.blob()
    assert "outer" in pipeline.blob()
