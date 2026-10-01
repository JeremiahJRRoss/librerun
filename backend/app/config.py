from pathlib import Path

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app import secret_files

# Install.md puts .env at the repository root, but dev mode runs uvicorn,
# alembic, and app.scripts.bootstrap_admin from backend/ — so the dotenv
# lookup must not depend on the current working directory. Anchor the root
# .env relative to this file (backend/app/config.py -> repo root). A .env
# in the CWD still wins (later entries override earlier ones), and real
# environment variables override both, so container deployments that pass
# env vars directly are unaffected. Missing files are skipped silently.
_REPO_ROOT_ENV = Path(__file__).resolve().parents[2] / ".env"


# The secret the tree ships with. Accepted only in demo mode (blueprint
# S3, decision L22): outside it the backend refuses to start on this value.
DEFAULT_APP_SECRET_KEY = "dev-secret-change-me"

# The variables that name a MODEL PROVIDER's credential. The backend
# declares none of them as settings and must not: they belong to the
# gateway process (L23). Named here so the guard and its test read from
# one list. An agent's own third-party secrets are not on it: since K8a
# they are declared in its manifest's secrets[] and read through the
# secrets capability (D20), which a provider key may never be (D35).
PROVIDER_KEY_VARIABLES = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_AI_API_KEY")

# The backend's secrets, and the only fields that read a ``<NAME>_FILE``
# companion (K blueprint K2, decision L30). Every one is a ``SecretStr``
# below, so a settings dump — a log line, a traceback, an admin
# response, ``print(settings)`` in a debugging session — prints
# ``**********`` and not the value (L31).
#
# ``DATABASE_URL`` and ``REDIS_URL`` are on this list because they carry
# a password in every deployment that sets one; the default in this file
# is a development URL and that is not a reason to treat the field as
# public.
FILE_BACKED_SECRETS = (
    "APP_SECRET_KEY",
    "DATABASE_URL",
    "REDIS_URL",
    "AZURE_CLIENT_SECRET",
    "INITIAL_ADMIN_PASSWORD",
    "INITIAL_USER_PASSWORD",
    "PINECONE_API_KEY",
    "LIBRERUN_BACKEND_SECRETS_KEY",
)

# Secrets masked with NO ``_FILE`` companion. None since K8a (L13): the
# demo agent's search key, the one entry, left this model for the
# agent's own declared tool secret, read through the secrets capability
# with the environment as its in-process fallback — a variable this
# model no longer names, so no _FILE spelling of it could reach the
# agent either.
MASKED_ONLY_SECRETS: tuple[str, ...] = ()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(_REPO_ROOT_ENV, ".env"),
        extra="ignore",
        case_sensitive=False,
        # Assignment goes through validation too, so a test or a script
        # that sets ``settings.APP_SECRET_KEY = "..."`` gets a
        # ``SecretStr`` rather than quietly replacing the masked field
        # with a bare string that the next dump would print.
        validate_assignment=True,
    )

    @model_validator(mode="before")
    @classmethod
    def _read_file_secrets(cls, values):
        """``<NAME>_FILE`` → the field, before anything else validates.

        Runs again on every assignment (``validate_assignment`` above),
        which is why ``secret_files.resolve`` is idempotent: with the
        file already read, the plain value equals the file's contents
        and there is nothing left to do.
        """
        return secret_files.resolve(values, FILE_BACKED_SECRETS)

    # Application
    APP_ENV: str = "development"
    APP_PORT: int = 8000
    APP_HOST: str = "0.0.0.0"
    APP_SECRET_KEY: SecretStr = SecretStr(DEFAULT_APP_SECRET_KEY)
    APP_SECRET_KEY_FILE: str = ""
    APP_CORS_ORIGINS: str = "http://localhost:3000"

    # Database
    DATABASE_URL: SecretStr = SecretStr(
        "postgresql+asyncpg://librerun:librerun_dev_pw@localhost:5432/librerun"
    )
    DATABASE_URL_FILE: str = ""
    DATABASE_POOL_SIZE: int = 20
    DATABASE_MAX_OVERFLOW: int = 10

    # Redis
    REDIS_URL: SecretStr = SecretStr("redis://localhost:6379/0")
    REDIS_URL_FILE: str = ""

    # Agents (blueprint B12). Where filesystem agent discovery looks for
    # agent packages: one directory, or several separated by the OS path
    # separator (``agents:agents/_examples`` — blueprint S3, the demo).
    # Empty = the ``agents/`` directory next to ``app/`` (``./agents`` in
    # the standard ``cd backend`` layout). A relative value resolves
    # against the process working directory. Installed (pip entry-point)
    # agents are discovered regardless of this path.
    LIBRERUN_AGENTS_PATH: str = ""
    # Where an agent keeps file-shaped state (K9-03). Read by name so the
    # deployment view can show it; the lifespan copies a value set in .env
    # into the process environment before discovery, since an agent reads
    # it from there and never from this model (main._export_state_dir).
    LIBRERUN_STATE_DIR: str = ""

    # Blueprint S4a: the model the PLATFORM uses to embed a knowledge
    # search query, as provider/model. The value when no
    # ``kb.embed_model`` setting row exists. The gateway is what calls
    # it — the backend makes no model call after S4a — but the default
    # lives here because the setting's default_factory reads it.
    LIBRERUN_KB_EMBED_MODEL: str = "openai/text-embedding-3-small"

    # Blueprint S4a: where the LLM gateway lives. Every model call an
    # in-process agent makes goes there over HTTP — never as a library,
    # because egress code in this process would keep provider
    # credentials in the one process every python-package agent shares.
    # The gateway is a DEFAULT compose service, so a local uvicorn needs
    # it running from ./compose.sh up -d.
    LIBRERUN_GATEWAY_URL: str = "http://localhost:8090"
    # Base URL at which agent CONTAINERS reach this chassis (blueprint
    # B13) — used to advertise the run-scoped MCP endpoint in the Run
    # Contract POST. In compose this is the backend service name; the
    # localhost default fits the dev layout.
    LIBRERUN_PUBLIC_URL: str = "http://localhost:8000"
    # Where the source of the running version can be fetched (K blueprint
    # A1, gate R17; AGPL-3.0 section 13). Blank means the default, as
    # LIBRERUN_AGENTS_PATH reads blank: /api/v1/meta derives it from the
    # public repository and the running version's tag, so no file carries
    # a version literal that a version bump could miss. An operator who
    # runs modified source sets where the source of THEIR version is.
    LIBRERUN_SOURCE_URL: str = ""
    # The ceiling on one phase invocation's wall-clock budget (blueprint
    # S4): a manifest's ``phases[].deadline_seconds`` applies under it,
    # and a phase that declares none gets exactly this. Containers get the
    # resolved value in the POST body; their run token outlives it by a
    # minute at most.
    LIBRERUN_MAX_PHASE_SECONDS: int = 3600
    # Queue-only logging (blueprint S4): every log record is walked on a
    # listener thread before any sink sees it, stdout/stderr and the raw
    # descriptors are captured, and no handler can be attached that
    # bypasses the walk. False = the synchronous handler pipeline, which
    # the test suite uses so pytest's own capture handlers keep working.
    LOG_QUEUE_ONLY: bool = True
    # Keyless pipeline mode for CI (blueprint B14). When true, the llm
    # capability reports stub mode and agents answer from canned
    # per-step fixtures instead of calling a provider, so the smoke
    # workflow can drive a REAL run to completion with no API keys.
    # Never enable it where a real answer is expected: reports produced
    # this way are fixtures, and say so in their text.
    LIBRERUN_STUB_LLM: bool = False
    # Demo mode (blueprint S3, decision L22): the zero-config local demo
    # scripts/demo.sh boots. The only mode in which the shipped default
    # APP_SECRET_KEY is accepted, announced by a banner at startup and in
    # the UI. Never a production setting.
    LIBRERUN_DEMO: bool = False

    # LLM — nothing. The provider credentials live in the gateway
    # process and nowhere else (blueprint S4a, L23), and DECLARING them
    # here was enough to break that on its own: this model reads the
    # repository-root .env, which is where an operator's keys are, so
    # the documented `cd backend && uvicorn` dev mode materialised the
    # real values onto ``settings`` — where any in-process
    # ``python-package`` agent could read them by importing
    # ``app.config`` (Codex P1). Nothing in the backend ever used the
    # fields; they only bound the secret into the one process every
    # in-process agent shares. ``extra="ignore"`` means the .env lines
    # are now read past rather than bound. ``PROVIDER_KEY_VARIABLES``
    # below is what keeps this true.

    # Vector store
    PINECONE_API_KEY: SecretStr = SecretStr("")
    PINECONE_API_KEY_FILE: str = ""
    PINECONE_ENVIRONMENT: str = ""
    PINECONE_INDEX_NAME: str = "librerun-kb"

    # Observability — vendor-neutral OTEL (see app/observability/otel_init.py).
    # Trace export activates when OTEL_EXPORTER_OTLP_ENDPOINT is set (blank =
    # clean no-op boot). Protocol: "grpc" (4317) or "http/protobuf" (4318).
    OTEL_EXPORTER_OTLP_ENDPOINT: str = ""
    OTEL_EXPORTER_OTLP_PROTOCOL: str = "grpc"
    OTEL_SERVICE_NAME: str = "librerun-backend"
    # When True, stream every finished span to stderr via ConsoleSpanExporter
    # (works with or without an OTLP endpoint). Verbose — leave off in prod.
    OTEL_DEBUG: bool = False
    # GenAI content capture — whether LLM prompt/completion text is recorded
    # onto run-plane spans/events. util-genai enum: NO_CONTENT / SPAN_ONLY /
    # EVENT_ONLY / SPAN_AND_EVENT (legacy true/false accepted and
    # normalized). Blank = keep the default, SPAN_AND_EVENT (full capture).
    # The instrumentations read the variable from os.environ, which .env
    # alone never reaches — otel_init threads this Settings value into the
    # real environment before any instrumentor runs, which is what makes
    # the opt-out reachable from .env in both run modes.
    OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT: str = ""

    # Browser (UX-plane) telemetry relay — see docs/platform/Browser_Observability.md
    # and app/routers/ux_telemetry.py. The relay is authenticated and
    # same-origin; emission reuses OTEL_EXPORTER_OTLP_ENDPOINT above (blank
    # endpoint = records validate and drop cleanly). Setting ENABLED=false
    # answers 410 and browsers stop sending for the rest of their session —
    # a runtime kill switch that needs no frontend rebuild. Quota units are
    # max(records, KiB) per request, counted per minute against the
    # VERIFIED principal (never payload fields).
    UX_TELEMETRY_ENABLED: bool = True
    UX_TELEMETRY_USER_UNITS_PER_MINUTE: int = 600
    UX_TELEMETRY_TENANT_UNITS_PER_MINUTE: int = 6000
    UX_TELEMETRY_GLOBAL_UNITS_PER_MINUTE: int = 60000

    # Which observability vendor overlay Vector and the otel-bridge were
    # started with (blueprint S7a, decision L26): "datadog", "elastic",
    # "splunk", or blank for none — the shipped default, where nothing
    # leaves the box but what the operator already configured. The
    # backend does NOT forward anything itself and holds none of the
    # vendor's credentials (those are mounted on vector and otel-bridge
    # alone, from observability.env); it carries the NAME so
    # /admin/otel-status can tell an operator where telemetry is going.
    # An unrecognised value is reported as unsupported rather than
    # guessed at — Vector and the bridge refuse to start on one, because
    # it names a config file that does not exist.
    LIBRERUN_OBS_VENDOR: str = ""

    # Pluggable trace viewer (see app/observability/trace_viewer.py).
    # Presets: jaeger (default — the compose `viewer` profile), phoenix,
    # tempo, langsmith (template required), custom (template required),
    # off. The base URL is opened by the OPERATOR'S BROWSER, hence
    # localhost rather than a compose service name.
    # "off" until a viewer is really there (blueprint S3, Codex on PR #51):
    # the run pages render "View trace" whenever a link can be built, and
    # a link to an absent viewer is worse than none. The demo sets jaeger
    # because it starts one; a hand-configured deployment sets it with
    # the viewer profile (VECTOR_VIEWER=1).
    TRACE_VIEWER: str = "off"
    TRACE_VIEWER_BASE_URL: str = "http://localhost:16686"
    TRACE_VIEWER_URL_TEMPLATE: str = ""
    # The tenant whose admins are the PLATFORM operators — bootstrap
    # credentials land in this tenant (schema seeds slug 'dev'). Only its
    # admins may edit application-global runtime settings (/admin/settings);
    # other tenants' admins administer their tenant, not the deployment.
    PLATFORM_TENANT_SLUG: str = "dev"


    # Google OAuth. The client id is the whole of it: Google sign-in
    # verifies the ID token against the client id alone, so there is no
    # client secret to hold (K6-05, L28). The id is the default of the
    # `auth.google_client_id` setting, which Admin -> Settings overrides.
    GOOGLE_CLIENT_ID: str = ""

    # Microsoft Entra. Each is the default of an `auth.*` setting the admin
    # page overrides (K6); the secret's is `auth.azure_client_secret`, held
    # encrypted in the secrets store, with this value its fallback (L29).
    AZURE_CLIENT_ID: str = ""
    AZURE_CLIENT_SECRET: SecretStr = SecretStr("")
    AZURE_CLIENT_SECRET_FILE: str = ""
    AZURE_TENANT_ID: str = ""

    # The encrypted secrets store's key (K6; D14, D33): a comma-separated
    # list of Fernet keys, the first one current, the rest still opening
    # the rows they sealed until the rewrap script moves them on. Blank
    # means "unconfigured" in every mode — a secret write answers 503 and
    # each secret setting reads its environment fallback — and a malformed
    # entry stops the backend at boot, naming its position.
    LIBRERUN_BACKEND_SECRETS_KEY: SecretStr = SecretStr("")
    LIBRERUN_BACKEND_SECRETS_KEY_FILE: str = ""

    # Auth
    CREDENTIALS_ENABLED: bool = True
    JWT_EXPIRY_HOURS: int = 24
    BCRYPT_ROUNDS: int = 12

    # Initial credentials-auth users seeded by ``app.scripts.bootstrap_admin``
    # at container startup. Both pairs are independent — leave either email
    # OR password blank to skip that role. ``.env`` is the source of truth:
    # the bootstrap always overwrites the password hash on every boot, so
    # rotating a password is just a ``.env`` edit + container restart.
    INITIAL_ADMIN_EMAIL: str = ""
    INITIAL_ADMIN_PASSWORD: SecretStr = SecretStr("")
    INITIAL_ADMIN_PASSWORD_FILE: str = ""
    INITIAL_USER_EMAIL: str = ""
    INITIAL_USER_PASSWORD: SecretStr = SecretStr("")
    INITIAL_USER_PASSWORD_FILE: str = ""

    # PII
    PII_CONFIDENCE_THRESHOLD: float = 0.7
    # The operator's explicit opt-out from failing closed when the
    # named-entity detector is unavailable (blueprint S4c, gap H15).
    #
    # Default false, and the default is the point: stage 3 of the
    # redaction pipeline is the only stage that finds a person, a place
    # or an organisation, so a missing spaCy model used to delete those
    # classes from every persist and export point silently. With this
    # false the chassis refuses instead — 503 at intake, the preview and
    # the upload, a refusal at the run boundary and the MCP `redact`
    # tool, a stripped span or a dropped record on the way out.
    #
    # Setting it true restores the regex-only pass-through for a
    # deployment that knowingly accepts it. It is not free of trace: the
    # startup line says it, every span and record it touches carries
    # `librerun.pii.degraded=true`, and the tenant it served that way
    # gets a `pii_detector_degraded` audit row.
    #
    # Both processes read it: the gateway image ships `backend/app` and
    # runs this same `pii_service`, so a deployment that sets it for one
    # and not the other has two different policies (docs/platform/Install.md).
    LIBRERUN_PII_ALLOW_DEGRADED: bool = False
    # The region the walker's number rule parses a bare number as a
    # national phone number of (blueprint S4): an ISO 3166-1 alpha-2 code.
    PII_PHONE_REGION: str = "US"

    # Files
    FILE_STORAGE_BACKEND: str = "local"
    FILE_STORAGE_PATH: str = "./data/files"

    # Logging
    LOG_LEVEL: str = "INFO"
    LOG_FORMAT: str = "json"
    LOG_FILE_PATH: str = "./data/logs/backend.jsonl"
    LOG_FILE_ROTATION_WHEN: str = "midnight"
    LOG_FILE_BACKUP_COUNT: int = 14
    LOG_REDACT_PII: bool = True
    LOG_STDERR_ENABLED: bool = True

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.APP_CORS_ORIGINS.split(",") if o.strip()]


settings = Settings()
