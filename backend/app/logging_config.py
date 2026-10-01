"""Central logging bootstrap. Call ``configure_logging()`` once at startup,
before any module-level loggers are touched.

Design:
    - A single ``TimedRotatingFileHandler`` writes NDJSON to ``settings.LOG_FILE_PATH``.
    - A ``StreamHandler`` to stderr preserves ``docker logs`` visibility. It
      emits JSON in non-development environments (so aggregators can ingest
      it) and a colored console format during local ``APP_ENV=development``
      work.
    - Root, ``uvicorn``, ``uvicorn.access``, and ``uvicorn.error`` all flow
      through ``structlog.stdlib.ProcessorFormatter``. This means a stdlib
      ``logger.info(...)`` call from any module comes out in the same JSON
      schema as a native ``structlog.get_logger(...).info(...)`` call, and
      uvicorn's access lines get reshaped (see ``logging_formatters``).
    - With ``LOG_QUEUE_ONLY`` (the default; blueprint S4) those sinks are
      not attached to any logger: every record goes through the queue-only
      pipeline in ``app.logging_queue`` — captured at emission, walked on
      the listener thread, fanned out to the sinks — and no handler can be
      attached that bypasses the walk. The test suite runs with the
      setting off (the synchronous pipeline below) so pytest's own
      capture handlers keep working, and tests the queue explicitly.
"""
from __future__ import annotations

import logging
import logging.config
import os
from logging.handlers import TimedRotatingFileHandler

import structlog

from app import logging_queue
from app.config import settings
from app.logging_formatters import (
    SHARED_PROCESSORS,
    STDLIB_PRE_CHAIN,
    strip_positional_args,
)
from app.logging_pii import redact_user_content_processor


_CONFIGURED = False

# Per-logger levels, both pipelines. LLM SDK loggers are pinned so
# prompt/response payloads stay out of the log file even at DEBUG
# (OpenInference instrumentors still capture them onto exported spans);
# ``opentelemetry`` is DEBUG so exporter problems (auth, TLS, network)
# that the BatchSpanProcessor would swallow on its thread are visible.
_LOGGER_LEVELS = {
    "openai": "INFO",
    "anthropic": "INFO",
    "google": "INFO",
    "httpx": "WARNING",
    "httpcore": "WARNING",
    "opentelemetry": "DEBUG",
    # Presidio logs an INFO line on EVERY analyze call ("Fetching all
    # recognizers ..."): one per redacted string, which on the walking
    # pipeline would be one more record per record.
    "presidio-analyzer": "WARNING",
    "presidio-anonymizer": "WARNING",
}


def _file_handler_factory() -> TimedRotatingFileHandler:
    path = settings.LOG_FILE_PATH
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, mode=0o750, exist_ok=True)
    return TimedRotatingFileHandler(
        filename=path,
        when=settings.LOG_FILE_ROTATION_WHEN,
        utc=True,
        backupCount=settings.LOG_FILE_BACKUP_COUNT,
        encoding="utf-8",
    )


def _probe_log_file() -> str | None:
    """Return None when ``settings.LOG_FILE_PATH`` is writable, else the error.

    A handler factory that raises inside ``dictConfig`` aborts the whole
    config ("Unable to configure handler 'file'") and takes the process down
    with it — which is how a root-owned ``./data/logs`` bind mount (Docker
    creates missing bind dirs as root) turned into a backend crash loop.
    The compose deployment starts the container as root (``user: "0:0"``)
    so the entrypoint can chown the mount before dropping privileges, but
    runs that never start as root (the override removed, Kubernetes, plain
    ``docker run``) or read-only mounts can still hit this. Losing the log
    *file* must not cost us the app, so probe up front and fall back to
    stderr-only when unwritable.
    """
    try:
        path = settings.LOG_FILE_PATH
        directory = os.path.dirname(path) or "."
        os.makedirs(directory, mode=0o750, exist_ok=True)
        with open(path, "a", encoding="utf-8"):
            pass
        return None
    except OSError as e:
        return f"{type(e).__name__}: {e}"


def configure_logging() -> None:
    """Install the JSON-file + stderr logging pipeline. Idempotent."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    is_dev = settings.APP_ENV.lower() == "development"

    final_processors = [
        structlog.stdlib.ProcessorFormatter.remove_processors_meta,
        strip_positional_args,
        redact_user_content_processor,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    json_processor = structlog.processors.JSONRenderer()
    console_processor = structlog.dev.ConsoleRenderer(colors=True)

    # structlog native-loggers pipeline: run SHARED_PROCESSORS, then hand off
    # to the stdlib formatter, which applies the final processors below.
    structlog.configure(
        processors=[
            *SHARED_PROCESSORS,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    foreign_pre_chain = [*STDLIB_PRE_CHAIN, *SHARED_PROCESSORS]

    file_error = _probe_log_file()
    file_ok = file_error is None
    # Never configure zero handlers: if the file is unwritable, stderr goes
    # on even when LOG_STDERR_ENABLED=false — a silent process is worse than
    # an ignored setting.
    stderr_on = settings.LOG_STDERR_ENABLED or not file_ok

    if settings.LOG_QUEUE_ONLY:
        _configure_queue_only(
            is_dev=is_dev,
            file_ok=file_ok,
            stderr_on=stderr_on,
            foreign_pre_chain=foreign_pre_chain,
            final_processors=final_processors,
            json_processor=json_processor,
            console_processor=console_processor,
        )
        _CONFIGURED = True
        _warn_if_file_unwritable(file_ok, file_error)
        return

    # A switch from the queue-only pipeline (a re-configuration with the
    # setting off) tears the queue down first, or dispatch would keep
    # bypassing the handlers configured below.
    if logging_queue.installed():
        logging_queue.uninstall()

    root_handlers = ["file"] if file_ok else []
    if stderr_on:
        root_handlers.append("stderr")

    dict_config = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "json": {
                "()": structlog.stdlib.ProcessorFormatter,
                "foreign_pre_chain": foreign_pre_chain,
                "pass_foreign_args": True,
                "processors": [*final_processors, json_processor],
            },
            "console": {
                "()": structlog.stdlib.ProcessorFormatter,
                "foreign_pre_chain": foreign_pre_chain,
                "pass_foreign_args": True,
                "processors": [*final_processors, console_processor],
            },
        },
        "handlers": {
            "stderr": {
                "class": "logging.StreamHandler",
                "formatter": "console" if is_dev else "json",
                "stream": "ext://sys.stderr",
                "level": settings.LOG_LEVEL,
            },
        },
        "root": {
            "level": settings.LOG_LEVEL,
            "handlers": root_handlers,
        },
        "loggers": {
            "uvicorn": {"level": settings.LOG_LEVEL, "propagate": True},
            "uvicorn.error": {"level": settings.LOG_LEVEL, "propagate": True},
            "uvicorn.access": {"level": "INFO", "propagate": True},
            **{
                name: {"level": level, "propagate": True}
                for name, level in _LOGGER_LEVELS.items()
            },
        },
    }

    # OTEL_DEBUG: attach dedicated DEBUG-level handlers directly to the
    # opentelemetry logger so exporter DEBUG records (auth, TLS, transport
    # responses) reach the file and stderr regardless of the global
    # LOG_LEVEL. propagate=False keeps them out of the root pipeline
    # (which would re-filter at LOG_LEVEL and lose them, or duplicate
    # them). The handlers reuse the same formatters as the rest of the
    # app so the output is JSON-aggregator friendly.
    if file_ok:
        dict_config["handlers"]["file"] = {
            "()": _file_handler_factory,
            "formatter": "json",
            "level": settings.LOG_LEVEL,
        }

    if settings.OTEL_DEBUG:
        debug_handlers = []
        if file_ok:
            dict_config["handlers"]["otel_debug_file"] = {
                "()": _file_handler_factory,
                "formatter": "json",
                "level": "DEBUG",
            }
            debug_handlers.append("otel_debug_file")
        if settings.LOG_STDERR_ENABLED or not file_ok:
            dict_config["handlers"]["otel_debug_stderr"] = {
                "class": "logging.StreamHandler",
                "formatter": "console" if is_dev else "json",
                "stream": "ext://sys.stderr",
                "level": "DEBUG",
            }
            debug_handlers.append("otel_debug_stderr")
        dict_config["loggers"]["opentelemetry"] = {
            "level": "DEBUG",
            "handlers": debug_handlers,
            "propagate": False,
        }

    logging.config.dictConfig(dict_config)

    _CONFIGURED = True
    _warn_if_file_unwritable(file_ok, file_error)


def _configure_queue_only(
    *,
    is_dev: bool,
    file_ok: bool,
    stderr_on: bool,
    foreign_pre_chain: list,
    final_processors: list,
    json_processor,
    console_processor,
) -> None:
    """Build the sinks and hand them to the queue-only pipeline. No sink
    is attached to any logger; the stderr sink writes to a duplicate of
    descriptor 2 taken now, before ``install_process_capture`` turns the
    descriptor into the belt's pipe."""

    def _formatter(processor):
        return structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=foreign_pre_chain,
            pass_foreign_args=True,
            processors=[*final_processors, processor],
        )

    json_formatter = _formatter(json_processor)
    stderr_formatter = _formatter(console_processor if is_dev else json_processor)

    root = logging.getLogger()
    root.setLevel(settings.LOG_LEVEL)
    for name, level in {
        "uvicorn": settings.LOG_LEVEL,
        "uvicorn.error": settings.LOG_LEVEL,
        "uvicorn.access": "INFO",
        **_LOGGER_LEVELS,
    }.items():
        lg = logging.getLogger(name)
        lg.setLevel(level)
        lg.propagate = True

    def _file_sink(level: str) -> logging.Handler:
        path = settings.LOG_FILE_PATH
        os.makedirs(os.path.dirname(path) or ".", mode=0o750, exist_ok=True)
        handler = logging_queue.GuardedFileHandler(
            filename=path,
            when=settings.LOG_FILE_ROTATION_WHEN,
            utc=True,
            backupCount=settings.LOG_FILE_BACKUP_COUNT,
            encoding="utf-8",
        )
        handler.setFormatter(json_formatter)
        handler.setLevel(level)
        return handler

    def _stderr_sink(level: str) -> logging.Handler:
        handler = logging_queue.GuardedStreamHandler(logging_queue.sink_stream(2))
        handler.setFormatter(stderr_formatter)
        handler.setLevel(level)
        return handler

    root_sinks: list[logging.Handler] = []
    if file_ok:
        root_sinks.append(_file_sink(settings.LOG_LEVEL))
    if stderr_on:
        root_sinks.append(_stderr_sink(settings.LOG_LEVEL))

    # OTEL_DEBUG: the ``opentelemetry.*`` loggers' records go to dedicated
    # DEBUG sinks regardless of LOG_LEVEL — the same fan-out the classic
    # pipeline's propagate=False handlers gave them, now behind the walk.
    debug_sinks: list[logging.Handler] = []
    if settings.OTEL_DEBUG:
        if file_ok:
            debug_sinks.append(_file_sink("DEBUG"))
        if stderr_on:
            debug_sinks.append(_stderr_sink("DEBUG"))

    logging_queue.install(
        root_sinks=root_sinks,
        debug_sinks=debug_sinks,
        redact=settings.LOG_REDACT_PII,
    )


def install_process_capture() -> None:
    """After :func:`configure_logging` with the queue-only pipeline on:
    capture ``sys.stdout``/``sys.stderr`` per caller context, route
    descriptors 1 and 2 through the walking queue, and carry context into
    threads and executor work items (blueprint S4). A no-op otherwise."""
    if not (settings.LOG_QUEUE_ONLY and logging_queue.installed()):
        return
    logging_queue.install_thread_context()
    logging_queue.install_stdio_capture(fd_belt=True)


def _warn_if_file_unwritable(file_ok: bool, file_error: str | None) -> None:
    if not file_ok:
        logging.getLogger(__name__).warning(
            "log_file_unwritable — JSON file logging disabled, stderr only. "
            "path=%s error=%s (container mode: the self-heal chown runs "
            "only when the container STARTS as root — compose sets "
            "user: \"0:0\" for exactly that. Seeing this means the "
            "container started unprivileged (override removed? "
            "Kubernetes? plain docker run?) or the mount is read-only. "
            "Manual fix: uid=$(docker run --rm --entrypoint '' "
            "<backend-image> id -u librerun) && chown -R $uid ./data/logs)",
            settings.LOG_FILE_PATH,
            file_error,
        )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Convenience wrapper. Prefer this in new code; existing
    ``logging.getLogger(__name__)`` calls keep working too.
    """
    return structlog.get_logger(name)
