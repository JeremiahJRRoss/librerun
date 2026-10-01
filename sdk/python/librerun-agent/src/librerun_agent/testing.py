"""Helpers for tests: serve an app on an ephemeral port in a thread, and
install the stdout/stderr capture around a block."""
from __future__ import annotations

import contextlib
import socket
import sys
import threading
import time
from dataclasses import dataclass

from . import _capture


@dataclass
class ServerHandle:
    url: str
    _server: object
    _thread: threading.Thread

    def stop(self) -> None:
        self._server.should_exit = True  # type: ignore[attr-defined]
        self._thread.join(timeout=10)


def serve_in_thread(app, *, host: str = "127.0.0.1", startup_timeout: float = 15.0) -> ServerHandle:
    """Run ``app`` under uvicorn on a free port in a daemon thread and
    return its URL. Requires the ``uvicorn`` extra."""
    try:
        import uvicorn
    except ImportError as exc:
        raise RuntimeError("pip install 'librerun-agent[uvicorn]' for serve_in_thread()") from exc
    with socket.socket() as probe:
        probe.bind((host, 0))
        port = probe.getsockname()[1]
    config = uvicorn.Config(app, host=host, port=port, log_config=None, access_log=False, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="librerun-agent-test-server", daemon=True)
    thread.start()
    deadline = time.monotonic() + startup_timeout
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            raise RuntimeError("the test server did not start")
        time.sleep(0.02)
    return ServerHandle(f"http://{host}:{port}", server, thread)


@contextlib.contextmanager
def capture_stdio():
    """Install the SDK's stdout/stderr capture for the duration of a block.

    The server installs it once at startup, but a test runner owns
    ``sys.stdout`` and reassigns it between tests — pytest does — which
    leaves the writer the server installed no longer reachable, and a
    ``print()`` inside a handler goes to the runner's console instead of
    becoming a record of the invocation. A test asserting on captured
    output wraps the invocation in this instead, and gets the runner's
    own streams back afterwards, whatever they were.
    """
    saved = (sys.stdout, sys.stderr)
    _capture.uninstall_stdio_capture()
    _capture.install_stdio_capture()
    try:
        yield
    finally:
        _capture.uninstall_stdio_capture()
        sys.stdout, sys.stderr = saved


__all__ = ["ServerHandle", "capture_stdio", "serve_in_thread"]
