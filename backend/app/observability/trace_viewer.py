"""Pluggable trace-viewer deep links (blueprint B5, decision L8).

Replaces the retired vendor-specific admin deep-link builder: the platform
builds a "View trace" URL for whatever viewer the deployment runs,
selected by ``TRACE_VIEWER``:

- ``jaeger`` (default) — the bundled all-in-one from the compose
  ``viewer`` profile; ``TRACE_VIEWER_BASE_URL`` defaults to its UI.
- ``phoenix`` — Phoenix (self-hosted).
- ``tempo`` — a Grafana Explore deep link querying the ``tempo``
  datasource by TraceQL. Assumes the default datasource uid ``tempo``;
  use ``custom`` if yours differs.
- ``langsmith`` — LangSmith has no stable public deep-link from a bare
  W3C trace id, so this preset REQUIRES ``TRACE_VIEWER_URL_TEMPLATE``
  (a one-time warning is logged and no link is rendered until set).
- ``custom`` — ``TRACE_VIEWER_URL_TEMPLATE`` is used verbatim.
- ``off`` — never render a trace link.

Templates may reference ``{base}`` (``TRACE_VIEWER_BASE_URL`` with any
trailing slash removed) and ``{trace_id}`` (32-hex W3C trace id).
"""

from __future__ import annotations

import structlog

from app import config as _config

logger = structlog.get_logger(__name__)

_ZERO_TRACE_ID = "0" * 32

_PRESET_TEMPLATES: dict[str, str] = {
    "jaeger": "{base}/trace/{trace_id}",
    "phoenix": "{base}/traces/{trace_id}",
    "tempo": (
        "{base}/explore?schemaVersion=1&orgId=1&panes=%7B%22lr%22%3A%7B"
        "%22datasource%22%3A%22tempo%22%2C%22queries%22%3A%5B%7B%22query"
        "%22%3A%22{trace_id}%22%2C%22queryType%22%3A%22traceql%22%7D%5D"
        "%7D%7D"
    ),
    # "langsmith" and "custom" have no built-in template — they require
    # TRACE_VIEWER_URL_TEMPLATE.
}

_TEMPLATE_REQUIRED = {"langsmith", "custom"}

_warned_missing_template = False


def _render(
    trace_id: str | None, viewer: str, base_url: str, url_template: str
) -> str | None:
    """Pure link builder — shared by the env-config and runtime-config paths."""
    global _warned_missing_template

    if not trace_id or trace_id == _ZERO_TRACE_ID:
        return None

    viewer = (viewer or "").strip().lower()
    if viewer in ("", "off", "none"):
        return None

    template = (url_template or "").strip() or _PRESET_TEMPLATES.get(viewer)
    if not template:
        if not _warned_missing_template:
            _warned_missing_template = True
            reason = (
                "TRACE_VIEWER_URL_TEMPLATE is required for this viewer"
                if viewer in _TEMPLATE_REQUIRED
                else f"unknown TRACE_VIEWER preset {viewer!r}"
            )
            logger.warning(
                "trace_viewer_link_disabled", viewer=viewer, reason=reason
            )
        return None

    base = (base_url or "").strip().rstrip("/")
    if "{base}" in template and not base:
        # A preset with no base URL would render a relative link into the
        # LibreRun UI itself — worse than no link (Codex on PR #51).
        if not _warned_missing_template:
            _warned_missing_template = True
            logger.warning(
                "trace_viewer_link_disabled",
                viewer=viewer,
                reason="TRACE_VIEWER_BASE_URL is blank and the template uses {base}",
            )
        return None
    return template.replace("{base}", base).replace("{trace_id}", trace_id)


# Any well-formed non-zero id: the probe only asks whether a link CAN be
# rendered for this viewer configuration.
_PROBE_TRACE_ID = "f" * 32


def viewer_configured(viewer: str, base_url: str, url_template: str) -> bool:
    """Whether "View trace" links render at all for this configuration
    (blueprint S3, the public ``/meta`` fact): a preset with a template,
    or a custom template — never ``off``."""
    return _render(_PROBE_TRACE_ID, viewer, base_url, url_template) is not None


def _normalized(viewer: str, base_url: str, url_template: str) -> tuple[str, str, str]:
    return (
        (viewer or "").strip().lower() or "off",
        (base_url or "").strip(),
        (url_template or "").strip(),
    )


async def effective_viewer(db) -> tuple[str, str, str, str]:
    """The viewer configuration in effect — ``(viewer, base_url,
    url_template, source)`` — honoring the runtime overrides, with the
    same environment fallback as :func:`effective_trace_url`.

    ``source`` is ``"env"`` when the effective values are the
    environment's, ``"runtime"`` when an admin override changed any of
    them (Codex on PR #51): the public ``/meta`` says which, so a script
    that knows the environment knows whether its destination is the
    one the links open — the destination itself is not a public fact.
    """
    from app.services import app_settings_service as _svc

    s = _config.settings
    env = _normalized(s.TRACE_VIEWER, s.TRACE_VIEWER_BASE_URL, s.TRACE_VIEWER_URL_TEMPLATE)
    try:
        effective = _normalized(
            await _svc.get_setting(db, "trace_viewer"),
            await _svc.get_setting(db, "trace_viewer_base_url"),
            await _svc.get_setting(db, "trace_viewer_url_template"),
        )
    except Exception:  # noqa: BLE001 — a public fact must never 500 over a convenience link
        effective = env
    viewer, base_url, template = effective
    return viewer, base_url, template, "env" if effective == env else "runtime"


async def effective_viewer_configured(db) -> bool:
    """``viewer_configured`` honoring the runtime overrides (blueprint S3,
    the public ``/meta`` fact)."""
    viewer, base_url, template, _source = await effective_viewer(db)
    return viewer_configured(viewer, base_url, template)


def build_trace_url(trace_id: str | None) -> str | None:
    """Deep-link for ``trace_id`` from **environment** config, or ``None``.

    Returns ``None`` for a missing/all-zeros trace id (OTEL's "no span
    was active" value), when ``TRACE_VIEWER=off``, or when the selected
    viewer needs a template that isn't configured. Reads settings via
    the module so test fixtures that swap ``app.config.settings`` are
    observed. Request handlers should prefer :func:`effective_trace_url`,
    which honors runtime overrides from ``/admin/settings``.
    """
    s = _config.settings
    return _render(
        trace_id, s.TRACE_VIEWER, s.TRACE_VIEWER_BASE_URL, s.TRACE_VIEWER_URL_TEMPLATE
    )


_warned_runtime_fallback = False


async def effective_trace_url(db, trace_id: str | None) -> str | None:
    """Deep-link honoring runtime overrides (``/admin/settings``).

    The three ``trace_viewer*`` keys are registry-backed: a DB override set
    in the admin UI wins, otherwise the live environment defaults apply.
    Any failure reading the overrides — Redis down, table missing — falls
    back to :func:`build_trace_url`: a run page must never fail over a
    convenience link, and the fallback is exactly yesterday's behavior.
    """
    global _warned_runtime_fallback
    if not trace_id or trace_id == _ZERO_TRACE_ID:
        return None
    from app.services import app_settings_service as _svc

    try:
        viewer = await _svc.get_setting(db, "trace_viewer")
        base_url = await _svc.get_setting(db, "trace_viewer_base_url")
        template = await _svc.get_setting(db, "trace_viewer_url_template")
    except Exception as exc:
        if not _warned_runtime_fallback:
            _warned_runtime_fallback = True
            logger.warning(
                "trace_viewer_runtime_settings_unavailable",
                error=str(exc),
                error_type=type(exc).__name__,
                fallback="environment configuration",
            )
        return build_trace_url(trace_id)
    return _render(trace_id, viewer, base_url, template)


__all__ = ["build_trace_url", "effective_trace_url"]
