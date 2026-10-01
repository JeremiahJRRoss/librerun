"""The deployment as the backend reads it (K blueprint K9-04; D16, L29, L31).

What Application Settings' Deployment panel and ``librerun doctor``'s
"Deployment" section show a platform operator: the posture, read-only,
each value with where it came from, so no shell is needed to find out
what the deployment is running with.

It is an **allowlist**. Each name below is read on its own from the
backend's settings model, and nothing else is: never a ``SecretStr``,
and never ``os.environ``'s items, so a variable nobody listed here — a
provider key an operator left in the backend's environment, a vendor
token — stays out of the answer by construction rather than by a filter
that would have to know every spelling of a secret. The three OTLP
header variables, which carry an exporter's credentials, are reported
by presence alone. A URL keeps its scheme, host, port and path, and
loses its userinfo, query and fragment (``strip_url``).
"""
from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from app import config as _config

# What a hint says, by what compose's backend ``environment:`` does with
# the name (compose.yaml). ``test_the_view_is_the_allowlist_and_nothing_else``
# holds each hint to that block, so a name compose starts pinning, or
# stops passing, turns the test red rather than the panel wrong.
FROM_ENV = "Change it in .env, then restart the backend."
PINNED_STATE_DIR = (
    "Compose pins it to /app/data/state, on a named volume; a value in .env "
    "reaches a local uvicorn alone."
)
NOT_PASSED = (
    "Compose does not pass it, so a container reads the default; a value in "
    ".env reaches a local uvicorn alone."
)
LEAVE_COMMENTED = (
    "Leave it commented: compose's default is where the agent containers "
    "reach the backend (docs/platform/Install.md)."
)

# (name, class, hint). Class [2] is each posture name of .env.example that
# the backend's settings model has — LIBRERUN_STUB_LLM aside, since keyless
# mode is the gateway's to report (D16), and ``stub`` below is its answer —
# and class [1] the four bootstrap names an operator asks about.
ALLOWLIST: tuple[tuple[str, int, str], ...] = (
    ("LIBRERUN_DEMO", 2, FROM_ENV),
    ("LIBRERUN_MAX_PHASE_SECONDS", 2, FROM_ENV),
    ("CREDENTIALS_ENABLED", 2, FROM_ENV),
    ("BCRYPT_ROUNDS", 2, FROM_ENV),
    ("PII_CONFIDENCE_THRESHOLD", 2, FROM_ENV),
    ("PII_PHONE_REGION", 2, FROM_ENV),
    ("LIBRERUN_PII_ALLOW_DEGRADED", 2, FROM_ENV),
    ("OTEL_EXPORTER_OTLP_ENDPOINT", 2, FROM_ENV),
    ("OTEL_EXPORTER_OTLP_PROTOCOL", 2, FROM_ENV),
    ("OTEL_SERVICE_NAME", 2, FROM_ENV),
    ("OTEL_DEBUG", 2, FROM_ENV),
    ("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", 2, FROM_ENV),
    ("UX_TELEMETRY_ENABLED", 2, FROM_ENV),
    ("UX_TELEMETRY_USER_UNITS_PER_MINUTE", 2, FROM_ENV),
    ("UX_TELEMETRY_TENANT_UNITS_PER_MINUTE", 2, FROM_ENV),
    ("UX_TELEMETRY_GLOBAL_UNITS_PER_MINUTE", 2, FROM_ENV),
    ("LOG_LEVEL", 2, FROM_ENV),
    ("LOG_FORMAT", 2, FROM_ENV),
    ("LOG_FILE_PATH", 2, FROM_ENV),
    ("LOG_FILE_ROTATION_WHEN", 2, FROM_ENV),
    ("LOG_FILE_BACKUP_COUNT", 2, FROM_ENV),
    ("LOG_REDACT_PII", 2, FROM_ENV),
    ("LOG_QUEUE_ONLY", 2, FROM_ENV),
    ("LOG_STDERR_ENABLED", 2, FROM_ENV),
    ("FILE_STORAGE_BACKEND", 2, NOT_PASSED),
    ("FILE_STORAGE_PATH", 2, NOT_PASSED),
    ("LIBRERUN_AGENTS_PATH", 1, FROM_ENV),
    ("LIBRERUN_STATE_DIR", 1, PINNED_STATE_DIR),
    ("LIBRERUN_PUBLIC_URL", 1, LEAVE_COMMENTED),
    ("LIBRERUN_SOURCE_URL", 1, FROM_ENV),
)

# The values that are URLs, each shown as ``strip_url`` leaves it.
URL_NAMES = frozenset(
    {"OTEL_EXPORTER_OTLP_ENDPOINT", "LIBRERUN_PUBLIC_URL", "LIBRERUN_SOURCE_URL"}
)

# Read by the OTel SDK from the process environment (the senders pass
# ``endpoint=`` alone), and each can carry an exporter's credential: only
# whether one is set is reported, never what it says.
OTLP_HEADER_VARIABLES = (
    "OTEL_EXPORTER_OTLP_HEADERS",
    "OTEL_EXPORTER_OTLP_TRACES_HEADERS",
    "OTEL_EXPORTER_OTLP_LOGS_HEADERS",
)

UNPARSED = "unparsed"


def strip_url(value: str | None) -> str | None:
    """A URL as far as it can be shown: scheme, host, port and path.

    Userinfo, query and fragment go, since each can carry a credential (a
    password, a token in ``?key=``). A bare authority, ``vector:4317``,
    stays bare. A value this cannot take apart — a port that is not a
    number, an ``@``, ``?`` or ``#`` still in what would be returned —
    answers ``unparsed`` rather than being passed through.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return text
    bare = "//" not in text
    try:
        parts = urlsplit(f"//{text}" if bare else text)
        host = parts.hostname
        port = parts.port
    except ValueError:
        return UNPARSED
    if not host:
        return UNPARSED
    netloc = f"[{host}]" if ":" in host else host
    if port is not None:
        netloc = f"{netloc}:{port}"
    shown = f"{netloc}{parts.path}" if bare else urlunsplit(
        (parts.scheme, netloc, parts.path, "", "")
    )
    if any(mark in shown for mark in "@?#"):
        return UNPARSED
    return shown


def _value(settings: Any, name: str) -> str | int | float | bool | None:
    value = getattr(settings, name)
    if isinstance(value, SecretStr):
        # Never reached: no name above is a secret, and the test holds the
        # list to that. Refusing here keeps a future edit from serving one.
        raise TypeError(f"{name} is a secret, and the deployment view shows none")
    if name in URL_NAMES:
        return strip_url(value)
    return value


async def build(db: AsyncSession, *, transport: dict) -> dict:
    """``GET /admin/deployment``'s body (``schemas.admin.DeploymentView``).

    ``transport`` is the request's own scheme and host (D43), which the
    route reads from the request and never from a variable.
    """
    # Per call, never an import-time binding: the tests swap
    # ``app.config.settings`` for a fresh instance.
    settings = _config.settings
    fields_set = settings.model_fields_set
    # Imported here, not at the top: app.main imports the admin router,
    # which imports this module.
    from app.main import source_url
    from app.services import gateway_client, provider_keys_service
    from app.version import __license__, __version__

    health = await gateway_client.health()
    status = await provider_keys_service.providers(db)
    return {
        "version": __version__,
        "license": __license__,
        "source_url": strip_url(source_url(settings.LIBRERUN_SOURCE_URL)),
        "demo": bool(settings.LIBRERUN_DEMO),
        # Keyless mode is the gateway's to know (D16), as /meta has it:
        # null when the gateway does not answer.
        "stub": bool(health.get("stub")) if health is not None else None,
        "gateway": {
            "reachable": health is not None,
            "reported": status["reported"],
            "version": status["gateway_version"],
            "updated_at": status["updated_at"],
            "providers": [
                {
                    "name": entry["name"],
                    "source": entry["source"],
                    "fingerprint": entry["fingerprint"],
                }
                for entry in status["providers"]
            ],
        },
        "settings": [
            {
                "name": name,
                "env_class": env_class,
                "value": _value(settings, name),
                "source": "env" if name in fields_set else "default",
                "hint": hint,
            }
            for name, env_class, hint in ALLOWLIST
        ],
        "otlp_headers": [
            {"name": name, "set": bool(os.environ.get(name))}
            for name in OTLP_HEADER_VARIABLES
        ],
        "transport": transport,
    }
