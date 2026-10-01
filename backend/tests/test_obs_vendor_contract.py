"""The vendor contract checker, negative-tested (blueprint S7a).

``scripts/obs_vendor_contract.py`` is the gate the obs-vendors workflow
stands on: it decides whether what a vendor's mock intake received is
the wire shape that vendor documents, carrying this run's identity and
none of the S4 raw-instrumentation fixture. On a clean capture a broken
checker and a working one are indistinguishable — both print OK — so
every rule it enforces is exercised here by INJECTING the violation it
exists to catch, against captures built in this file rather than
against whatever CI happens to produce.

The captures are built from each vendor's documented envelope, so these
tests also pin the envelope: a decoder rewritten to accept a different
shape fails here before it can pass a real vendor's data.
"""
from __future__ import annotations

import base64
import gzip
import importlib.util
import json
import sys
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "obs_vendor_contract.py"
_spec = importlib.util.spec_from_file_location("obs_vendor_contract", _MODULE_PATH)
contract = importlib.util.module_from_spec(_spec)
# Registered before exec: @dataclass resolves annotations through
# ``sys.modules[cls.__module__]``, so a module executed outside it dies
# on its first dataclass.
sys.modules[_spec.name] = contract
_spec.loader.exec_module(contract)

TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
SPAN_ID = "00f067aa0ba902b7"
RUN_NUMBER = "RUN-000042"
FIXTURE = "pii.fixture@example.com"
SERVICE = "librerun-backend"
AGENT_SERVICE = "echo-v1"


# --------------------------------------------------------------------------
# Builders: each writes the envelope its vendor documents.
# --------------------------------------------------------------------------
def _row(path: str, body: bytes, headers: dict | None = None) -> dict:
    return {
        "path": path,
        "headers": headers or {"Content-Type": "application/json"},
        "body_b64": base64.b64encode(body).decode(),
    }


def _log_record(service_key, *, service=SERVICE, resources=True, text="hello"):
    rec: dict = {"message": text, "trace_id": TRACE_ID, "run_number": RUN_NUMBER}
    if isinstance(service_key, tuple):
        node = rec
        for key in service_key[:-1]:
            node = node.setdefault(key, {})
        node[service_key[-1]] = service
    else:
        rec[service_key] = service
    if resources:
        rec["resources"] = {"service.name": service}
    return rec


def _datadog_body(**kw) -> bytes:
    return json.dumps([_log_record("service", **kw)]).encode()


def _elastic_body(**kw) -> bytes:
    action = json.dumps({"create": {"_index": "logs-librerun-default"}})
    source = json.dumps(_log_record(("service", "name"), **kw))
    return (action + "\n" + source + "\n").encode()


def _splunk_body(**kw) -> bytes:
    return json.dumps({"event": _log_record("service.name", **kw)}).encode()


LOG_BODIES = {
    "datadog": _datadog_body,
    "elastic": _elastic_body,
    "splunk": _splunk_body,
}


def _trace_body(
    *, service=SERVICE, resource_attrs=True, trace_id=TRACE_ID, span_attr_text=None
) -> bytes:
    """A real OTLP ExportTraceServiceRequest, serialized as the bridge sends it."""
    from opentelemetry.proto.collector.trace.v1 import trace_service_pb2

    req = trace_service_pb2.ExportTraceServiceRequest()
    rs = req.resource_spans.add()
    if resource_attrs:
        kv = rs.resource.attributes.add()
        kv.key = "service.name"
        kv.value.string_value = service
    ss = rs.scope_spans.add()
    ss.scope.name = "librerun-obs-forward"
    span = ss.spans.add()
    span.trace_id = bytes.fromhex(trace_id)
    span.span_id = bytes.fromhex(SPAN_ID)
    span.name = "run"
    if span_attr_text is not None:
        attr = span.attributes.add()
        attr.key = "librerun.note"
        attr.value.string_value = span_attr_text
    return req.SerializeToString()


PROTO_HEADERS = {"Content-Type": "application/x-protobuf"}


def _capture_file(tmp_path, rows) -> str:
    path = tmp_path / "capture.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return str(path)


def _clean(tmp_path, vendor, **trace_kw):
    return _capture_file(
        tmp_path,
        [
            _row(contract.LOG_PATHS[vendor], LOG_BODIES[vendor]()),
            _row(contract.TRACE_PATHS[vendor], _trace_body(**trace_kw), PROTO_HEADERS),
        ],
    )


def _check(vendor, capture, **over):
    kw = {
        "trace_id": TRACE_ID,
        "run_number": RUN_NUMBER,
        "forbid": FIXTURE,
        "expect_service": SERVICE,
    }
    kw.update(over)
    captures = contract.load_captures(capture)
    logs = contract.check_log_leg(vendor, captures, **kw)
    traces = contract.check_trace_leg(
        vendor,
        captures,
        trace_id=kw["trace_id"],
        forbid=kw["forbid"],
        expect_service=kw["expect_service"],
    )
    return logs, traces


# --------------------------------------------------------------------------
# The positive control. Without it every negative below could pass on a
# checker that refuses everything.
# --------------------------------------------------------------------------
@pytest.mark.parametrize("vendor", contract.VENDORS)
def test_a_well_formed_capture_passes_both_legs(tmp_path, vendor):
    logs, traces = _check(vendor, _clean(tmp_path, vendor))
    assert logs.records == 1 and logs.correlation_hits == 1
    assert logs.resource_services == {SERVICE}
    assert traces.records == 1 and traces.correlation_hits == 1
    assert traces.resource_services == {SERVICE}


# --------------------------------------------------------------------------
# 1. The resource identity must survive the transform.
# --------------------------------------------------------------------------
@pytest.mark.parametrize("vendor", contract.VENDORS)
def test_a_valid_envelope_without_the_resource_fails_the_log_leg(tmp_path, vendor):
    """The exact defect the Changes paragraph names.

    The envelope still decodes — it is the vendor's documented shape,
    it would ingest, and a checker counting requests would pass it.
    What it has lost is who emitted it.
    """
    capture = _capture_file(
        tmp_path,
        [
            _row(contract.LOG_PATHS[vendor], LOG_BODIES[vendor](resources=False)),
            _row(contract.TRACE_PATHS[vendor], _trace_body(), PROTO_HEADERS),
        ],
    )
    with pytest.raises(contract.ContractFailure) as exc:
        _check(vendor, capture)
    assert "resource" in str(exc.value)


@pytest.mark.parametrize("vendor", contract.VENDORS)
def test_spans_without_a_service_name_resource_fail_the_trace_leg(tmp_path, vendor):
    with pytest.raises(contract.ContractFailure) as exc:
        _check(vendor, _clean(tmp_path, vendor, resource_attrs=False))
    assert "service.name" in str(exc.value)


# --------------------------------------------------------------------------
# 2. It has to be THIS run's telemetry.
# --------------------------------------------------------------------------
@pytest.mark.parametrize("vendor", contract.VENDORS)
def test_somebody_elses_backlog_does_not_pass_for_this_run(tmp_path, vendor):
    """Records and spans arrive, in the right shape, from another run."""
    other = "0" * 32
    capture = _capture_file(
        tmp_path,
        [
            _row(contract.LOG_PATHS[vendor], LOG_BODIES[vendor](text="unrelated")),
            _row(
                contract.TRACE_PATHS[vendor],
                _trace_body(trace_id=other),
                PROTO_HEADERS,
            ),
        ],
    )
    with pytest.raises(contract.ContractFailure) as exc:
        _check(vendor, capture, trace_id="9" * 32, run_number="RUN-999999")
    assert "trace id" in str(exc.value)


# --------------------------------------------------------------------------
# 3. The S4 raw-instrumentation fixture, on either leg, on the bytes.
# --------------------------------------------------------------------------
@pytest.mark.parametrize("vendor", contract.VENDORS)
def test_the_fixture_on_the_log_leg_fails(tmp_path, vendor):
    capture = _capture_file(
        tmp_path,
        [
            _row(
                contract.LOG_PATHS[vendor],
                LOG_BODIES[vendor](text=f"log record carrying {FIXTURE}"),
            ),
            _row(contract.TRACE_PATHS[vendor], _trace_body(), PROTO_HEADERS),
        ],
    )
    with pytest.raises(contract.ContractFailure) as exc:
        _check(vendor, capture)
    assert FIXTURE in str(exc.value) and "log leg" in str(exc.value)


@pytest.mark.parametrize("vendor", contract.VENDORS)
def test_the_fixture_in_a_span_attribute_fails(tmp_path, vendor):
    """Raw agent instrumentation is where S4 said the hole was."""
    capture = _capture_file(
        tmp_path,
        [
            _row(contract.LOG_PATHS[vendor], LOG_BODIES[vendor]()),
            _row(
                contract.TRACE_PATHS[vendor],
                _trace_body(span_attr_text=f"custom {FIXTURE}"),
                PROTO_HEADERS,
            ),
        ],
    )
    with pytest.raises(contract.ContractFailure) as exc:
        _check(vendor, capture)
    assert FIXTURE in str(exc.value) and "trace leg" in str(exc.value)


def test_the_fixture_is_refused_on_bytes_a_decoder_never_models(tmp_path):
    """A field this module does not decode is still a field that left the box.

    The scope name is carried by no assertion above; a tree-walking
    refusal would miss it, which is why the refusal reads the bytes.
    """
    from opentelemetry.proto.collector.trace.v1 import trace_service_pb2

    req = trace_service_pb2.ExportTraceServiceRequest()
    rs = req.resource_spans.add()
    kv = rs.resource.attributes.add()
    kv.key = "service.name"
    kv.value.string_value = SERVICE
    ss = rs.scope_spans.add()
    ss.scope.name = f"scope {FIXTURE}"
    span = ss.spans.add()
    span.trace_id = bytes.fromhex(TRACE_ID)
    span.span_id = bytes.fromhex(SPAN_ID)
    span.name = "run"

    capture = _capture_file(
        tmp_path,
        [
            _row(contract.LOG_PATHS["splunk"], LOG_BODIES["splunk"]()),
            _row(
                contract.TRACE_PATHS["splunk"],
                req.SerializeToString(),
                PROTO_HEADERS,
            ),
        ],
    )
    with pytest.raises(contract.ContractFailure) as exc:
        _check("splunk", capture)
    assert FIXTURE in str(exc.value)


# --------------------------------------------------------------------------
# 4. One leg must not pass for the other.
# --------------------------------------------------------------------------
@pytest.mark.parametrize("vendor", contract.VENDORS)
def test_a_missing_trace_leg_does_not_pass_on_the_strength_of_its_logs(tmp_path, vendor):
    capture = _capture_file(
        tmp_path, [_row(contract.LOG_PATHS[vendor], LOG_BODIES[vendor]())]
    )
    captures = contract.load_captures(capture)
    # The log leg is genuinely fine — that is the trap.
    contract.check_log_leg(
        vendor,
        captures,
        trace_id=TRACE_ID,
        run_number=RUN_NUMBER,
        forbid=FIXTURE,
        expect_service=SERVICE,
    )
    with pytest.raises(contract.ContractFailure) as exc:
        contract.check_trace_leg(
            vendor, captures, trace_id=TRACE_ID, forbid=FIXTURE, expect_service=SERVICE
        )
    assert contract.TRACE_PATHS[vendor] in str(exc.value)


@pytest.mark.parametrize("vendor", contract.VENDORS)
def test_a_missing_log_leg_is_not_excused_by_its_traces(tmp_path, vendor):
    capture = _capture_file(
        tmp_path,
        [_row(contract.TRACE_PATHS[vendor], _trace_body(), PROTO_HEADERS)],
    )
    with pytest.raises(contract.ContractFailure) as exc:
        _check(vendor, capture)
    assert contract.LOG_PATHS[vendor] in str(exc.value)


# --------------------------------------------------------------------------
# 5. The wire itself: paths, encodings, protobuf.
# --------------------------------------------------------------------------
def test_splunks_otlp_path_is_not_v1_traces(tmp_path):
    """Splunk documents /v2/trace/otlp. A bridge posting to /v1/traces is wrong."""
    assert contract.TRACE_PATHS["splunk"] == "/v2/trace/otlp"
    capture = _capture_file(
        tmp_path,
        [
            _row(contract.LOG_PATHS["splunk"], LOG_BODIES["splunk"]()),
            _row("/v1/traces", _trace_body(), PROTO_HEADERS),
        ],
    )
    with pytest.raises(contract.ContractFailure) as exc:
        _check("splunk", capture)
    assert "/v2/trace/otlp" in str(exc.value)


@pytest.mark.parametrize("vendor", contract.VENDORS)
def test_a_json_trace_leg_fails_because_the_bridge_exists_to_prevent_it(tmp_path, vendor):
    capture = _capture_file(
        tmp_path,
        [
            _row(contract.LOG_PATHS[vendor], LOG_BODIES[vendor]()),
            _row(
                contract.TRACE_PATHS[vendor],
                json.dumps({"resourceSpans": []}).encode(),
                {"Content-Type": "application/json"},
            ),
        ],
    )
    with pytest.raises(contract.ContractFailure) as exc:
        _check(vendor, capture)
    assert "protobuf" in str(exc.value)


def test_a_gzipped_body_is_decompressed_before_anything_is_judged(tmp_path):
    """The collector gzips OTLP by default; a checker reading the raw bytes
    would find neither the trace id nor the fixture in a compressed leg —
    and would call both absences a pass."""
    body = _trace_body(span_attr_text=f"custom {FIXTURE}")
    capture = _capture_file(
        tmp_path,
        [
            _row(contract.LOG_PATHS["elastic"], LOG_BODIES["elastic"]()),
            _row(
                contract.TRACE_PATHS["elastic"],
                gzip.compress(body),
                {"Content-Type": "application/x-protobuf", "Content-Encoding": "gzip"},
            ),
        ],
    )
    with pytest.raises(contract.ContractFailure) as exc:
        _check("elastic", capture)
    assert FIXTURE in str(exc.value)


def test_an_empty_export_request_is_not_a_trace_leg(tmp_path):
    from opentelemetry.proto.collector.trace.v1 import trace_service_pb2

    empty = trace_service_pb2.ExportTraceServiceRequest().SerializeToString()
    capture = _capture_file(
        tmp_path,
        [
            _row(contract.LOG_PATHS["datadog"], LOG_BODIES["datadog"]()),
            _row(contract.TRACE_PATHS["datadog"], empty, PROTO_HEADERS),
        ],
    )
    with pytest.raises(contract.ContractFailure) as exc:
        _check("datadog", capture)
    assert "no spans" in str(exc.value)


def test_an_empty_log_envelope_is_not_a_log_leg(tmp_path):
    capture = _capture_file(
        tmp_path,
        [
            _row(contract.LOG_PATHS["datadog"], b"[]"),
            _row(contract.TRACE_PATHS["datadog"], _trace_body(), PROTO_HEADERS),
        ],
    )
    with pytest.raises(contract.ContractFailure) as exc:
        _check("datadog", capture)
    assert "no decodable log records" in str(exc.value)


# --------------------------------------------------------------------------
# 6. Each vendor's envelope is that vendor's, not a lookalike.
# --------------------------------------------------------------------------
def test_datadog_rejects_a_bare_object_where_its_api_documents_an_array(tmp_path):
    capture = _capture_file(
        tmp_path,
        [
            _row(contract.LOG_PATHS["datadog"], json.dumps(_log_record("service")).encode()),
            _row(contract.TRACE_PATHS["datadog"], _trace_body(), PROTO_HEADERS),
        ],
    )
    with pytest.raises(contract.ContractFailure) as exc:
        _check("datadog", capture)
    assert "JSON array" in str(exc.value)


def test_splunk_rejects_a_payload_that_is_not_wrapped_in_an_event_field(tmp_path):
    capture = _capture_file(
        tmp_path,
        [
            _row(contract.LOG_PATHS["splunk"], json.dumps(_log_record("service.name")).encode()),
            _row(contract.TRACE_PATHS["splunk"], _trace_body(), PROTO_HEADERS),
        ],
    )
    with pytest.raises(contract.ContractFailure) as exc:
        _check("splunk", capture)
    assert "event" in str(exc.value)


def test_elastic_rejects_a_bulk_body_with_no_source_line(tmp_path):
    capture = _capture_file(
        tmp_path,
        [
            _row(
                contract.LOG_PATHS["elastic"],
                (json.dumps({"create": {"_index": "logs-librerun-default"}}) + "\n").encode(),
            ),
            _row(contract.TRACE_PATHS["elastic"], _trace_body(), PROTO_HEADERS),
        ],
    )
    with pytest.raises(contract.ContractFailure) as exc:
        _check("elastic", capture)
    assert "source line" in str(exc.value)


def test_multiple_hec_events_in_one_request_all_decode(tmp_path):
    """Vector concatenates HEC events; a decoder reading only the first
    would check one record and report on all of them."""
    good = json.dumps({"event": _log_record("service.name")})
    bad = json.dumps({"event": _log_record("service.name", text=f"second {FIXTURE}")})
    capture = _capture_file(
        tmp_path,
        [
            _row(contract.LOG_PATHS["splunk"], (good + "\n" + bad + "\n").encode()),
            _row(contract.TRACE_PATHS["splunk"], _trace_body(), PROTO_HEADERS),
        ],
    )
    with pytest.raises(contract.ContractFailure) as exc:
        _check("splunk", capture)
    assert FIXTURE in str(exc.value)


# --------------------------------------------------------------------------
# 7. The agent's own resource identity, which only the OTLP path carries.
# --------------------------------------------------------------------------
def test_the_agents_resource_identity_is_distinguishable_from_the_backends(tmp_path):
    """`echo-v1` can only come from a record that arrived with its own
    OTLP resource — the overlay's fallback stamps the backend's name."""
    capture = _capture_file(
        tmp_path,
        [
            _row(
                contract.LOG_PATHS["splunk"],
                json.dumps({"event": _log_record("service.name", service=AGENT_SERVICE)}).encode(),
            ),
            _row(
                contract.TRACE_PATHS["splunk"],
                _trace_body(service=AGENT_SERVICE),
                PROTO_HEADERS,
            ),
        ],
    )
    logs, traces = _check("splunk", capture, expect_service=AGENT_SERVICE)
    assert logs.resource_services == {AGENT_SERVICE}
    assert traces.resource_services == {AGENT_SERVICE}

    # …and a capture carrying only the backend's identity does not pass
    # for the agent's.
    with pytest.raises(contract.ContractFailure) as exc:
        _check("splunk", _clean(tmp_path, "splunk"), expect_service=AGENT_SERVICE)
    assert AGENT_SERVICE in str(exc.value)
