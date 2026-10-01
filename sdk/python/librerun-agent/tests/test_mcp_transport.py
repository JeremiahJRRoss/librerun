"""A transport failure leaves this client as ITS OWN exception.

`MCPClient._call_sync` caught only `urllib.error.HTTPError` — an answer
with a status. Everything else left as itself: a refused connection, a
DNS failure, a timeout, a 200 whose body is not JSON. So a typed client
raised `urllib.error.URLError` through its own API, and an agent told to
write `except CapabilityError` around `ctx.pii.redact` — which is what
the `container-python` template does, with a comment explaining why —
did not catch it.

THAT KILLED A CONFORMANT AGENT, measured rather than imagined. Driving
the rendered template with an advertised `run.mcp.url` that answered
nothing:

    passed: False
    checks: completed=fail, traceparent_optional=fail
    FAIL:   expected exactly one terminal event, completed; got ['failed']
            (URLError: <urlopen error [Errno 111] Connection refused>)

and with this fix, on the same probe:

    passed: True
    checks: completed=pass, traceparent_optional=pass, mcp=skip

`skip`, not `pass`: nothing reached the endpoint, and an unobserved
obligation is not a met one.

The distinction the new class carries is worth the class: "I could not
ask" is not "I asked and was refused", and an agent may reasonably
retry one and not the other.
"""
from __future__ import annotations

import json
import socket
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from librerun_agent import CapabilityError, CapabilityUnreachable
from librerun_agent._mcp import MCPClient


def _dead_url() -> str:
    """A URL on a port that was bound and released — nothing answers it."""
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return f"http://127.0.0.1:{port}/"


class _Serves(BaseHTTPRequestHandler):
    """Answers with whatever `body` and `status` the test sets."""

    body: bytes = b"{}"
    status: int = 200

    def log_message(self, *args):
        pass

    def do_POST(self):
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(type(self).body)))
        self.end_headers()
        self.wfile.write(type(self).body)


@pytest.fixture
def serving():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Serves)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/"
    server.shutdown()
    server.server_close()


def test_a_refused_connection_is_a_capability_error(): 
    client = MCPClient(_dead_url(), "tok")
    with pytest.raises(CapabilityUnreachable) as caught:
        client._call_sync("redact", {"text": "x"})
    # An agent writes `except CapabilityError`; this must be caught by it.
    assert isinstance(caught.value, CapabilityError)
    assert caught.value.code == -32000
    # The URL it was handed, named — it carries no credential, and the
    # bearer must not appear.
    assert "127.0.0.1" in str(caught.value)
    assert "tok" not in str(caught.value)


def test_the_raw_urlerror_this_replaced_is_not_a_capability_error():
    """THE DEFECT, stated as the thing that must no longer happen.

    Without the wrapping, `urlopen` raises `URLError`, which is not a
    `CapabilityError` — so the template's handler missed it. Asserting
    the two are unrelated classes keeps the reason for the wrapping on
    the record even when nobody remembers it.
    """
    assert not issubclass(urllib.error.URLError, CapabilityError)
    client = MCPClient(_dead_url(), "tok")
    try:
        client._call_sync("redact", {"text": "x"})
    except CapabilityError:
        pass
    else:  # pragma: no cover — the call cannot succeed against a dead port
        pytest.fail("a refused connection did not raise CapabilityError")


def test_a_dns_failure_is_the_same_class():
    client = MCPClient("http://no-such-host.invalid./", "tok", timeout=5.0)
    with pytest.raises(CapabilityUnreachable):
        client._call_sync("redact", {"text": "x"})


def test_a_two_hundred_that_is_not_json_is_unreachable_not_a_result(serving):
    _Serves.status, _Serves.body = 200, b"<html>a proxy said no</html>"
    client = MCPClient(serving, "tok")
    with pytest.raises(CapabilityUnreachable) as caught:
        client._call_sync("redact", {"text": "x"})
    assert "not JSON" in str(caught.value)


def test_a_two_hundred_carrying_json_that_is_not_an_object_is_refused(serving):
    _Serves.status, _Serves.body = 200, b'["not", "an", "object"]'
    client = MCPClient(serving, "tok")
    with pytest.raises(CapabilityUnreachable) as caught:
        client._call_sync("redact", {"text": "x"})
    assert "not an" in str(caught.value)


def test_an_http_error_carrying_a_jsonrpc_body_is_still_the_chassis_answer(serving):
    """The boundary: an ANSWER with a status is not a transport failure.

    The chassis refuses an unknown token with `-32001` at HTTP 401, and
    that must keep arriving as the code it carries rather than being
    swallowed into `CapabilityUnreachable` — otherwise the wrapping
    would have hidden every refusal it was meant to leave alone.
    """
    _Serves.status = 401
    _Serves.body = json.dumps({
        "jsonrpc": "2.0", "id": 1,
        "error": {"code": -32001, "message": "unknown or expired run token"},
    }).encode()
    client = MCPClient(serving, "tok")
    with pytest.raises(CapabilityError) as caught:
        client._call_sync("redact", {"text": "x"})
    assert not isinstance(caught.value, CapabilityUnreachable)
    assert caught.value.code == -32001


def test_an_http_error_with_no_usable_body_is_still_not_unreachable(serving):
    """…and one whose body is not JSON at all keeps its status code."""
    _Serves.status, _Serves.body = 502, b"<html>bad gateway</html>"
    client = MCPClient(serving, "tok")
    with pytest.raises(CapabilityError) as caught:
        client._call_sync("redact", {"text": "x"})
    assert not isinstance(caught.value, CapabilityUnreachable)
    assert caught.value.code == 502
