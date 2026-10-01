"""``python -m app.edge_control``: the ``edge-control`` service's HTTP server
(K blueprint T2; L42, D44 refined).

The standard library's server, on the ``edge`` network alone, at
:data:`PORT`. It answers two kinds of request:

- ``GET /status``: what the edge serves and needs, public material alone,
  for the backend's ``GET /api/v1/admin/tls``;
- the four certificate changes (:data:`~app.edge_control.edge.CHANGE_ROUTES`),
  from the edge's own address alone — ``EDGE_ADDRESS``, which compose
  derives from ``LIBRERUN_EDGE_NET`` as it derives the backend's
  ``FORWARDED_ALLOW_IPS`` — which the edge sends here only after
  ``forward_auth`` asked the backend whether the caller is a platform admin.

A key arrives in a change's body and nowhere else: no response carries one
— a malformed body's 422 names the check and never echoes the input — and
no log line does: a request is logged by its method, path and status.
"""
from __future__ import annotations

import ipaddress
import json
import os
import sys
import traceback
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from app.edge_control.edge import (
    BODY_LIMIT,
    CHANGE_ROUTES,
    Edge,
    EdgeUnavailable,
    Refused,
)

PORT = 8081
CONTROL = Path("/control")
CADDYFILE = Path("/etc/caddy/Caddyfile")
CHOSEN_BY = "X-Librerun-Chosen-By"


def log(event: str, **fields: Any) -> None:
    """One JSON line to stderr: never a body, a header's value or a key."""
    line = {"ts": datetime.now(timezone.utc).isoformat(), "event": event, **fields}
    print(json.dumps(line, sort_keys=True), file=sys.stderr, flush=True)


class _BodyError(Exception):
    def __init__(self, status: int, check: str, detail: str) -> None:
        super().__init__(detail)
        self.status, self.check, self.detail = status, check, detail


def make_handler(edge: Edge, edge_address: str) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "edge-control"
        sys_version = ""
        # A peer that stops sending mid-request frees its thread.
        timeout = 30

        def _path(self) -> str:
            return unquote(urlsplit(self.path).path).lower()

        def _send(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            if self.close_connection:
                self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

        def _refuse(self, status: int, check: str, detail: str) -> None:
            self._send(status, {"detail": detail, "check": check})

        def do_GET(self) -> None:  # noqa: N802 - the standard library's name
            if self._path() == "/status":
                self._send(200, edge.status())
            else:
                self._refuse(404, "route", "Not a route of edge-control.")

        def do_PUT(self) -> None:  # noqa: N802
            self._change("PUT")

        def do_DELETE(self) -> None:  # noqa: N802
            self._change("DELETE")

        def do_POST(self) -> None:  # noqa: N802
            self.close_connection = True
            self._refuse(405, "route", "edge-control takes PUT and DELETE for a change, GET for the status.")

        do_PATCH = do_POST

        def _body(self) -> bytes:
            """The body, read whole within the limit — or refused unread."""
            if self.headers.get("Transfer-Encoding"):
                raise _BodyError(411, "body", "A certificate change is sent with a Content-Length.")
            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                raise _BodyError(400, "body", "The Content-Length is not a number.") from None
            if length < 0 or length > BODY_LIMIT:
                raise _BodyError(413, "body_limit", f"The body is over {BODY_LIMIT // 1024} KiB.")
            return self.rfile.read(length) if length else b""

        @staticmethod
        def _json(raw: bytes) -> dict[str, Any]:
            """A JSON object — and a 422 that names the check, never the input."""
            try:
                data = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                raise Refused("body", "The body is not JSON.") from None
            if not isinstance(data, dict):
                raise Refused("body", "The body is not a JSON object.")
            return data

        def _change(self, method: str) -> None:
            name = CHANGE_ROUTES.get((method, self._path()))
            if name is None:
                self.close_connection = True
                self._refuse(404, "route", "Not a certificate change.")
                return
            if self.client_address[0] != edge_address:
                self.close_connection = True
                self._refuse(403, "origin", "A certificate change is taken from the edge alone.")
                return
            try:
                raw = self._body()
            except _BodyError as exc:
                self.close_connection = True
                self._refuse(exc.status, exc.check, exc.detail)
                return
            by = self.headers.get(CHOSEN_BY)
            try:
                if name in ("load_ca", "use_files"):
                    data = self._json(raw)
                    cert, key = data.get("certificate"), data.get("key")
                    if not isinstance(cert, str) or not isinstance(key, str):
                        raise Refused("body", "The body needs certificate and key, each a PEM string.")
                    status = getattr(edge, name)(cert, key, by)
                elif name == "use_acme":
                    status = edge.use_acme(self._json(raw).get("email"), by)
                else:
                    # Back to the environment takes no body; one sent is not read further.
                    status = edge.use_environment(by)
            except Refused as exc:
                self._refuse(422, exc.check, exc.detail)
            except EdgeUnavailable as exc:
                self._refuse(503, "edge_restart", f"{exc} Restart the edge; the previous files are in place.")
            except Exception as exc:  # noqa: BLE001 - logged by type and frames, never by value
                log("edge_control_error", error=type(exc).__name__, frames=traceback.format_tb(exc.__traceback__))
                self._refuse(500, "internal", "edge-control failed; its log names where.")
            else:
                log("edge_control_change", change=name, source=status.get("source"))
                self._send(200, status)

        def log_request(self, code: Any = "-", size: Any = "-") -> None:
            log("edge_control_request", method=self.command, path=urlsplit(self.path).path, status=str(code))

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - the standard library's name
            # send_error's own lines (a malformed request line, say) carry
            # nothing of a body; they are kept as the one field they are.
            log("edge_control_http", message=(format % args)[:200])

    return Handler


def serve(edge: Edge, edge_address: str, host: str = "0.0.0.0", port: int = PORT) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), make_handler(edge, edge_address))
    server.daemon_threads = True
    return server


def main() -> None:
    edge_address = os.environ.get("EDGE_ADDRESS", "").strip()
    try:
        ipaddress.ip_address(edge_address)
    except ValueError:
        log("edge_control_refused_to_start", reason="EDGE_ADDRESS is not one address", value=edge_address[:64])
        raise SystemExit(2) from None
    edge = Edge(CONTROL, CADDYFILE, edge_address)
    server = serve(edge, edge_address)
    log("edge_control_started", port=PORT, control=str(CONTROL), edge=edge_address)
    server.serve_forever()


if __name__ == "__main__":
    main()
