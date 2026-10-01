#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""A mock vendor intake for the obs-vendors contract tests (blueprint S7a).

Stdlib only, so it runs in any python image with nothing installed. Two
listeners, because the overlays split their legs that way:

    :9009  the LOG intake  — Datadog /api/v2/logs, Elasticsearch /_bulk,
                             Splunk HEC /services/collector/event
    :9010  the OTLP TRACE intake — /v1/traces, and Splunk's /v2/trace/otlp

Every request is journalled to a JSONL file, one line per request, with
the body base64-encoded so protobuf survives the round trip. It answers
each vendor's documented success shape so the sink in front of it
behaves as it would in production — a sink that retries forever against
a 500 would look identical to a sink that never sent anything.

WHAT THIS PROVES: that the bytes LibreRun puts on the wire have the
shape each vendor documents. It is not a vendor's acceptance of the
data. Only an account at that vendor can tell you that — see
docs/platform/Observability.md.
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_LOCK = threading.Lock()
_JOURNAL = None


def _record(path: str, headers, body: bytes) -> None:
    row = {
        "path": path,
        "headers": {k: v for k, v in headers.items()},
        "body_b64": base64.b64encode(body).decode(),
    }
    with _LOCK:
        _JOURNAL.write(json.dumps(row) + "\n")
        _JOURNAL.flush()


class Intake(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quieter than the default stderr spray
        sys.stderr.write(f"mock-intake {self.address_string()} {fmt % args}\n")

    def _reply(self, code: int, payload: bytes, content_type="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if payload:
            self.wfile.write(payload)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        # Elasticsearch's version probe: Vector's sink asks the cluster
        # who it is when api_version is `auto`. Answered so the overlay
        # works either way, pinned or probing.
        if path in ("/", ""):
            return self._reply(
                200,
                json.dumps(
                    {
                        "name": "librerun-mock",
                        "version": {"number": "8.13.0", "build_flavor": "default"},
                        "tagline": "You Know, for Search",
                    }
                ).encode(),
            )
        # Datadog's key check and Splunk's HEC health check, in the
        # shapes their sinks' healthchecks expect.
        if path.startswith("/api/v1/validate"):
            return self._reply(200, b'{"valid":true}')
        if path.startswith("/services/collector/health"):
            return self._reply(200, b'{"text":"HEC is healthy","code":17}')
        return self._reply(200, b"{}")

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        _record(path, self.headers, body)

        # Elasticsearch parses the bulk response and treats `errors` as
        # authoritative, so the item list has to match the action count
        # or the sink reports a partial failure and retries.
        if path.rstrip("/").endswith("/_bulk"):
            actions = max(1, len([ln for ln in body.split(b"\n") if ln.strip()]) // 2)
            items = [
                {"create": {"_index": "logs-librerun-default", "status": 201}}
                for _ in range(actions)
            ]
            return self._reply(
                200, json.dumps({"took": 1, "errors": False, "items": items}).encode()
            )

        if path.startswith("/services/collector"):
            return self._reply(200, b'{"text":"Success","code":0}')

        if path.startswith("/api/v2/logs"):
            # Datadog's logs intake answers 202 with an empty object.
            return self._reply(202, b"{}")

        # OTLP/HTTP: an empty ExportTraceServiceResponse is a zero-length
        # protobuf message, which is what a successful export looks like.
        if "otlp" in path or path.endswith("/v1/traces"):
            return self._reply(200, b"", content_type="application/x-protobuf")

        return self._reply(200, b"{}")

    def do_PUT(self):
        return self.do_POST()


def serve(port: int) -> threading.Thread:
    server = ThreadingHTTPServer(("0.0.0.0", port), Intake)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    sys.stderr.write(f"mock-intake listening on :{port}\n")
    sys.stderr.flush()
    return thread


def main() -> int:
    global _JOURNAL
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--journal", required=True, help="JSONL file, one line per request")
    p.add_argument("--log-port", type=int, default=9009)
    p.add_argument("--trace-port", type=int, default=9010)
    args = p.parse_args()

    # Line-buffered and appended: the CI job truncates it between
    # vendors, and a crash must not lose what already arrived.
    _JOURNAL = open(args.journal, "a", buffering=1)
    threads = [serve(args.log_port), serve(args.trace_port)]
    for t in threads:
        t.join()
    return 0


if __name__ == "__main__":
    sys.exit(main())
