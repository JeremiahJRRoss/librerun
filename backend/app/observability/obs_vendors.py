"""Which observability vendor overlay this deployment is running (S7a, L26).

The chassis forwards nothing itself and holds none of a vendor's
credentials — ``observability.env`` is mounted on ``vector`` and
``otel-bridge`` alone. What the backend has is the NAME the operator
selected, ``LIBRERUN_OBS_VENDOR``, passed through by compose so
``/admin/otel-status`` can answer the question an operator actually
asks: *where is my telemetry going right now?*

Two honest limits, both stated in the response rather than papered over:

* **The sinks are the overlay's, declared.** They are read from the
  table below — the same table the overlay files implement — and not
  read back from Vector. Vector's API is bound to ``127.0.0.1`` inside
  its own container on purpose (``config/vector.yaml``), and widening it
  to the compose network so a status page could query it would trade a
  real boundary for a nicer field. The response says which it is.
* **Vector's health is reachability from here.** A TCP connect to the
  OTLP endpoint the backend exports to, which is the only vantage point
  the backend legitimately has, and the one that matters to it: if this
  fails, the backend's own spans are going nowhere, whatever any vendor
  overlay downstream is doing.

An unrecognised value is reported as unsupported and never guessed at.
Vector and the bridge refuse to start on one (it names a config file
that is not mounted), so an unsupported vendor here means nothing is
being forwarded — which is what the operator needs told.
"""
from __future__ import annotations

import asyncio
import socket
from dataclasses import dataclass, field
from urllib.parse import urlparse


@dataclass(frozen=True)
class VendorOverlay:
    """One supported overlay: the files it loads and the legs it ships."""

    vendor: str
    label: str
    vector_config: str
    bridge_config: str
    sinks: tuple[dict, ...] = field(default_factory=tuple)


# The three tested overlays of 1.0 (blueprint §3, R9; decision L26).
# A vendor absent from this table is unsupported — there is no fourth
# entry and no fallback, so a typo, or a vendor LibreRun does not ship,
# is reported as such rather than half-configured.
SUPPORTED_VENDORS: dict[str, VendorOverlay] = {
    "datadog": VendorOverlay(
        vendor="datadog",
        label="Datadog",
        vector_config="config/vector-datadog.yaml",
        bridge_config="config/otel-bridge-datadog.yaml",
        sinks=(
            {
                "id": "datadog_logs",
                "leg": "logs",
                "type": "datadog_logs",
                "destination": "Datadog logs intake (POST <DATADOG_LOGS_ENDPOINT>/api/v2/logs)",
            },
            {
                "id": "datadog_traces",
                "leg": "traces",
                "type": "http -> otel-bridge",
                "destination": "a Datadog Agent's OTLP receiver (DD_AGENT_OTLP_URL), OTLP protobuf",
            },
        ),
    ),
    "elastic": VendorOverlay(
        vendor="elastic",
        label="Elastic",
        vector_config="config/vector-elastic.yaml",
        bridge_config="config/otel-bridge-elastic.yaml",
        sinks=(
            {
                "id": "elasticsearch_logs",
                "leg": "logs",
                "type": "elasticsearch",
                "destination": "Elasticsearch data streams (POST <ELASTIC_URL>/_bulk)",
            },
            {
                "id": "elastic_traces",
                "leg": "traces",
                "type": "http -> otel-bridge",
                "destination": "Elastic APM OTLP intake (ELASTIC_APM_OTLP_URL), OTLP protobuf",
            },
        ),
    ),
    "splunk": VendorOverlay(
        vendor="splunk",
        label="Splunk",
        vector_config="config/vector-splunk.yaml",
        bridge_config="config/otel-bridge-splunk.yaml",
        sinks=(
            {
                "id": "splunk_hec_logs",
                "leg": "logs",
                "type": "splunk_hec_logs",
                "destination": "Splunk HEC (POST <SPLUNK_HEC_URL>/services/collector/event)",
            },
            {
                "id": "splunk_traces",
                "leg": "traces",
                "type": "http -> otel-bridge",
                "destination": "Splunk Observability ingest (SPLUNK_OTLP_URL), OTLP protobuf",
            },
        ),
    ),
}

SINKS_ARE = (
    "declared by the overlay config this selection loads, not read back "
    "from Vector's API (which is bound to loopback inside its container)"
)


def overlay_status(raw: str | None) -> dict:
    """What ``LIBRERUN_OBS_VENDOR`` means, reported rather than guessed.

    Blank is the shipped default and is not an error: no overlay is
    loaded and the telemetry spine behaves exactly as it does with none
    configured.
    """
    vendor = (raw or "").strip().lower()
    if not vendor:
        return {
            "vendor": None,
            "supported": True,
            "active": False,
            "detail": (
                "no vendor overlay selected — Vector runs its base config "
                "alone and nothing is forwarded to a vendor"
            ),
            "configs": [],
            "sinks": [],
            "sinks_are": SINKS_ARE,
        }

    overlay = SUPPORTED_VENDORS.get(vendor)
    if overlay is None:
        return {
            "vendor": vendor,
            "supported": False,
            "active": False,
            "detail": (
                f"LIBRERUN_OBS_VENDOR={vendor!r} is not a vendor LibreRun "
                f"ships an overlay for. Supported: "
                f"{', '.join(sorted(SUPPORTED_VENDORS))}. Vector and the "
                f"otel-bridge refuse to start on this value — it names a "
                f"config file that is not mounted — so nothing is being "
                f"forwarded to a vendor."
            ),
            "configs": [],
            "sinks": [],
            "sinks_are": SINKS_ARE,
        }

    return {
        "vendor": overlay.vendor,
        "label": overlay.label,
        "supported": True,
        "active": True,
        "detail": (
            f"{overlay.label} overlay selected: Vector loads "
            f"config/vector-otlp-shape.yaml and {overlay.vector_config}, "
            f"and the otel-bridge loads {overlay.bridge_config}."
        ),
        "configs": [
            "config/vector.yaml",
            "config/vector-otlp-shape.yaml",
            overlay.vector_config,
            overlay.bridge_config,
        ],
        "sinks": [dict(s) for s in overlay.sinks],
        "sinks_are": SINKS_ARE,
    }


def _endpoint_host_port(endpoint: str) -> tuple[str, int] | None:
    """``host, port`` of an OTLP endpoint, or ``None`` if there is none.

    Accepts the two spellings compose and the OTEL SDK both allow: a
    full URL (``http://vector:4317``) and a bare authority
    (``vector:4317``).
    """
    endpoint = (endpoint or "").strip()
    if not endpoint:
        return None
    parsed = urlparse(endpoint if "//" in endpoint else f"//{endpoint}")
    if not parsed.hostname:
        return None
    port = parsed.port
    if port is None:
        port = 443 if parsed.scheme == "https" else 4317
    return parsed.hostname, port


def _connect(host: str, port: int, timeout: float) -> str | None:
    try:
        socket.create_connection((host, port), timeout=timeout).close()
    except OSError as exc:  # unreachable, refused, DNS, timeout
        return f"{type(exc).__name__}: {exc}"
    return None


async def vector_health(endpoint: str, *, timeout: float = 1.5) -> dict:
    """Can the backend still reach the router it exports spans to?

    A TCP connect, off the event loop. Not Vector's own ``/health``:
    that API is deliberately loopback-only inside Vector's container, so
    this reports what the backend can actually observe and says so.
    """
    target = _endpoint_host_port(endpoint)
    if target is None:
        return {
            "endpoint": endpoint or None,
            "reachable": None,
            "checked": "none",
            "detail": (
                "no OTLP endpoint configured — the backend exports no "
                "spans, so no overlay can forward any"
            ),
        }

    host, port = target
    error = await asyncio.to_thread(_connect, host, port, timeout)
    return {
        "endpoint": endpoint,
        "reachable": error is None,
        "checked": f"tcp connect to {host}:{port} ({timeout}s)",
        "detail": error
        or "the OTLP endpoint accepted a connection from the backend",
    }


__all__ = [
    "SUPPORTED_VENDORS",
    "VendorOverlay",
    "overlay_status",
    "vector_health",
]
