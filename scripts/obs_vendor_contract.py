#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Decode what a vendor's mock intake received, and judge it (blueprint S7a).

Two legs per vendor, and this module refuses to let one stand in for the
other:

* **the log leg** — the envelope the vendor documents (the logs-intake
  JSON array for Datadog, ``_bulk`` NDJSON for Elasticsearch, the HEC
  event JSON for Splunk), decoded from the body the mock actually
  received rather than from a count of requests;
* **the trace leg** — OTLP ``ExportTraceServiceRequest`` protobuf, as
  the otel-bridge re-exports it, decoded with the same
  ``opentelemetry-proto`` the chassis relay parses OTLP with.

Each leg is checked for three things, and each is there because its
absence is a way a green tick could lie:

1. **The resource identity survived.** At least one decoded record and
   one decoded span carries ``service.name`` from its OTLP *resource*.
   A transform that emits a perfectly valid vendor envelope after
   dropping the resource attributes fails here — which is the whole
   point, because such a payload ingests fine and is useless.
2. **The demo run is in it.** The run's trace id (or, on the log leg,
   its run number) appears, so an intake that received somebody else's
   backlog does not pass for this run's telemetry.
3. **The S4 fixture is nowhere.** The address the echo agent emits
   through its OWN instrumentation — a custom span attribute, a link
   attribute, a span event, a log record — is absent from every byte of
   every leg. The overlays forward and never redact; both planes reach
   Vector already walked (S4's walkers and the authenticated relay are
   the only door in), and this is where that claim is re-asserted at
   each vendor's wire.

What the mocks prove is the WIRE SHAPE LibreRun emits — that it is the
one each vendor documents. They are not a vendor's acceptance of the
data; only an account at that vendor can tell you that.

Usage:
    obs_vendor_contract.py --vendor splunk --capture capture.jsonl \\
        --trace-id <32 hex> --run-number RUN-000001 \\
        --forbid pii.fixture@example.com --expect-service librerun-backend
"""
from __future__ import annotations

import argparse
import base64
import binascii
import gzip
import json
import sys
from dataclasses import dataclass, field

# The documented log-intake path per vendor: the mock records the path
# it was POSTed to, and a sink that posts its logs somewhere else is a
# sink whose envelope nobody at the vendor will ever read.
LOG_PATHS = {
    "datadog": "/api/v2/logs",
    "elastic": "/_bulk",
    "splunk": "/services/collector/event",
}

# The OTLP trace path per vendor, as each vendor documents it. Splunk's
# is deliberately NOT /v1/traces — that difference is exactly the kind
# of thing an untested overlay gets wrong.
TRACE_PATHS = {
    "datadog": "/v1/traces",
    "elastic": "/v1/traces",
    "splunk": "/v2/trace/otlp",
}

VENDORS = tuple(sorted(LOG_PATHS))


class ContractFailure(Exception):
    """A leg that did not hold. The message names what was missing."""


@dataclass
class Capture:
    """One request the mock intake received."""

    path: str
    headers: dict
    body: bytes

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")


@dataclass
class LegReport:
    """What one leg carried, for the CI log as much as for the asserts."""

    requests: int = 0
    records: int = 0
    services: set = field(default_factory=set)
    resource_services: set = field(default_factory=set)
    correlation_hits: int = 0

    def as_dict(self) -> dict:
        return {
            "requests": self.requests,
            "records": self.records,
            "services": sorted(self.services),
            "resource_services": sorted(self.resource_services),
            "correlation_hits": self.correlation_hits,
        }


def load_captures(path: str) -> list[Capture]:
    """Read the mock's JSONL journal.

    Bodies are stored base64 so a protobuf leg survives the round trip
    intact — decoding it to text first would lose the bytes the trace
    assertions need.
    """
    out = []
    with open(path, "rb") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            body = base64.b64decode(row.get("body_b64") or "")
            headers = {str(k).lower(): v for k, v in (row.get("headers") or {}).items()}
            if headers.get("content-encoding", "").lower() == "gzip":
                try:
                    body = gzip.decompress(body)
                except (OSError, EOFError, binascii.Error) as exc:
                    raise ContractFailure(
                        f"a {row.get('path')} request declared Content-Encoding: "
                        f"gzip and did not decompress ({exc}) — the sink and the "
                        f"intake disagree about the wire"
                    ) from exc
            out.append(Capture(path=row.get("path") or "", headers=headers, body=body))
    return out


# --------------------------------------------------------------------------
# The log leg: one decoder per vendor, each reading the envelope that
# vendor documents. They return the list of log RECORDS, so the checks
# below are the same three questions for all three vendors.
# --------------------------------------------------------------------------
def _decode_datadog(cap: Capture) -> list[dict]:
    """Datadog's logs intake: a JSON array of log objects."""
    payload = json.loads(cap.text)
    if not isinstance(payload, list):
        raise ContractFailure(
            f"the Datadog logs intake received {type(payload).__name__}, not the "
            f"JSON array its API documents"
        )
    return [r for r in payload if isinstance(r, dict)]


def _decode_elastic(cap: Capture) -> list[dict]:
    """Elasticsearch `_bulk`: NDJSON, action line then source line."""
    lines = [ln for ln in cap.text.split("\n") if ln.strip()]
    if not lines:
        raise ContractFailure("the Elasticsearch _bulk body was empty")
    records = []
    expect_action = True
    for ln in lines:
        doc = json.loads(ln)
        if expect_action:
            # The action line names the operation (create/index). A body
            # that is all source lines is not bulk NDJSON.
            if not isinstance(doc, dict) or not doc:
                raise ContractFailure(f"_bulk action line is not an object: {ln[:120]}")
            expect_action = False
            continue
        records.append(doc if isinstance(doc, dict) else {})
        expect_action = True
    if expect_action is False:
        raise ContractFailure(
            "the _bulk body ended on an action line with no source line — "
            "that is a malformed bulk request, not a log"
        )
    return records


def _decode_splunk(cap: Capture) -> list[dict]:
    """Splunk HEC: one or more `{"event": {...}}` objects, concatenated."""
    records, decoder, text, idx = [], json.JSONDecoder(), cap.text, 0
    while idx < len(text):
        while idx < len(text) and text[idx] in " \r\n\t":
            idx += 1
        if idx >= len(text):
            break
        doc, idx = decoder.raw_decode(text, idx)
        if not isinstance(doc, dict) or "event" not in doc:
            raise ContractFailure(
                f"a HEC request carried {str(doc)[:120]} — the HEC event JSON "
                f"Splunk documents wraps the payload in an `event` field"
            )
        event = doc["event"]
        records.append(event if isinstance(event, dict) else {"_raw": event})
    if not records:
        raise ContractFailure("the HEC body carried no events")
    return records


LOG_DECODERS = {
    "datadog": _decode_datadog,
    "elastic": _decode_elastic,
    "splunk": _decode_splunk,
}

# Where each vendor's overlay puts the service name it lifted, and where
# the untouched OTLP resource still sits. The resource is the one that
# makes the check bite: the overlay stamps the flat field for the
# vendor's UI, but only a record that really arrived with a resource has
# `resources`.
SERVICE_FIELDS = {
    "datadog": ("service",),
    "elastic": ("service", "name"),
    "splunk": ("service.name",),
}


def _dig(record: dict, path: tuple):
    node = record
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node if isinstance(node, str) else None


def check_log_leg(
    vendor: str,
    captures: list[Capture],
    *,
    trace_id: str,
    run_number: str,
    forbid: str,
    expect_service: str,
) -> LegReport:
    """The vendor's documented log envelope, decoded and judged."""
    path = LOG_PATHS[vendor]
    mine = [c for c in captures if c.path.rstrip("/").endswith(path.rstrip("/"))]
    if not mine:
        seen = sorted({c.path for c in captures})
        raise ContractFailure(
            f"nothing was POSTed to {vendor}'s documented logs path {path}. "
            f"Paths the mock saw: {seen or '(none at all)'}"
        )

    report = LegReport(requests=len(mine))
    decode = LOG_DECODERS[vendor]
    for cap in mine:
        records = decode(cap)
        report.records += len(records)
        for rec in records:
            flat = _dig(rec, SERVICE_FIELDS[vendor])
            if flat:
                report.services.add(flat)
            resources = rec.get("resources")
            if isinstance(resources, dict):
                name = resources.get("service.name")
                if isinstance(name, str) and name:
                    report.resource_services.add(name)
            blob = json.dumps(rec)
            if (trace_id and trace_id in blob) or (run_number and run_number in blob):
                report.correlation_hits += 1

    if not report.records:
        raise ContractFailure(
            f"{vendor}'s logs intake received {len(mine)} request(s) carrying no "
            f"decodable log records — an empty envelope is not a log leg"
        )

    # 1. the resource identity survived the transform
    if not report.resource_services:
        raise ContractFailure(
            f"no record at {vendor}'s logs intake carried an OTLP resource "
            f"(`resources.service.name`). The envelope decodes, so this would "
            f"ingest — and be unattributable. A transform that drops the "
            f"resource attributes fails exactly here. Flat service fields seen: "
            f"{sorted(report.services) or '(none)'}"
        )
    if expect_service and expect_service not in (
        report.services | report.resource_services
    ):
        raise ContractFailure(
            f"{vendor}'s logs intake never saw service {expect_service!r}. "
            f"Seen: flat {sorted(report.services)}, resource "
            f"{sorted(report.resource_services)}"
        )

    # 2. this run's telemetry, not somebody's backlog
    if not report.correlation_hits:
        raise ContractFailure(
            f"no record at {vendor}'s logs intake carried the demo run's trace "
            f"id ({trace_id or 'unset'}) or run number ({run_number or 'unset'}) "
            f"— {report.records} record(s) arrived and none of them is this run's"
        )

    # 3. the S4 raw-instrumentation fixture is nowhere on this leg
    _refuse_fixture(vendor, "log", mine, forbid)
    return report


# --------------------------------------------------------------------------
# The trace leg: OTLP protobuf, decoded with opentelemetry-proto.
# --------------------------------------------------------------------------
def check_trace_leg(
    vendor: str,
    captures: list[Capture],
    *,
    trace_id: str,
    forbid: str,
    expect_service: str,
) -> LegReport:
    """The OTLP protobuf the bridge re-exported, decoded and judged."""
    from opentelemetry.proto.collector.trace.v1 import trace_service_pb2

    path = TRACE_PATHS[vendor]
    mine = [c for c in captures if c.path.rstrip("/").endswith(path.rstrip("/"))]
    if not mine:
        seen = sorted({c.path for c in captures})
        raise ContractFailure(
            f"nothing was POSTed to {vendor}'s documented OTLP trace path "
            f"{path}. Paths the mock saw: {seen or '(none at all)'}. A trace "
            f"leg missing entirely must not pass on the strength of its logs."
        )

    report = LegReport(requests=len(mine))
    for cap in mine:
        content_type = cap.headers.get("content-type", "")
        if "x-protobuf" not in content_type:
            raise ContractFailure(
                f"{vendor}'s OTLP intake was sent Content-Type {content_type!r}; "
                f"the bridge exists to make this protobuf "
                f"(application/x-protobuf)"
            )
        req = trace_service_pb2.ExportTraceServiceRequest()
        req.ParseFromString(cap.body)  # raises on anything that is not OTLP
        for rs in req.resource_spans:
            for kv in rs.resource.attributes:
                if kv.key == "service.name" and kv.value.string_value:
                    report.resource_services.add(kv.value.string_value)
                    report.services.add(kv.value.string_value)
            for ss in rs.scope_spans:
                for span in ss.spans:
                    report.records += 1
                    if trace_id and span.trace_id.hex() == trace_id:
                        report.correlation_hits += 1

    if not report.records:
        raise ContractFailure(
            f"{vendor}'s OTLP intake received {len(mine)} request(s) that decoded "
            f"as OTLP and carried no spans — an empty ExportTraceServiceRequest "
            f"is not a trace leg"
        )
    if not report.resource_services:
        raise ContractFailure(
            f"no span at {vendor}'s OTLP intake carried a `service.name` resource "
            f"attribute — the spans arrived unattributable"
        )
    if expect_service and expect_service not in report.resource_services:
        raise ContractFailure(
            f"{vendor}'s OTLP intake never saw service {expect_service!r}. "
            f"Seen: {sorted(report.resource_services)}"
        )
    if trace_id and not report.correlation_hits:
        raise ContractFailure(
            f"no span at {vendor}'s OTLP intake carries the demo run's trace id "
            f"{trace_id} — {report.records} span(s) arrived and none is this run's"
        )

    _refuse_fixture(vendor, "trace", mine, forbid)
    return report


def _refuse_fixture(vendor: str, leg: str, captures: list[Capture], forbid: str) -> None:
    """The S4 raw-instrumentation fixture, refused on the raw bytes.

    On the BYTES, not on the decoded tree: a fixture hiding in a field
    this module does not model — a scope name, a dropped-attribute
    count's sibling, an events array a decoder skipped — is still a
    fixture that left the box. The bytes cannot be skipped by accident.
    """
    if not forbid:
        return
    needle = forbid.encode()
    for cap in captures:
        if needle in cap.body:
            raise ContractFailure(
                f"the S4 raw-instrumentation fixture {forbid!r} reached "
                f"{vendor}'s {leg} leg at {cap.path}. The overlays forward and "
                f"never redact, so this means the walk on export did not happen "
                f"— telemetry left the box carrying an address."
            )


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--vendor", required=True, choices=VENDORS)
    p.add_argument("--capture", required=True, help="the mock's JSONL journal")
    p.add_argument("--trace-id", default="", help="the demo run's trace id, 32 hex")
    p.add_argument("--run-number", default="", help="the demo run's run number")
    p.add_argument(
        "--forbid",
        default="",
        help="the S4 raw-instrumentation fixture, which must be on neither leg",
    )
    p.add_argument(
        "--expect-service",
        default="",
        help="a service.name that must appear on both legs",
    )
    p.add_argument("--summary-json", default="")
    args = p.parse_args(argv)

    captures = load_captures(args.capture)
    print(f"{args.vendor}: {len(captures)} request(s) captured")

    logs = check_log_leg(
        args.vendor,
        captures,
        trace_id=args.trace_id,
        run_number=args.run_number,
        forbid=args.forbid,
        expect_service=args.expect_service,
    )
    print(f"  log leg   OK  {json.dumps(logs.as_dict())}")

    traces = check_trace_leg(
        args.vendor,
        captures,
        trace_id=args.trace_id,
        forbid=args.forbid,
        expect_service=args.expect_service,
    )
    print(f"  trace leg OK  {json.dumps(traces.as_dict())}")

    summary = {
        "vendor": args.vendor,
        "logs": logs.as_dict(),
        "traces": traces.as_dict(),
        "fixture_absent": bool(args.forbid),
    }
    if args.summary_json:
        with open(args.summary_json, "w") as fh:
            json.dump(summary, fh, indent=2)
    print("CONTRACT OK " + json.dumps(summary))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ContractFailure as exc:
        print(f"::error::CONTRACT FAILED — {exc}")
        sys.exit(1)
