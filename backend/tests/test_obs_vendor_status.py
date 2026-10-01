"""What /admin/otel-status says about the vendor overlay (blueprint S7a).

The page exists so an operator can answer "where is my telemetry going
right now?" from inside the product. Three properties are worth a test
because each has a way of quietly becoming a lie:

* an unrecognised ``LIBRERUN_OBS_VENDOR`` is reported unsupported, not
  guessed at or silently treated as none — Groundcover is the named
  case (JR, 2026-09-20: not a supported vendor), and it is refused by
  being absent from the table rather than by a special case;
* the shipped default, blank, is not an error — no overlay is a healthy
  configuration and the page must not shout about it;
* the backend never claims to have read anything back from Vector: the
  sinks are the overlay's declaration and the response says so.
"""
from __future__ import annotations

import pytest

from app.observability import obs_vendors


def test_the_three_supported_vendors_and_no_fourth():
    assert set(obs_vendors.SUPPORTED_VENDORS) == {"datadog", "elastic", "splunk"}


@pytest.mark.parametrize("vendor", sorted(obs_vendors.SUPPORTED_VENDORS))
def test_a_supported_vendor_names_its_overlay_and_its_two_legs(vendor):
    status = obs_vendors.overlay_status(vendor)
    assert status["supported"] is True and status["active"] is True
    assert status["vendor"] == vendor
    # The overlay pair Vector loads and the bridge config, so the page
    # names files an operator can open.
    assert f"config/vector-{vendor}.yaml" in status["configs"]
    assert f"config/otel-bridge-{vendor}.yaml" in status["configs"]
    assert "config/vector-otlp-shape.yaml" in status["configs"]
    # Both legs, always: a vendor entry that lost its trace sink would
    # show a page reading "active" over logs alone.
    legs = {s["leg"] for s in status["sinks"]}
    assert legs == {"logs", "traces"}, status["sinks"]


@pytest.mark.parametrize(
    "raw",
    [
        "groundcover",  # JR, 2026-09-20: not a supported vendor
        "datadogg",  # a typo
        "newrelic",
        "cribl",  # a real overlay, but not one LIBRERUN_OBS_VENDOR selects
        "../../etc/passwd",
    ],
)
def test_an_unknown_vendor_is_reported_unsupported_and_never_half_configured(raw):
    status = obs_vendors.overlay_status(raw)
    assert status["supported"] is False
    assert status["active"] is False
    # Nothing is offered as though it were loaded: a config list or a
    # sink list here would read as "this is wired up" on the page.
    assert status["configs"] == [] and status["sinks"] == []
    assert raw in status["detail"]
    assert "not a vendor LibreRun" in status["detail"]


def test_groundcover_is_refused_by_absence_not_by_a_special_case():
    """A named exclusion rots the moment somebody renames the vendor.

    The refusal has to come from the table having three entries, so a
    fourth vendor is refused on the same grounds without anybody
    remembering to add it to a denylist.
    """
    source = (obs_vendors.__file__,)
    text = open(source[0], encoding="utf-8").read().lower()
    assert "groundcover" not in text, (
        "the module names Groundcover — the refusal must come from the "
        "supported table, not from a denylist that only knows the names "
        "somebody thought of"
    )


def test_no_overlay_is_a_healthy_configuration():
    for blank in ("", "   ", None):
        status = obs_vendors.overlay_status(blank)
        assert status["supported"] is True, blank
        assert status["active"] is False, blank
        assert status["vendor"] is None, blank
        assert status["sinks"] == []


def test_the_vendor_name_is_normalised_but_not_invented():
    assert obs_vendors.overlay_status("  Datadog  ")["vendor"] == "datadog"
    assert obs_vendors.overlay_status("SPLUNK")["active"] is True


def test_the_response_says_the_sinks_are_declared_not_read_back():
    """The one sentence that keeps this page from reading as a receipt."""
    status = obs_vendors.overlay_status("elastic")
    assert "not read back" in status["sinks_are"]


@pytest.mark.parametrize(
    "endpoint,host,port",
    [
        ("http://vector:4317", "vector", 4317),
        ("vector:4317", "vector", 4317),
        ("https://collector.example:443", "collector.example", 443),
        ("http://vector", "vector", 4317),
        ("https://collector.example", "collector.example", 443),
    ],
)
def test_the_otlp_endpoint_is_parsed_in_both_spellings(endpoint, host, port):
    """Compose and the OTEL SDK both accept a URL and a bare authority."""
    assert obs_vendors._endpoint_host_port(endpoint) == (host, port)


@pytest.mark.parametrize("endpoint", ["", "   ", None])
@pytest.mark.asyncio
async def test_no_endpoint_reports_not_exporting_rather_than_unreachable(endpoint):
    """Blank is the documented way to turn export off; calling that
    "unreachable" would send an operator hunting a network fault."""
    health = await obs_vendors.vector_health(endpoint)
    assert health["reachable"] is None
    assert health["checked"] == "none"


@pytest.mark.asyncio
async def test_an_unreachable_router_is_reported_as_such_with_the_error():
    # Port 1 on localhost: nothing listens, and the refusal is immediate.
    health = await obs_vendors.vector_health("http://127.0.0.1:1", timeout=0.5)
    assert health["reachable"] is False
    assert health["checked"].startswith("tcp connect to 127.0.0.1:1")
    assert health["detail"], "an unreachable router with no reason is a dead end"


@pytest.mark.asyncio
async def test_a_reachable_router_is_reported_reachable():
    """The positive control: without it, a probe that always failed
    would pass every negative above."""
    import socket

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    try:
        port = listener.getsockname()[1]
        health = await obs_vendors.vector_health(f"http://127.0.0.1:{port}")
        assert health["reachable"] is True, health
    finally:
        listener.close()
