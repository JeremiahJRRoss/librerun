"""Shared structlog processors and the processor chain used by both the
native-structlog app stream and the stdlib-bridged uvicorn streams.

Exposes:
    - ``add_otel_ids``: pulls active OTEL span IDs onto the event dict.
    - ``tag_stream_from_logger_name``: tags each record with ``stream=app``
      or ``stream=access``.
    - ``reshape_uvicorn_access_record``: pre-chain processor for uvicorn.access
      records. Pulls ``client_addr``, ``method``, ``path``, ``http_version``,
      ``status_code`` out of ``record.args`` onto top-level fields, strips
      query strings, and replaces the rendered message with ``http_request``.
    - ``SHARED_PROCESSORS``: the canonical processor list (structlog-native).
    - ``STDLIB_PRE_CHAIN``: pre-chain processors for stdlib records so
      uvicorn logs come out shaped identically to app logs.
"""
from __future__ import annotations

import logging
from typing import Any

import structlog

from app.logging_context import current_scope


# ---------------------------------------------------------------------------
# Processors
# ---------------------------------------------------------------------------

def merge_captured_context(logger, method_name, event_dict):
    """Merge the context the queue-only dispatcher captured on the record
    at emission (blueprint S4): a stdlib record is formatted on the
    listener thread, where the emitting task's contextvars are not
    bound — the runner's ``agent_id``, the request's ``request_id`` —
    so ``merge_contextvars`` would find nothing. Runs first in the
    foreign pre-chain; a no-op for records that were not queued."""
    record = event_dict.get("_record")
    captured = getattr(record, "_librerun_ctx", None) if record is not None else None
    if captured:
        for key, value in captured.items():
            event_dict.setdefault(key, value)
    return event_dict


def adopt_rendered_exception(logger, method_name, event_dict):
    """A queued stdlib record had its traceback and stack rendered to
    text — and walked — on the listener thread (``record.exc_text`` /
    ``record.stack_info``); carry them into the event dict as the
    ``exception`` / ``stack`` fields the renderers print, in place of
    the stdlib tuples the formatter would render after the walk."""
    record = event_dict.get("_record")
    if record is None:
        return event_dict
    exc_text = getattr(record, "exc_text", None)
    if exc_text and "exception" not in event_dict:
        event_dict.pop("exc_info", None)
        event_dict["exception"] = exc_text
    stack_text = event_dict.pop("stack_info", None)
    if stack_text and "stack" not in event_dict:
        event_dict["stack"] = stack_text
    return event_dict


def add_otel_ids(logger, method_name, event_dict):
    """Add ``trace_id`` and ``span_id`` from the active OTEL span, if any.

    A queued record carries the ids the dispatcher captured on the
    emitting thread; they win over the listener thread's (empty) span.
    No-op when OTEL isn't installed/configured or when no span is
    recording. Wrapped in a broad try/except because logging must never
    raise on account of observability plumbing.
    """
    record = event_dict.get("_record")
    captured = getattr(record, "_librerun_otel", None) if record is not None else None
    if captured:
        event_dict["trace_id"], event_dict["span_id"] = captured
        return event_dict
    try:
        from opentelemetry import trace  # type: ignore

        span = trace.get_current_span()
        ctx = span.get_span_context() if span else None
        if ctx is not None and getattr(ctx, "is_valid", False):
            event_dict["trace_id"] = format(ctx.trace_id, "032x")
            event_dict["span_id"] = format(ctx.span_id, "016x")
    except Exception:
        pass
    return event_dict


def add_librerun_scope(logger, method_name, event_dict):
    """Stamp the telemetry plane on every log line (``librerun_scope``).

    ``run`` while a task is executing an agent run (``agent_id`` bound by
    ``agent_runner``), ``platform`` otherwise. Derived from
    ``logging_context.current_scope()`` — the same predicate the span
    enricher uses for ``librerun.scope`` — rather than bound at call
    sites, so the field exists on EVERY line and cannot drift out of
    sync with the span attribute. Routing contract:
    ``docs/authoring/Agents_Design.md`` "Observability contract".
    """
    # A queued record is formatted off its task: the plane is read from
    # the merged context (``agent_id`` bound = run) with the live
    # predicate as the fallback for the synchronous pipeline.
    if event_dict.get("agent_id"):
        event_dict["librerun_scope"] = "run"
    else:
        event_dict["librerun_scope"] = current_scope()
    return event_dict


def tag_stream_from_logger_name(logger, method_name, event_dict):
    """Set ``stream`` based on logger name — ``access`` for uvicorn.access,
    ``app`` for everything else.
    """
    name = event_dict.get("logger") or event_dict.get("logger_name") or ""
    event_dict["stream"] = "access" if name == "uvicorn.access" else "app"
    return event_dict


def reshape_uvicorn_access_record(logger, method_name, event_dict):
    """Promote uvicorn.access ``record.args`` into top-level fields.

    Uvicorn's access logger emits records with ``args`` as a 5-tuple:
    ``(client_addr, method, full_path, http_version, status_code)``.
    Its default message is a human-readable format string that re-renders
    those args. We override the message to a stable event name
    (``http_request``) and hoist the fields out so Cribl doesn't have to
    parse the rendered line.

    The ``full_path`` field includes the query string when present; we
    strip it and set a boolean ``has_query`` flag instead. Query strings
    routinely carry secrets (tokens, credentials) — operational logs
    should never contain them.
    """
    record: logging.LogRecord | None = event_dict.get("_record")
    # Check name on the record itself, not event_dict["logger"], because this
    # processor runs in the stdlib pre-chain *before* add_logger_name.
    if record is None or getattr(record, "name", "") != "uvicorn.access":
        return event_dict

    # ProcessorFormatter clears record.args before the pre-chain runs, so we
    # rely on ``pass_foreign_args=True`` which stashes the original tuple
    # under ``positional_args`` in the event dict.
    args = event_dict.get("positional_args")
    if args is None:
        args = getattr(record, "args", None)

    if isinstance(args, tuple) and len(args) == 5:
        client_addr, method, full_path, http_version, status_code = args
        path, _, _ = str(full_path).partition("?")
        event_dict["client_addr"] = client_addr
        event_dict["method"] = method
        event_dict["path"] = path
        event_dict["has_query"] = "?" in str(full_path)
        event_dict["http_version"] = http_version
        try:
            event_dict["status_code"] = int(status_code)
        except (TypeError, ValueError):
            event_dict["status_code"] = status_code
        # What THIS processor produced, for the export walk. A logger
        # name is freely selectable — agent code can call
        # ``structlog.get_logger("uvicorn.access").info(…)`` — so the
        # walk cannot take the name as evidence that these fields came
        # from uvicorn's 5-tuple. It takes this instead, which only this
        # branch sets.
        record._librerun_access = (  # type: ignore[attr-defined]
            "client_addr", "method", "path", "has_query", "http_version", "status_code",
        )

    event_dict["event"] = "http_request"
    return event_dict


def duplicate_run_ids_for_one_release(logger, method_name, event_dict):
    """Blueprint S1 (L18): log lines carry ``run_id`` / ``run_number``.
    The pre-S1 spellings ride along as duplicates for one release, the
    same way spans carry ``case.id`` next to ``run.id`` — saved log
    queries keep working — and are removed at v1.1."""
    for new, old in (("run_id", "case_id"), ("run_number", "case_number")):
        if new in event_dict and old not in event_dict:
            event_dict[old] = event_dict[new]
    return event_dict


def strip_positional_args(logger, method_name, event_dict):
    """Remove the ``positional_args`` entry added by ProcessorFormatter's
    ``pass_foreign_args=True``. It's only needed for the reshape processor
    above — the raw tuple has no place in the final JSON output.
    """
    event_dict.pop("positional_args", None)
    return event_dict


# ---------------------------------------------------------------------------
# Processor chains
# ---------------------------------------------------------------------------

# Used as the structlog-native chain *and* as the ``foreign_pre_chain``
# for the stdlib ProcessorFormatter. The last processor (JSONRenderer /
# ConsoleRenderer) is attached by the formatter/config, not here.
SHARED_PROCESSORS: list[Any] = [
    structlog.contextvars.merge_contextvars,
    duplicate_run_ids_for_one_release,
    add_librerun_scope,
    structlog.processors.add_log_level,
    structlog.stdlib.add_logger_name,
    add_otel_ids,
    structlog.processors.TimeStamper(fmt="iso", utc=True),
    tag_stream_from_logger_name,
]


# Stdlib pre-chain — runs before SHARED_PROCESSORS when a stdlib record
# (uvicorn etc.) is formatted via ProcessorFormatter. Only the uvicorn
# reshape lives here; everything else is already shared.
STDLIB_PRE_CHAIN: list[Any] = [
    merge_captured_context,
    adopt_rendered_exception,
    reshape_uvicorn_access_record,
]
