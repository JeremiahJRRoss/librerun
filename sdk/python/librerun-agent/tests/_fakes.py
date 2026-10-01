"""A scripted MCP server and a recording OTLP relay for the tests."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeMCP(BaseHTTPRequestHandler):
    """Answers tools/call from a script: ``responses[name]`` is a result
    payload, or ``("error", code, message)``."""

    responses: dict = {}
    calls: list = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        message = json.loads(self.rfile.read(length))
        name = message["params"]["name"]
        type(self).calls.append(
            {
                "name": name,
                "arguments": message["params"]["arguments"],
                "authorization": self.headers.get("Authorization"),
                "traceparent": self.headers.get("traceparent"),
            }
        )
        scripted = type(self).responses.get(name, {})
        if isinstance(scripted, tuple) and scripted[0] == "error":
            body = {"jsonrpc": "2.0", "id": message["id"], "error": {"code": scripted[1], "message": scripted[2]}}
        else:
            body = {
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": {"content": [{"type": "text", "text": json.dumps(scripted)}]},
            }
        raw = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class FakeRelay(BaseHTTPRequestHandler):
    """Records every OTLP request by bearer, decoded."""

    requests: list = []
    lock = threading.Lock()

    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        entry = {
            "path": self.path,
            "token": (self.headers.get("Authorization") or "").removeprefix("Bearer "),
            "content_type": self.headers.get("Content-Type"),
            "raw": raw,
        }
        if self.path.endswith("/v1/traces"):
            from opentelemetry.proto.collector.trace.v1 import trace_service_pb2

            req = trace_service_pb2.ExportTraceServiceRequest()
            req.ParseFromString(raw)
            entry["spans"] = [
                {"name": s.name, "trace_id": s.trace_id.hex(), "parent": s.parent_span_id.hex(),
                 "attributes": {kv.key: kv.value.string_value for kv in s.attributes}}
                for rs in req.resource_spans for ss in rs.scope_spans for s in ss.spans
            ]
        elif self.path.endswith("/v1/logs"):
            from opentelemetry.proto.collector.logs.v1 import logs_service_pb2

            req = logs_service_pb2.ExportLogsServiceRequest()
            req.ParseFromString(raw)
            entry["records"] = [
                {"body": r.body.string_value, "trace_id": r.trace_id.hex(),
                 "attributes": {kv.key: kv.value.string_value for kv in r.attributes}}
                for rl in req.resource_logs for sl in rl.scope_logs for r in sl.log_records
            ]
        with type(self).lock:
            type(self).requests.append(entry)
        self.send_response(202)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")


def start(handler_cls) -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"
