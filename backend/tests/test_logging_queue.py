"""The queue-only logging pipeline (blueprint S4; gaps H9, H10), driven
through the real ``configure_logging`` with ``LOG_QUEUE_ONLY`` on:

- every record is walked on the listener thread before the JSONL sink
  sees it — the message, a structlog extra, a rendered traceback;
- a record carrying a flagged identifier or number is dropped for one
  platform-plane warning naming the logger and the count, never the text;
- the stdlib's own numbers are never checked (a Luhn-valid thread id),
  and every stdlib LogRecord attribute is classified (totality);
- no registration path attaches a handler that receives records:
  ``addHandler`` is refused with ``RuntimeError``, a non-incremental
  ``dictConfig``, an assignment to ``logger.handlers`` and a
  ``removeHandler`` of the marker each leave a record logged afterwards
  reaching the sink walked while the stray's own stream stays empty;
- the boot check fails on a stray handler attached before the patch,
  and ``enforce`` (the per-invocation belt) removes one attached after;
- ``print()`` and ``sys.stderr`` writes become records of the caller's
  plane; ``opentelemetry.*`` records reach the debug sink under
  ``OTEL_DEBUG`` walked;
- a thread started inside an invocation inherits its context, an
  executor work item runs in its submitter's context (done callbacks
  included) whichever worker serves it, and a pool worker carries none.
"""
from __future__ import annotations

import concurrent.futures
import io
import json
import logging
import logging.config
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest
import structlog

from app import logging_queue
from app.logging_context import log_context
from app.observability import walkers

FIXTURE_EMAIL = "pii.fixture@example.com"
CARD = 4111111111111111
LUHN_THREAD_ID = 139000000000384


def _records(path: Path) -> list[dict]:
    logging_queue.drain()
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


@pytest.fixture
def queue_log(tmp_path, monkeypatch):
    """The real queue-only pipeline against a temp JSONL file. Restores
    the suite's synchronous pipeline afterwards."""
    path = tmp_path / "backend.jsonl"
    monkeypatch.setenv("LOG_FILE_PATH", str(path))
    monkeypatch.setenv("LOG_STDERR_ENABLED", "false")
    monkeypatch.setenv("LOG_QUEUE_ONLY", "true")
    monkeypatch.setenv("OTEL_DEBUG", "false")
    from app import logging_config as _lc
    from app.config import Settings

    settings = Settings()
    monkeypatch.setattr("app.config.settings", settings)
    monkeypatch.setattr(_lc, "settings", settings)
    monkeypatch.setattr(_lc, "_CONFIGURED", False)
    monkeypatch.setattr(logging_queue, "DEFAULT_TOLERATED_MODULES", ("_pytest",))
    _lc.configure_logging()
    assert logging_queue.installed()
    yield path
    logging_queue.uninstall()
    monkeypatch.setattr(_lc, "_CONFIGURED", False)
    monkeypatch.setenv("LOG_QUEUE_ONLY", "false")
    restored = Settings()
    monkeypatch.setattr("app.config.settings", restored)
    monkeypatch.setattr(_lc, "settings", restored)
    _lc.configure_logging()


def _by_event(records: list[dict]) -> dict[str, dict]:
    return {r.get("event"): r for r in records}


# ------------------------------------------------------------- the walk --


def test_stdlib_and_structlog_records_are_walked_before_the_sink(queue_log):
    logging.getLogger("agents.demo").info("stdlib says hi to %s", FIXTURE_EMAIL)
    structlog.get_logger("agents.demo").info("native", contact=FIXTURE_EMAIL, count=3)
    records = _records(queue_log)
    text = json.dumps(records)
    assert FIXTURE_EMAIL not in text
    stdlib = next(r for r in records if r["event"].startswith("stdlib says"))
    assert stdlib["event"] == "stdlib says hi to [REDACTED_EMAIL_ADDRESS_1]"
    native = _by_event(records)["native"]
    assert native["contact"] == "[REDACTED_EMAIL_ADDRESS_1]" and native["count"] == 3
    assert native["librerun_scope"] == "platform"


def test_a_rendered_traceback_is_walked_in_jsonl(queue_log):
    try:
        raise RuntimeError(f"boom for {FIXTURE_EMAIL}")
    except RuntimeError:
        logging.getLogger("agents.demo").exception("stdlib failed %s", FIXTURE_EMAIL)
        structlog.get_logger("agents.demo").exception("native failed")
    records = _records(queue_log)
    text = json.dumps(records)
    assert FIXTURE_EMAIL not in text
    stdlib = next(r for r in records if r["event"].startswith("stdlib failed"))
    assert "Traceback" in stdlib["exception"] and "[REDACTED_EMAIL_ADDRESS_1]" in stdlib["exception"]
    native = _by_event(records)["native failed"]
    assert "RuntimeError: boom for [REDACTED_EMAIL_ADDRESS_1]" in native["exception"]


def test_a_flagged_extra_drops_the_record_for_one_warning_naming_the_logger(queue_log):
    logging.getLogger("agents.demo").info("with card", extra={"card": CARD})
    structlog.get_logger("agents.demo").info("with key", **{FIXTURE_EMAIL: 1})
    records = _records(queue_log)
    events = _by_event(records)
    assert "with card" not in events and "with key" not in events
    drops = [r for r in records if r["event"] == "log_record_dropped"]
    assert len(drops) == 2
    assert {d["logger_name"] for d in drops} == {"agents.demo"}
    assert all(d["count"] == 1 for d in drops)
    text = json.dumps(records)
    assert FIXTURE_EMAIL not in text and str(CARD) not in text
    assert logging_queue.dropped_count() == 2


def test_the_stdlibs_numbers_are_never_checked_and_every_attribute_is_placed():
    record = logging.LogRecord("agents.demo", logging.INFO, "p.py", 7, "clean", None, None)
    record.thread = LUHN_THREAD_ID
    record.process = 2125551234  # a valid US number as a pid: metadata
    record.lineno = 4111111111111111
    walkers.walk_log_record(record)  # no RecordRefused
    assert record.thread == LUHN_THREAD_ID
    unplaced = set(logging.LogRecord("n", 10, "p", 1, "m", None, None).__dict__) - set(
        walkers.STDLIB_RECORD_ATTRIBUTES
    )
    assert unplaced == set()


def test_a_clean_record_from_a_luhn_valid_thread_is_delivered(queue_log, monkeypatch):
    monkeypatch.setattr(threading, "get_ident", lambda: LUHN_THREAD_ID)
    logging.getLogger("agents.demo").info("clean line")
    records = _by_event(_records(queue_log))
    assert "clean line" in records
    assert records["clean line"]["librerun_scope"] == "platform"


# ------------------------------------------------------- queue only ------


def test_add_handler_is_refused_inside_an_invocation_and_ignored_outside(queue_log):
    stream = io.StringIO()
    stray = logging.StreamHandler(stream)
    with log_context(agent_id="langgraph-example", run_id="run-1"):
        with pytest.raises(RuntimeError, match="queue-only"):
            logging.getLogger("agents.demo").addHandler(stray)
    # A library attaching its handler at import (outside any run) must
    # not fail — it attaches nothing and is reported instead.
    logging.getLogger("spacy_like").addHandler(stray)
    assert logging.getLogger("spacy_like").handlers == []
    logging.getLogger("agents.demo").info("still walked %s", FIXTURE_EMAIL)
    records = _records(queue_log)
    events = _by_event(records)
    assert events["logging_handler_refused"]["logger_name"] == "agents.demo"
    assert events["logging_handler_ignored"]["logger_name"] == "spacy_like"
    assert "still walked [REDACTED_EMAIL_ADDRESS_1]" in events
    assert stream.getvalue() == ""


def test_dict_config_handlers_assignment_and_remove_handler_bypass_nothing(queue_log):
    stray_stream = io.StringIO()

    class Stray(logging.StreamHandler):
        pass

    # A non-incremental dictConfig (from agent code at import, say): it
    # removes existing handlers, shuts handlers down and registers its
    # own — and dispatch consults none of that.
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "handlers": {"stray": {"()": lambda: Stray(stray_stream)}},
            "root": {"handlers": ["stray"], "level": "INFO"},
        }
    )
    logging.getLogger("agents.demo").info("after dictConfig %s", FIXTURE_EMAIL)

    root = logging.getLogger()
    root.handlers = [Stray(stray_stream)]  # an assignment reaches the list...
    logging.getLogger("agents.demo").info("after assignment %s", FIXTURE_EMAIL)
    root.handlers = []  # ...and so does removing the marker
    logging.getLogger("agents.demo").info("after removal %s", FIXTURE_EMAIL)

    records = _records(queue_log)
    events = [r["event"] for r in records]
    for prefix in ("after dictConfig", "after assignment", "after removal"):
        (line,) = [e for e in events if e.startswith(prefix)]
        assert line.endswith("[REDACTED_EMAIL_ADDRESS_1]")
    assert stray_stream.getvalue() == ""
    assert FIXTURE_EMAIL not in json.dumps(records)
    assert any(r["event"] == "logging_handler_ignored" for r in records)


def test_the_boot_check_supersedes_a_stray_attached_before_the_patch(monkeypatch):
    """A handler attached before the patch is found by the boot check,
    removed and reported; a record logged afterwards reaches the sink
    walked while the stray's own stream stays empty."""
    stray_stream = io.StringIO()
    stray = logging.StreamHandler(stray_stream)
    logging.getLogger("agents.stray").addHandler(stray)
    sink_stream = io.StringIO()
    sink = logging.StreamHandler(sink_stream)
    monkeypatch.setattr(logging_queue, "DEFAULT_TOLERATED_MODULES", ("_pytest",))
    try:
        superseded = logging_queue.install(root_sinks=[sink])
        assert ("agents.stray", stray) in superseded
        assert logging.getLogger("agents.stray").handlers == []
        logging_queue.check_strict()  # nothing left that bypasses the walk
        logging.getLogger("agents.stray").warning("after install %s", FIXTURE_EMAIL)
        logging_queue.drain()
    finally:
        logging_queue.uninstall()
        logging.getLogger("agents.stray").handlers = []
    assert stray_stream.getvalue() == ""
    assert "after install [REDACTED_EMAIL_ADDRESS_1]" in sink_stream.getvalue()
    assert FIXTURE_EMAIL not in sink_stream.getvalue()
    assert "logging_handler_superseded" in sink_stream.getvalue()


def test_enforce_removes_a_stray_attached_after_the_patch_and_says_so(queue_log):
    stray = logging.StreamHandler(io.StringIO())
    logging.getLogger("agents.late").handlers.append(stray)  # around the guard
    assert logging_queue.enforce() == 1
    assert logging.getLogger("agents.late").handlers == []
    records = _by_event(_records(queue_log))
    assert records["logging_stray_handler_removed"]["logger_name"] == "agents.late"
    assert logging_queue.enforce() == 0


def test_enforce_is_a_no_op_when_the_pipeline_is_off():
    assert not logging_queue.installed()
    assert logging_queue.enforce() == 0


# ------------------------------------------------------- stdio capture --


def test_print_and_stderr_writes_become_records_of_the_callers_plane(queue_log):
    logging_queue.install_stdio_capture(fd_belt=False)
    try:
        with log_context(agent_id="echo-v1", run_id="run-1"):
            print(f"hello {FIXTURE_EMAIL}")
            sys.stderr.write(f"warn {FIXTURE_EMAIL}\n")
        print("outside any run")
    finally:
        logging_queue._restore_stdio(logging_queue._STATE.stdio)
        logging_queue._STATE.stdio = None
    records = _by_event(_records(queue_log))
    hello = records["hello [REDACTED_EMAIL_ADDRESS_1]"]
    assert hello["logger"] == "stdout" and hello["level"] == "info"
    assert hello["librerun_scope"] == "run" and hello["run_id"] == "run-1"
    warn = records["warn [REDACTED_EMAIL_ADDRESS_1]"]
    assert warn["logger"] == "stderr" and warn["level"] == "warning"
    assert warn["agent_id"] == "echo-v1"
    assert records["outside any run"]["librerun_scope"] == "platform"
    assert FIXTURE_EMAIL not in json.dumps(list(records.values()))


def test_the_descriptor_belt_walks_a_raw_write_in_a_subprocess(tmp_path):
    """A C-level write to descriptor 2 carries no context: it reaches the
    JSONL sink on the platform plane, walked; the process's own stderr
    (the stderr sink, JSON) never shows the fixture."""
    path = tmp_path / "belt.jsonl"
    script = f"""
import os, sys, time
os.environ.update(LOG_FILE_PATH={str(path)!r}, LOG_STDERR_ENABLED="true", LOG_QUEUE_ONLY="true",
                  APP_ENV="production", OTEL_DEBUG="false")
from app.logging_config import configure_logging, install_process_capture
configure_logging()
install_process_capture()
os.write(2, b"raw stderr {FIXTURE_EMAIL}\\n")
os.write(1, b"raw stdout {FIXTURE_EMAIL}\\n")
print("python stdout {FIXTURE_EMAIL}")
sys.stderr.write("python stderr {FIXTURE_EMAIL}\\n")
time.sleep(0.5)
from app import logging_queue
logging_queue.drain()
logging_queue.uninstall()
"""
    env = dict(os.environ)
    env.pop("LOG_QUEUE_ONLY", None)
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(Path(__file__).resolve().parents[1]),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    records = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    events = {r["event"]: r for r in records}
    assert events["raw stderr [REDACTED_EMAIL_ADDRESS_1]"]["logger"] == "fd.stderr"
    assert events["raw stdout [REDACTED_EMAIL_ADDRESS_1]"]["logger"] == "fd.stdout"
    assert events["python stdout [REDACTED_EMAIL_ADDRESS_1]"]["logger"] == "stdout"
    assert events["python stderr [REDACTED_EMAIL_ADDRESS_1]"]["logger"] == "stderr"
    assert all(events[e]["librerun_scope"] == "platform" for e in events)
    assert FIXTURE_EMAIL not in path.read_text()
    assert FIXTURE_EMAIL not in proc.stderr and FIXTURE_EMAIL not in proc.stdout
    assert "[REDACTED_EMAIL_ADDRESS_1]" in proc.stderr  # the stderr sink, walked


# ------------------------------------------------------- OTEL_DEBUG -----


def test_opentelemetry_debug_records_reach_the_debug_sink_walked(tmp_path, monkeypatch):
    path = tmp_path / "debug.jsonl"
    monkeypatch.setenv("LOG_FILE_PATH", str(path))
    monkeypatch.setenv("LOG_STDERR_ENABLED", "false")
    monkeypatch.setenv("LOG_QUEUE_ONLY", "true")
    monkeypatch.setenv("OTEL_DEBUG", "true")
    monkeypatch.setenv("LOG_LEVEL", "INFO")
    from app import logging_config as _lc
    from app.config import Settings

    settings = Settings()
    monkeypatch.setattr("app.config.settings", settings)
    monkeypatch.setattr(_lc, "settings", settings)
    monkeypatch.setattr(_lc, "_CONFIGURED", False)
    monkeypatch.setattr(logging_queue, "DEFAULT_TOLERATED_MODULES", ("_pytest",))
    _lc.configure_logging()
    try:
        logging.getLogger("opentelemetry.exporter.otlp").debug("exporter says %s", FIXTURE_EMAIL)
        logging.getLogger("agents.demo").debug("never at INFO")
        records = _by_event(_records(path))
    finally:
        logging_queue.uninstall()
        monkeypatch.setattr(_lc, "_CONFIGURED", False)
        monkeypatch.setenv("LOG_QUEUE_ONLY", "false")
        monkeypatch.setenv("OTEL_DEBUG", "false")
        restored = Settings()
        monkeypatch.setattr("app.config.settings", restored)
        monkeypatch.setattr(_lc, "settings", restored)
        _lc.configure_logging()
    assert records["exporter says [REDACTED_EMAIL_ADDRESS_1]"]["level"] == "debug"
    assert "never at INFO" not in records


# ------------------------------------------------ thread and executor ---


def test_threads_inherit_the_context_and_executor_items_run_in_their_submitters(queue_log):
    logging_queue.install_stdio_capture(fd_belt=False)
    logging_queue.install_thread_context()
    try:
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        with log_context(agent_id="first-v1", run_id="run-first"):
            t = threading.Thread(target=lambda: print("from a spawned thread"))
            t.start()
            t.join()
            pool.submit(lambda: print("job from first")).result()  # creates the worker
        with log_context(agent_id="second-v1", run_id="run-second"):
            fut = pool.submit(lambda: print("job from second"))
            fut.add_done_callback(lambda f: print("callback from second"))
            fut.result()
        pool.submit(lambda: print("job outside any run")).result()
        pool.shutdown(wait=True)
        outside = threading.Thread(target=lambda: print("thread outside any run"))
        outside.start()
        outside.join()
    finally:
        logging_queue.uninstall_thread_context()
        logging_queue._restore_stdio(logging_queue._STATE.stdio)
        logging_queue._STATE.stdio = None
    records = _by_event(_records(queue_log))
    assert records["from a spawned thread"]["run_id"] == "run-first"
    assert records["job from first"]["run_id"] == "run-first"
    assert records["job from second"]["run_id"] == "run-second"
    assert records["callback from second"]["run_id"] == "run-second"
    assert "run_id" not in records["job outside any run"]  # the worker carries none
    assert "run_id" not in records["thread outside any run"]
    assert records["job outside any run"]["librerun_scope"] == "platform"


def test_a_traceback_and_an_otel_debug_record_reach_stderr_walked(tmp_path):
    """The stderr leg, which the fixtures above cannot reach.

    Every assertion in this module runs with ``LOG_STDERR_ENABLED=false``
    — pytest's own stderr is pytest's — so the stderr sink, the one an
    operator actually reads in ``docker logs backend``, was asserted on
    nowhere. A walk that covered the JSONL file and not the console
    would look identical from in here and leak in production.

    So: a subprocess with both sinks on, a ``logging.exception()`` whose
    message and traceback carry the fixture, and an
    ``opentelemetry.*`` record under ``OTEL_DEBUG`` — all three must
    show the placeholder on stderr and in the file, and the address in
    neither.
    """
    path = tmp_path / "both.jsonl"
    script = f"""
import logging, os, sys, time
os.environ.update(LOG_FILE_PATH={str(path)!r}, LOG_STDERR_ENABLED="true", LOG_QUEUE_ONLY="true",
                  APP_ENV="production", OTEL_DEBUG="true", LOG_LEVEL="INFO")
from app.logging_config import configure_logging
configure_logging()
try:
    raise RuntimeError("boom for {FIXTURE_EMAIL}")
except RuntimeError:
    logging.getLogger("agents.demo").exception("agent failed {FIXTURE_EMAIL}")
logging.getLogger("opentelemetry.exporter.otlp").debug("exporter says {FIXTURE_EMAIL}")
time.sleep(0.5)
from app import logging_queue
logging_queue.drain()
logging_queue.uninstall()
"""
    env = dict(os.environ)
    env.pop("LOG_QUEUE_ONLY", None)
    env.pop("OTEL_DEBUG", None)
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(Path(__file__).resolve().parents[1]),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]

    # Neither stream nor the file may carry the address.
    assert FIXTURE_EMAIL not in proc.stderr, "the stderr sink is not walked"
    assert FIXTURE_EMAIL not in proc.stdout
    assert FIXTURE_EMAIL not in path.read_text()

    # The traceback: rendered text, on both sinks, with the placeholder.
    assert "agent failed [REDACTED_EMAIL_ADDRESS_1]" in proc.stderr
    assert "RuntimeError: boom for [REDACTED_EMAIL_ADDRESS_1]" in proc.stderr
    events = _by_event([json.loads(l) for l in path.read_text().splitlines() if l.strip()])
    failed = events["agent failed [REDACTED_EMAIL_ADDRESS_1]"]
    assert "Traceback" in failed["exception"]
    assert "RuntimeError: boom for [REDACTED_EMAIL_ADDRESS_1]" in failed["exception"]

    # The OTEL_DEBUG record: the debug sink AND stderr, both walked.
    assert "exporter says [REDACTED_EMAIL_ADDRESS_1]" in proc.stderr
    assert "exporter says [REDACTED_EMAIL_ADDRESS_1]" in events


def test_an_agent_cannot_exempt_a_value_by_choosing_a_reserved_field_name(queue_log):
    """A field name is not provenance.

    The exemption was "any key in this set is the chassis's". An
    explicitly passed kwarg beats the bound context in structlog, so
    agent code writing ``log.info("x", user_id=<the address>)`` had the
    value copied straight through to the sink. It is exempt now only
    when it still equals what the CHASSIS bound, captured on the record
    at emit time — so the bound value passes and the agent's does not.
    """
    with log_context(agent_id="echo-v1", run_id="run-1", tenant_id="t-1"):
        # The chassis's own bound ids: through, untouched.
        structlog.get_logger("agents.demo").info("bound ids")
        # The same names, values the caller chose: walked.
        structlog.get_logger("agents.demo").info(
            "agent chose the names",
            user_id=FIXTURE_EMAIL,
            run_id=FIXTURE_EMAIL,
            agent_id=FIXTURE_EMAIL,
            tenant_id=FIXTURE_EMAIL,
            request_id=FIXTURE_EMAIL,
            trace_id=FIXTURE_EMAIL,
            span_id=FIXTURE_EMAIL,
            path=FIXTURE_EMAIL,
            client_addr=FIXTURE_EMAIL,
        )
    records = _records(queue_log)
    assert FIXTURE_EMAIL not in json.dumps(records), "a reserved name exempted agent content"

    events = _by_event(records)
    bound = events["bound ids"]
    assert bound["agent_id"] == "echo-v1" and bound["run_id"] == "run-1"
    assert bound["tenant_id"] == "t-1"
    assert len(bound["trace_id"]) == 32 if bound.get("trace_id") else True

    chosen = events["agent chose the names"]
    for field in ("user_id", "run_id", "agent_id", "tenant_id", "request_id",
                  "trace_id", "span_id", "path", "client_addr"):
        assert chosen[field] == "[REDACTED_EMAIL_ADDRESS_1]", field


def test_the_chassis_own_ids_still_survive_the_walk(queue_log):
    """The other half. Walking a UUID destroys it — the recognizers read
    its digit run as a card number — so an exemption that was simply
    removed would be a different regression, not a fix."""
    run_id = "9a2b7c3d-0000-4000-8000-000000000002"
    with log_context(run_id=run_id, tenant_id="3f1c5a1e-0000-4000-8000-000000000001"):
        structlog.get_logger("agents.demo").info("real ids")
    record = _by_event(_records(queue_log))["real ids"]
    assert record["run_id"] == run_id
    assert record["tenant_id"] == "3f1c5a1e-0000-4000-8000-000000000001"


# A sealing key's fingerprint (K7, D34) whose hex the NER stage reads a
# nationality in, and which the base64-blob rule takes whole.
SEALING_FINGERPRINT = (
    "SHA256:985d432ee54816097cc3299de2774c2234c4da474fadd0be29d53f5e44d020ee"
)


def test_a_sealing_key_fingerprint_survives_the_walk(queue_log):
    """The gateway logs the fingerprint of the key it seals to, and the
    operator compares it with the one the admin page shows (D34). Walked,
    a digest never survives — the base64-blob rule takes any 40-character
    run — so the field is exempt, in exactly this shape: ``SHA256:`` and
    64 lowercase hex, which no address, number or name can be."""
    structlog.get_logger("gateway.provider_store").info(
        "gateway_sealing_key", fingerprint=SEALING_FINGERPRINT
    )
    record = _by_event(_records(queue_log))["gateway_sealing_key"]
    assert record["fingerprint"] == SEALING_FINGERPRINT


def test_the_fingerprint_name_exempts_nothing_but_a_digest(queue_log):
    """The name alone is not provenance: the same field with any other
    value is walked, and so is the digest under any other name."""
    structlog.get_logger("agents.demo").info(
        "near misses",
        fingerprint=FIXTURE_EMAIL,
    )
    structlog.get_logger("agents.demo").info(
        "a prefix before an address", fingerprint="SHA256:" + FIXTURE_EMAIL
    )
    structlog.get_logger("agents.demo").info(
        "a digest and more", fingerprint=SEALING_FINGERPRINT + " " + FIXTURE_EMAIL
    )
    structlog.get_logger("agents.demo").info(
        "upper-case hex", fingerprint="SHA256:" + SEALING_FINGERPRINT[7:].upper()
    )
    structlog.get_logger("agents.demo").info("another name", sha256=SEALING_FINGERPRINT)
    records = _records(queue_log)
    assert FIXTURE_EMAIL not in json.dumps(records), "the fingerprint name exempted an address"
    events = _by_event(records)
    assert events["near misses"]["fingerprint"] == "[REDACTED_EMAIL_ADDRESS_1]"
    assert events["upper-case hex"]["fingerprint"] != "SHA256:" + SEALING_FINGERPRINT[7:].upper()
    assert events["another name"]["sha256"] != SEALING_FINGERPRINT


def test_a_logger_name_does_not_exempt_the_access_fields(queue_log):
    """The access exemption follows the processor, not the name.

    ``reshape_uvicorn_access_record`` produces `client_addr`, `path` and
    the rest from uvicorn's own 5-tuple, and those are exempt because it
    wrote them. A logger name is freely selectable, though: agent code
    can call ``structlog.get_logger("uvicorn.access").info(...)`` and
    pass whatever it likes under those names. The record carries what
    the processor actually produced, so the impostor's fields are
    walked.
    """
    with log_context(agent_id="echo-v1", run_id="run-1"):
        structlog.get_logger("uvicorn.access").info(
            "http_request",
            client_addr=FIXTURE_EMAIL,
            path=FIXTURE_EMAIL,
            method=FIXTURE_EMAIL,
            http_version=FIXTURE_EMAIL,
        )
    records = _records(queue_log)
    assert FIXTURE_EMAIL not in json.dumps(records), "a logger name exempted agent content"
    event = _by_event(records)["http_request"]
    for field in ("client_addr", "path", "method", "http_version"):
        assert event[field] == "[REDACTED_EMAIL_ADDRESS_1]", field


def test_a_real_access_record_keeps_its_fields(queue_log):
    """The other half, through the real processor: a genuine uvicorn
    access record still comes out with its address and path intact —
    otherwise the fix above would be a different regression."""
    logging.getLogger("uvicorn.access").info(
        '%s - "%s %s HTTP/%s" %d',
        "127.0.0.1:52290", "GET", "/api/v1/runs?page=2", "1.1", 200,
    )
    event = _by_event(_records(queue_log))["http_request"]
    assert event["client_addr"] == "127.0.0.1:52290"
    assert event["path"] == "/api/v1/runs" and event["has_query"] is True
    assert event["method"] == "GET" and event["status_code"] == 200


def test_two_runs_partial_writes_never_join_into_one_line(queue_log):
    """A shared buffer mixes one tenant's text into another's telemetry.

    The pending text was buffered once for the process, so two runs
    writing without a newline interleaved in it and the thread that
    finally supplied one emitted the join as ITS run's line. Buffered
    per bound run, a run's fragments can only ever meet its own.
    """
    logging_queue.install_stdio_capture(fd_belt=False)
    try:
        with log_context(agent_id="a", run_id="run-A"):
            print("A-first ", end="")
        with log_context(agent_id="b", run_id="run-B"):
            print("B-first ", end="")
            print("B-second")           # B's newline: emits B's line only
        with log_context(agent_id="a", run_id="run-A"):
            print("A-second")           # A's newline: emits A's line only
    finally:
        logging_queue._restore_stdio(logging_queue._STATE.stdio)
        logging_queue._STATE.stdio = None

    events = _by_event(_records(queue_log))
    assert "B-first B-second" in events, sorted(events)
    assert "A-first A-second" in events, sorted(events)
    assert events["B-first B-second"]["run_id"] == "run-B"
    assert events["A-first A-second"]["run_id"] == "run-A"
    # And neither line carries the other's text.
    for event in ("B-first B-second", "A-first A-second"):
        other = "A-" if event.startswith("B") else "B-"
        assert other not in event
