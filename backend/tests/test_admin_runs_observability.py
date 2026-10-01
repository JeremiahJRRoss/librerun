"""Tests for the pluggable trace-viewer deep links (blueprint B5, L8) and
the customer/admin schema split.

``build_trace_url`` replaced the retired vendor-specific URL builder:
links now come from ``TRACE_VIEWER`` presets (jaeger / phoenix / tempo) or
``TRACE_VIEWER_URL_TEMPLATE``. The schema guards pin that the customer
``RunDetail`` exposes the deep link but never the raw ``trace_id``.
"""
from __future__ import annotations

import pytest

from app.observability import trace_viewer
from app.observability.trace_viewer import build_trace_url
from app.schemas.admin import AdminRunDetail
from app.schemas.run import RunDetail


@pytest.fixture
def viewer_settings(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "TRACE_VIEWER", "jaeger")
    monkeypatch.setattr(settings, "TRACE_VIEWER_BASE_URL", "http://localhost:16686")
    monkeypatch.setattr(settings, "TRACE_VIEWER_URL_TEMPLATE", "")
    return settings


def test_jaeger_preset_happy_path(viewer_settings):
    trace_id = "a" * 32
    assert build_trace_url(trace_id) == f"http://localhost:16686/trace/{trace_id}"


def test_base_url_trailing_slash_trimmed(monkeypatch, viewer_settings):
    monkeypatch.setattr(viewer_settings, "TRACE_VIEWER_BASE_URL", "http://localhost:16686/")
    assert build_trace_url("b" * 32) == "http://localhost:16686/trace/" + "b" * 32


def test_phoenix_preset(monkeypatch, viewer_settings):
    monkeypatch.setattr(viewer_settings, "TRACE_VIEWER", "phoenix")
    monkeypatch.setattr(viewer_settings, "TRACE_VIEWER_BASE_URL", "http://localhost:6006")
    assert build_trace_url("c" * 32) == "http://localhost:6006/traces/" + "c" * 32


def test_tempo_preset_embeds_trace_id(monkeypatch, viewer_settings):
    monkeypatch.setattr(viewer_settings, "TRACE_VIEWER", "tempo")
    monkeypatch.setattr(viewer_settings, "TRACE_VIEWER_BASE_URL", "http://localhost:3000")
    url = build_trace_url("d" * 32)
    assert url.startswith("http://localhost:3000/explore?")
    assert "d" * 32 in url


def test_custom_template_used_verbatim(monkeypatch, viewer_settings):
    monkeypatch.setattr(viewer_settings, "TRACE_VIEWER", "custom")
    monkeypatch.setattr(
        viewer_settings,
        "TRACE_VIEWER_URL_TEMPLATE",
        "https://viewer.example/x/{trace_id}?src={base}",
    )
    monkeypatch.setattr(viewer_settings, "TRACE_VIEWER_BASE_URL", "http://b/")
    assert (
        build_trace_url("e" * 32)
        == f"https://viewer.example/x/{'e' * 32}?src=http://b"
    )


def test_template_overrides_preset(monkeypatch, viewer_settings):
    """An explicit template wins even when a preset with a built-in exists."""
    monkeypatch.setattr(
        viewer_settings, "TRACE_VIEWER_URL_TEMPLATE", "{base}/custom/{trace_id}"
    )
    assert build_trace_url("f" * 32) == "http://localhost:16686/custom/" + "f" * 32


@pytest.mark.parametrize("missing", ["", None, "0" * 32])
def test_invalid_trace_id_returns_none(viewer_settings, missing):
    assert build_trace_url(missing) is None


@pytest.mark.parametrize("viewer", ["off", "OFF", "none", ""])
def test_viewer_off_returns_none(monkeypatch, viewer_settings, viewer):
    monkeypatch.setattr(viewer_settings, "TRACE_VIEWER", viewer)
    assert build_trace_url("a" * 32) is None


@pytest.mark.parametrize("viewer", ["langsmith", "custom", "not-a-preset"])
def test_template_required_viewers_without_template_return_none(
    monkeypatch, viewer_settings, viewer
):
    monkeypatch.setattr(viewer_settings, "TRACE_VIEWER", viewer)
    monkeypatch.setattr(trace_viewer, "_warned_missing_template", False)
    assert build_trace_url("a" * 32) is None


def test_customer_run_detail_has_link_but_not_raw_ids():
    """Regression guard: the deep link is customer-visible (B5 moved it to
    the run page); the raw trace/span ids stay admin-only."""
    assert "trace_url" in RunDetail.model_fields
    assert "trace_id" not in RunDetail.model_fields
    assert "phase2_span_id" not in RunDetail.model_fields


def test_admin_run_detail_includes_observability_fields():
    for field in ("trace_id", "phase2_span_id", "trace_url"):
        assert field in AdminRunDetail.model_fields, f"missing {field}"


def test_the_error_detail_is_admin_only_and_the_code_is_for_everyone():
    """Blueprint S7: an agent's failure text is operator-facing by the
    Run Contract, so the customer detail carries the chassis's code and
    sentence and never the text; the admin view carries all three."""
    assert "error_code" in RunDetail.model_fields
    assert "error_message" in RunDetail.model_fields
    assert "error_detail" not in RunDetail.model_fields
    assert "error_detail" in AdminRunDetail.model_fields
