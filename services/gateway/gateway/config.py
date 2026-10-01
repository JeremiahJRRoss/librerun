"""The gateway's own settings.

Deliberately a separate model from the backend's: the two processes hold
different secrets on purpose. The gateway has the provider credentials
and no ``APP_SECRET_KEY``; the backend has the reverse. A shared
settings class would make it too easy for one to start reading the
other's environment.
"""
from __future__ import annotations

import os
from pathlib import Path

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# The chassis module that owns the ``<NAME>_FILE`` rule. The gateway
# image copies ``backend/app`` (``services/gateway/Dockerfile``) for
# exactly this kind of sharing — the redaction pipeline and the export
# walkers come the same way — and one helper for two settings models is
# the whole point of K2: a second copy of the rule is a second place for
# it to be subtly different.
from app import secret_files

# The file that carries the provider credentials, and the only file that
# does (K1, decision L28). Loaded by the gateway compose service as a
# second env_file and read here as a dotenv, so a local
# `uvicorn gateway.main:app` and the container see the same thing.
GATEWAY_ENV_FILENAME = "gateway.env"


def repository_root(module_path: Path) -> Path:
    """The repository root above ``services/gateway/gateway/config.py``.

    Three levels up in a checkout. The image flattens the tree to
    ``/app/gateway/config.py`` (``services/gateway/Dockerfile``), where
    three levels up does not exist — so this returns the highest parent
    instead of raising ``IndexError`` at import and taking the container
    down. Nothing is lost there: compose delivers real environment
    variables, and a dotenv path that does not exist is skipped.
    """
    parents = module_path.resolve().parents
    return parents[3] if len(parents) > 3 else parents[-1]


# The dotenv sources, anchored to the repository root rather than left
# relative to the working directory. In containers this changes nothing —
# compose delivers real environment variables, which outrank every dotenv
# — but `uvicorn gateway.main:app` is documented to run from
# `services/gateway/`, where a bare ".env" found neither the root .env nor
# gateway.env and the process came up with no credential and no complaint.
# `backend/app/config.py` anchors the same way, and the two files sit side
# by side at the root.
#
# Order is precedence: pydantic-settings gives the LAST entry priority.
# So per file the copy in the working directory still outranks the root
# one (the backend's rule), and gateway.env outranks .env for every name
# both spell. That direction is load-bearing, and measured: an operator
# upgrading from a pre-K1 checkout has `OPENAI_API_KEY=` left blank in
# .env, and with the files the other way round that blank wins and the
# real key is lost — the same override that took the three interpolated
# lines out of the gateway's compose block.
#
# .env is still read because the gateway takes DATABASE_URL, the PII
# settings and the posture switches from it in dev mode; gateway.env is
# read for the provider credentials and nothing else. The backend names
# gateway.env nowhere, which a test pins.
_REPO_ROOT = repository_root(Path(__file__))
ENV_FILES = (
    _REPO_ROOT / ".env",
    ".env",
    _REPO_ROOT / GATEWAY_ENV_FILENAME,
    GATEWAY_ENV_FILENAME,
)

# The environment variable prefix a per-agent key is provisioned under
# (D10). ``<ID>`` is the agent id normalised for the environment:
# upper-cased with every character outside [A-Z0-9] replaced by ``_``.
AGENT_KEY_PREFIX = "LIBRERUN_AGENT_KEY_"

# The suffix that marks the *previous* value of an env-provisioned key
# during a rotation. Reserved, which is why an agent id ending in
# ``-previous`` cannot be provisioned from the environment (keys.py
# refuses it by name rather than guessing).
AGENT_KEY_PREVIOUS_SUFFIX = "_PREVIOUS"

# The opaque prefix every issued key carries, so an operator can tell a
# LibreRun agent key from a provider key at a glance — and so a provider
# key pasted into the wrong variable fails loudly instead of being
# forwarded somewhere.
KEY_VALUE_PREFIX = "lr_agent_"

# The gateway's secrets: the three provider credentials this process
# alone holds (L23, L28) and the two connection URLs that carry a
# password. Every one is a ``SecretStr`` below and every one reads a
# ``<NAME>_FILE`` companion, so a Docker, Podman or Kubernetes secret
# reaches the gateway with no wrapper (K2, L30).
FILE_BACKED_SECRETS = (
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GOOGLE_AI_API_KEY",
    "DATABASE_URL",
    "REDIS_URL",
    # K7 (D33): the key this process's own rows in the secrets store are
    # sealed with — the provider keys pasted in the admin UI and the
    # sealing keypair. Never the backend's, and delivered by gateway.env
    # or its _FILE alone, never by an `environment:` line.
    "LIBRERUN_GATEWAY_SECRETS_KEY",
)

# Nothing here, as in the backend since K8a moved its one entry onto the
# agent's declared tool secret: the gateway holds no secret it does not
# read through this model.
MASKED_ONLY_SECRETS: tuple[str, ...] = ()


class GatewaySettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ENV_FILES,
        extra="ignore",
        # See ``app.config``: assignment revalidates, so a test that
        # sets a provider key to a plain string still gets a masked
        # field back rather than replacing it with a bare one.
        validate_assignment=True,
    )

    @model_validator(mode="before")
    @classmethod
    def _read_file_secrets(cls, values):
        """``<NAME>_FILE`` → the field. Idempotent, because
        ``validate_assignment`` re-runs it on every assignment."""
        return secret_files.resolve(values, FILE_BACKED_SECRETS)

    # --- where the platform's state lives ---------------------------------
    DATABASE_URL: SecretStr = SecretStr(
        "postgresql+asyncpg://librerun:librerun_dev_pw@localhost:5432/librerun"
    )
    DATABASE_URL_FILE: str = ""
    REDIS_URL: SecretStr = SecretStr("redis://localhost:6379/0")
    REDIS_URL_FILE: str = ""

    # --- keyless mode ------------------------------------------------------
    # D4: ``stub`` becomes the provider for every step, so the demo and CI
    # complete real runs with no provider account anywhere.
    LIBRERUN_STUB_LLM: bool = False

    # --- the platform's own embedding step --------------------------------
    # ``provider/model``. The value when no ``kb.embed_model`` setting row
    # exists; never taken from any agent's llm.steps, because knowledge
    # search is the platform's feature, not the agent's.
    LIBRERUN_KB_EMBED_MODEL: str = "openai/text-embedding-3-small"

    # --- provider credentials (this process and no other) ------------------
    # Delivered by gateway.env: the env_file the gateway service loads
    # and no other service does, and the dotenv this model reads above
    # (K1, decision L28). Never from the root .env, which compose, the
    # backend and the frontend build all read. Each also takes a
    # ``<NAME>_FILE`` companion (K2, decision L30), for the deployments
    # whose secret store mounts a file rather than setting a variable.
    OPENAI_API_KEY: SecretStr = SecretStr("")
    OPENAI_API_KEY_FILE: str = ""
    ANTHROPIC_API_KEY: SecretStr = SecretStr("")
    ANTHROPIC_API_KEY_FILE: str = ""
    GOOGLE_AI_API_KEY: SecretStr = SecretStr("")
    GOOGLE_AI_API_KEY_FILE: str = ""
    # Not a secret: an endpoint, and one an operator needs to see in a
    # settings dump to debug a misrouted call.
    OPENAI_BASE_URL: str = ""

    # --- the gateway's own rows in the secrets store (K7; L33, D33) ------
    # A comma-separated list of Fernet keys, the first one current, exactly
    # the backend's LIBRERUN_BACKEND_SECRETS_KEY shape (app.secrets_keyring)
    # and never the same key: each row's key id is a keyed digest of the
    # key that sealed it, and the gateway refuses to boot when its id is on
    # another scope's row. Blank means unconfigured: no sealing keypair, no
    # public key published, and gateway.env serves the provider keys alone.
    LIBRERUN_GATEWAY_SECRETS_KEY: SecretStr = SecretStr("")
    LIBRERUN_GATEWAY_SECRETS_KEY_FILE: str = ""

    # --- agent keys --------------------------------------------------------
    # How long an admin rotation leaves the previous key working. An
    # env-provisioned previous key is not governed by this: it lives as
    # long as its _PREVIOUS variable does.
    #
    # Deliberately NOT spelled LIBRERUN_AGENT_KEY_*: every variable under
    # that prefix is read as one agent's key, and this one would invert
    # to an agent id (`grace-hours`) that passes the manifest pattern —
    # a setting quietly registering itself as an agent.
    LIBRERUN_KEY_ROTATION_GRACE_HOURS: int = 24

    # --- logging -----------------------------------------------------------
    # Every record is walked before any handler sees it (``gateway/logs.py``),
    # so this governs volume, not exposure.
    LOG_LEVEL: str = "INFO"

    # --- telemetry ---------------------------------------------------------
    OTEL_SERVICE_NAME: str = "librerun-gateway"
    OTEL_EXPORTER_OTLP_ENDPOINT: str = ""
    OTEL_EXPORTER_OTLP_PROTOCOL: str = "grpc"
    OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT: str = ""
    OTEL_DEBUG: bool = False

    # --- limits ------------------------------------------------------------
    # The bounds ``kb_search`` already imposes (``routers/mcp.py``),
    # repeated here because the gateway is what enforces them on the
    # embedding call: a kb-only agent must be unable to cause embedding
    # spend it could not already cause by searching. kb_search TRUNCATES
    # a long query; the gateway REFUSES it, because a truncating gateway
    # would silently embed something other than what was asked for.
    KB_EMBED_MAX_QUERIES: int = 20
    KB_EMBED_MAX_QUERY_CHARS: int = 1000


settings = GatewaySettings()


def reload_settings() -> GatewaySettings:
    """Re-read the environment. Tests use this after monkeypatching;
    production reads once at import."""
    global settings
    settings = GatewaySettings()
    return settings


def agent_key_variables(environ: dict | None = None) -> dict[str, str]:
    """Every ``LIBRERUN_AGENT_KEY_*`` variable in the environment, blanks
    dropped. Separated from the settings model because the names are not
    known in advance — they are one per installed agent."""
    env = os.environ if environ is None else environ
    return {
        name: value.strip()
        for name, value in env.items()
        if name.startswith(AGENT_KEY_PREFIX) and value and value.strip()
    }
