"""Demo mode (blueprint S3, decision L22).

``LIBRERUN_DEMO=true`` is the zero-config local demo ``scripts/demo.sh``
boots: generated credentials printed to the terminal, a stub LLM, the
example agents and Jaeger on. It is the only mode in which the shipped
default ``APP_SECRET_KEY`` is accepted — everywhere else the backend
refuses to start on it, because a known secret signs every session token.
"""
from __future__ import annotations

import sys

import structlog

from app import config as _config
from app import secret_files

logger = structlog.get_logger(__name__)


class DefaultSecretRefused(RuntimeError):
    """The backend will not serve on the shipped ``APP_SECRET_KEY``."""


def secret_is_default(settings=None) -> bool:
    s = settings or _config.settings
    # ``reveal`` rather than ``.get_secret_value()``: this takes a
    # settings DOUBLE in some callers, and a guard that raised
    # AttributeError on a plain string would refuse to run exactly where
    # it is being tested.
    secret = secret_files.reveal(getattr(s, "APP_SECRET_KEY", "")).strip()
    return secret in ("", _config.DEFAULT_APP_SECRET_KEY)


def refuse_default_secret_unless_demo(settings=None) -> bool:
    """Startup guard. Raises :class:`DefaultSecretRefused` when the secret
    is the shipped default (or blank) and demo mode is off; otherwise
    returns whether demo mode is on, having announced it if so."""
    s = settings or _config.settings
    weak = secret_is_default(s)
    if weak and not s.LIBRERUN_DEMO:
        logger.error(
            "app_secret_key_refused",
            hint=(
                "APP_SECRET_KEY is the shipped default. Set a real secret "
                "(python -c \"import secrets; print(secrets.token_urlsafe(64))\") "
                "or run the zero-config demo with LIBRERUN_DEMO=true (scripts/demo.sh)."
            ),
        )
        raise DefaultSecretRefused(
            "refusing to start with the default APP_SECRET_KEY outside demo mode "
            "(set APP_SECRET_KEY, or LIBRERUN_DEMO=true for the local demo)"
        )
    if s.LIBRERUN_DEMO:
        log_demo_banner(s)
    return bool(s.LIBRERUN_DEMO)


def warn_if_provider_key_present(environ=None) -> list[str]:
    """Say so when a model provider's credential is in this process.

    The backend declares none of them as settings (``app/config.py``), so
    a ``.env`` carrying them no longer binds anything here. What that
    cannot reach is an operator who EXPORTED one into the shell that runs
    uvicorn, or a deployment that passes it to this container: those sit
    in ``os.environ``, where any in-process agent can read them with
    ``os.environ``, whatever the settings model says.

    A warning rather than a refusal. The key being present is not the
    platform's doing and may be incidental — a developer whose shell
    carries it for something else entirely — and a backend that refuses
    to start over another program's variable would be worse than the
    exposure it is warning about. Silence would be worse still: the claim
    this batch makes is that no provider credential is in this process,
    and an operator should be able to find out when that stopped being
    true. Names only; a warning that printed the value would be the leak.
    """
    import os

    env = os.environ if environ is None else environ
    present = [name for name in _config.PROVIDER_KEY_VARIABLES if (env.get(name) or "").strip()]
    if present:
        logger.warning(
            "provider_key_in_backend_environment",
            variables=sorted(present),
            hint=(
                "a model provider's credential is in the backend process, where "
                "every in-process agent shares it. The gateway is the only "
                "process that needs one (blueprint S4a): move it to the "
                "gateway's environment and unset it here."
            ),
        )
    return sorted(present)


# Every line the boxed banner can carry, as literals.
#
# The banner goes STRAIGHT to the console (below), past the walk every
# other line of this process's output goes through — so it must be
# impossible for a value to ride along. Composing it from these
# constants makes that structural rather than a promise:
# ``test_the_banner_is_built_only_from_literals`` fails the moment a
# line is built by interpolation. Anything variable belongs in the
# ``librerun_demo_mode`` event above, which IS walked.
BANNER_TITLE = "LibreRun DEMO MODE  (LIBRERUN_DEMO=true)"
BANNER_DEFAULT_SECRET = (
    "- the shipped default APP_SECRET_KEY is in use: every session token "
    "is signed with a public value"
)
# Whether the LLM is stubbed is the GATEWAY's fact from blueprint S4a,
# and this banner prints before the first request — so it says where to
# look rather than guessing from a switch this process no longer has.
BANNER_STUB_LLM = (
    "- whether the LLM is stubbed is the gateway's: GET /api/v1/meta reports it"
)
BANNER_CREDENTIALS = (
    "- if scripts/demo.sh started this, the admin credentials are in your "
    "terminal and in .env"
)
BANNER_FOOTER = (
    "Not for production. To leave demo mode: unset LIBRERUN_DEMO and set "
    "APP_SECRET_KEY."
)
BANNER_LINES = frozenset(
    {
        "",
        BANNER_TITLE,
        BANNER_DEFAULT_SECRET,
        BANNER_STUB_LLM,
        BANNER_CREDENTIALS,
        BANNER_FOOTER,
    }
)


def banner_lines(settings=None) -> list[str]:
    """The banner's lines, each one of :data:`BANNER_LINES`."""
    s = settings or _config.settings
    lines = [BANNER_TITLE, ""]
    if secret_is_default(s):
        lines.append(BANNER_DEFAULT_SECRET)
    lines.append(BANNER_STUB_LLM)
    lines.append(BANNER_CREDENTIALS)
    lines.append("")
    lines.append(BANNER_FOOTER)
    return lines


def log_demo_banner(settings=None) -> None:
    """The loud one: a structured event for the log pipeline and a boxed
    banner on the console for the person watching ``compose logs``.

    The banner does NOT go through ``sys.stderr``. Blueprint S4 replaced
    that with a writer that turns each line into a walked, queued log
    record — right for anything that might carry a value, and wrong for
    this: the first walk loads spaCy, so the banner surfaced seconds
    after the API was already serving, and an operator who looked in
    between saw a demo-mode deployment with no notice that it was one.
    It carries no value by construction (:data:`BANNER_LINES`), so it
    goes straight to the descriptor taken before the capture.
    """
    s = settings or _config.settings
    default_secret = secret_is_default(s)
    logger.warning(
        "librerun_demo_mode",
        default_secret=default_secret,
        agents_path=s.LIBRERUN_AGENTS_PATH or "(default)",
        hint="not for production: unset LIBRERUN_DEMO and set APP_SECRET_KEY to leave demo mode",
    )
    lines = banner_lines(s)
    width = max(len(line) for line in lines) + 4
    bar = "=" * width
    body = "\n".join(f"  {line}" for line in lines)
    text = f"\n{bar}\n{body}\n{bar}\n\n"
    from app import logging_queue

    if not logging_queue.write_console(text):
        # No capture installed (the test suite, LOG_QUEUE_ONLY off): the
        # ordinary stream IS the console.
        print(text, end="", file=sys.stderr, flush=True)
