"""stdout / stderr capture per invocation, and the context that follows
work into threads and executor jobs — without the otel extra, a captured
line is a Run Contract ``log`` event of its invocation."""
from __future__ import annotations

import asyncio
import concurrent.futures
import io
import contextvars
import sys
import threading

import pytest

from librerun_agent import _capture
from librerun_agent._context import CURRENT, Invocation


def _invocation(name: str) -> Invocation:
    return Invocation(id=name, token=f"tok-{name}", run_id=f"run-{name}", tenant_id=None, phase="p", input={},
                      prior_output=None, user_edits=None, rerun=False, deadline_seconds=None, mcp_url=None,
                      traceparent=None, tracestate=None, trace_id=None)


@pytest.fixture
def captured():
    original_out, original_err = sys.stdout, sys.stderr
    _capture.install_thread_context()
    _capture.install_stdio_capture()
    try:
        yield
    finally:
        _capture.uninstall_stdio_capture()
        _capture.uninstall_thread_context()
        assert sys.stdout is original_out and sys.stderr is original_err


def _lines(inv: Invocation) -> list[str]:
    return [d["message"] for e, d in inv.events if e == "log"]


def test_two_overlapping_invocations_print_into_their_own_events(captured):
    first, second = _invocation("first"), _invocation("second")
    barrier = threading.Barrier(2)

    def work(inv: Invocation, text: str) -> None:
        CURRENT.set(inv)
        barrier.wait()
        for i in range(20):
            print(f"{text} {i}")
        sys.stderr.write(f"{text} err\n")

    t1 = threading.Thread(target=lambda: contextvars.copy_context().run(work, first, "one"))
    t2 = threading.Thread(target=lambda: contextvars.copy_context().run(work, second, "two"))
    t1.start(); t2.start(); t1.join(); t2.join()
    assert _lines(first) == [f"one {i}" for i in range(20)] + ["one err"]
    assert _lines(second) == [f"two {i}" for i in range(20)] + ["two err"]
    assert [d["level"] for e, d in first.events if e == "log"][-1] == "warning"


def test_a_print_outside_any_invocation_is_dropped(captured):
    before = list(CURRENT.get().events) if CURRENT.get() else None
    print("nobody owns this line")
    assert before is None
    assert isinstance(sys.stdout, _capture.CapturingWriter)


def test_threads_and_executor_jobs_land_under_the_invocation_that_started_or_submitted_them(captured):
    first, second = _invocation("first"), _invocation("second")
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)

    def in_first() -> None:
        CURRENT.set(first)
        t = threading.Thread(target=lambda: print("from a spawned thread"))
        t.start(); t.join()
        pool.submit(lambda: print("job from first")).result()  # creates the worker

    def in_second() -> None:
        CURRENT.set(second)
        fut = pool.submit(lambda: print("job from second"))
        fut.add_done_callback(lambda f: print("callback from second"))
        fut.result()

    contextvars.copy_context().run(in_first)
    contextvars.copy_context().run(in_second)
    pool.submit(lambda: print("job outside any run")).result()
    pool.shutdown(wait=True)
    assert _lines(first) == ["from a spawned thread", "job from first"]
    assert _lines(second) == ["job from second", "callback from second"]


def test_logging_lines_inside_an_invocation_become_log_events(captured):
    import logging

    inv = _invocation("logs")
    root = logging.getLogger()
    handler = _capture.InvocationLogHandler()
    root.addHandler(handler)
    try:
        def go():
            CURRENT.set(inv)
            logging.getLogger("agent.module").warning("careful %s", 42)
        contextvars.copy_context().run(go)
        logging.getLogger("agent.module").warning("outside")
    finally:
        root.removeHandler(handler)
    assert [(d["level"], d["message"], d["stream"]) for e, d in inv.events if e == "log"] == [
        ("warning", "careful 42", "logging:agent.module")
    ]


def test_the_server_flushes_a_partial_line_at_the_end_of_the_invocation(captured):
    from librerun_agent import serve
    from librerun_agent.testing import serve_in_thread
    import httpx, json

    async def handler(ctx):
        print("no newline at the end", end="")
        return {"ok": True}

    app = serve(handler, capture_stdio=True)
    handle = serve_in_thread(app)
    try:
        with httpx.Client(timeout=10) as client:
            r = client.post(f"{handle.url}/v1/runs", json={"contract": "v1", "phase": "p", "run": {"id": "r"}, "input": {}},
                            headers={"Authorization": "Bearer t"})
            invocation_id = r.json()["invocation_id"]
            with client.stream("GET", f"{handle.url}/v1/runs/{invocation_id}/events", headers={"Authorization": "Bearer t"}) as s:
                text = "".join(s.iter_text())
        assert "no newline at the end" in text
        assert text.index("no newline at the end") < text.index("event: completed")
    finally:
        handle.stop()


def test_capture_stdio_reinstalls_over_a_runners_streams_and_restores_them():
    """``testing.capture_stdio`` is for the runner problem.

    A test runner owns ``sys.stdout`` and reassigns it between tests —
    pytest does — so the writer the server installed at startup stops
    being what ``print()`` reaches, and an agent's test asserting on
    captured output silently stops testing anything. The helper installs
    over whatever the runner has now, and puts the runner's streams back
    exactly, so the next test's output is the runner's again.
    """
    import sys

    from librerun_agent import _capture
    from librerun_agent.testing import capture_stdio

    class _RunnerStream(io.StringIO):
        pass

    runner_out, runner_err = _RunnerStream(), _RunnerStream()
    saved = (sys.stdout, sys.stderr)
    sys.stdout, sys.stderr = runner_out, runner_err
    try:
        # Simulate the server having installed a writer over an older
        # stream that the runner has since replaced.
        _capture.install_stdio_capture()
        sys.stdout, sys.stderr = runner_out, runner_err

        with capture_stdio():
            assert isinstance(sys.stdout, _capture.CapturingWriter)
            assert isinstance(sys.stderr, _capture.CapturingWriter)
            print("swallowed: no invocation is bound")

        assert sys.stdout is runner_out and sys.stderr is runner_err
        assert runner_out.getvalue() == "", "a line with no invocation must be dropped"
        assert not _capture.capture_installed()
    finally:
        _capture.uninstall_stdio_capture()
        sys.stdout, sys.stderr = saved


def test_a_partial_line_is_flushed_while_the_span_is_still_open():
    """The flush has to happen inside the invocation's span.

    A handler that ends on ``print(..., end="")`` leaves a partial line.
    Flushed after the span exits, the record it becomes carries no trace
    context — the SDK would send it under the run token with an empty
    trace id, the relay would refuse the whole request `trace_mismatch`,
    and every other record batched with it would go down with it.
    """
    import inspect

    from librerun_agent import _server

    source = inspect.getsource(_server.RunContractApp._run)
    body, _, after = source.partition("_capture.flush_invocation_output(invocation)")
    assert "with _otel.invocation_span(invocation):" in body, (
        "the flush happens before the span is entered"
    )
    # The flush's own line must be indented deeper than the `with`, i.e.
    # still inside it.
    with_indent = next(
        len(line) - len(line.lstrip())
        for line in body.splitlines()
        if line.strip().startswith("with _otel.invocation_span")
    )
    flush_line = body.splitlines()[-1]
    assert len(flush_line) - len(flush_line.lstrip()) > with_indent, (
        "the flush is outside the invocation span"
    )
