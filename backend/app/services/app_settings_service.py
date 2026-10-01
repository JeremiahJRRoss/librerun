"""Runtime app-settings service.

Reads effective settings by merging the DB ``app_settings`` table on top of
``.env``/``config.py`` defaults. Values are cached in Redis with a short TTL
so per-request lookups are cheap; writes invalidate the cache.

Setting values are validated against a small registry (``SETTING_SPECS``)
that defines the expected type, default, and human-readable description.

A setting of type ``secret`` (K6, L31) is the exception to all of the
above: its value lives encrypted in the ``secrets`` table
(``app.services.secrets_service``), never in ``app_settings`` and never in
the Redis cache; ``get_setting`` refuses one; the API shows where it comes
from and a fingerprint, never the value; and its default is its
environment variable, read by ``get_secret_setting`` where it is used and
bound nowhere else (L29).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import config as _config
from app import secret_files
from app.config import settings as env_settings
from app.models import AppSetting, User
from app.redis import get_redis
from app.services import secrets_service

CACHE_TTL_SECONDS = 60
CACHE_PREFIX = "app_settings:"
CORS_RESTART_FLAG_KEY = "app_settings:cors_restart_required"


def _coerce_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def _coerce_int(v: Any) -> int:
    return int(v)


def _coerce_string_list(v: Any) -> list[str]:
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    if isinstance(v, str):
        return [s.strip() for s in v.split(",") if s.strip()]
    raise ValueError(f"expected list or comma-separated string, got {type(v).__name__}")


def _coerce_string(v: Any) -> str:
    return str(v)


# The longest secret a setting takes. An OAuth client secret is under a
# hundred characters; the ceiling is there so a pasted file is refused
# rather than sealed.
SECRET_MAX_LENGTH = 4096


def _coerce_secret(key: str, v: Any) -> str:
    """A non-blank string, stripped. Every refusal names the setting and
    never the value — ``str(v)`` of a secret in a 400 is the leak (L31)."""
    if not isinstance(v, str):
        raise ValueError(f"{key} takes a string")
    text = v.strip()
    if not text:
        raise ValueError(f"{key} cannot be set to a blank value; clear it instead")
    if len(text) > SECRET_MAX_LENGTH:
        raise ValueError(f"{key} is longer than {SECRET_MAX_LENGTH} characters")
    return text


@dataclass(frozen=True)
class SettingSpec:
    key: str
    value_type: str  # one of: "string_list", "int", "bool", "string", "secret"
    description: str
    default_factory: Callable[[], Any]
    # Optional constraints for "string" settings. ``choices`` restricts the
    # value to a fixed lowercase set; ``validator`` raises ValueError for
    # anything else the type coercion alone can't rule out. The admin PUT
    # handler maps ValueError to HTTP 400, so both surface to the UI as
    # validation errors rather than stored garbage.
    choices: tuple[str, ...] | None = None
    validator: Callable[[Any], None] | None = None
    # A "secret"'s environment fallback: the settings-model field (and
    # variable) the value comes from while the store holds no row (L29).
    # Read by ``get_secret_setting`` alone, so the value is never bound
    # into a spec, the Redis cache or a response.
    env_var: str | None = None

    @property
    def default(self) -> Any:
        return self.default_factory()

    def coerce(self, value: Any) -> Any:
        if self.value_type == "secret":
            return _coerce_secret(self.key, value)
        if self.value_type == "bool":
            coerced: Any = _coerce_bool(value)
        elif self.value_type == "int":
            coerced = _coerce_int(value)
        elif self.value_type == "string_list":
            coerced = _coerce_string_list(value)
        else:
            coerced = _coerce_string(value)
        if self.choices is not None:
            coerced = str(coerced).strip().lower()
            if coerced not in self.choices:
                raise ValueError(
                    f"{self.key} must be one of: {', '.join(self.choices)}"
                )
        if self.validator is not None:
            self.validator(coerced)
        return coerced



def _validate_viewer_base_url(v: Any) -> None:
    """Blank is allowed (a template may not use {base}); anything else must
    be an absolute http(s) URL — it is opened by the operator's browser."""
    text = str(v).strip()
    if text and not (text.startswith("http://") or text.startswith("https://")):
        raise ValueError(
            "trace_viewer_base_url must be blank or start with http:// or https://"
        )


def _validate_viewer_template(v: Any) -> None:
    """Blank is allowed (presets carry built-in templates); a non-blank
    template must reference {trace_id} or every run would link to the same
    page."""
    text = str(v).strip()
    if text and "{trace_id}" not in text:
        raise ValueError(
            "trace_viewer_url_template must contain {trace_id} (and may use {base})"
        )


def _validate_provider_model(value: str) -> None:
    """``provider/model`` — both halves present, one separator.

    A bare model name would leave the gateway guessing which provider to
    bill, and guessing is how a tenant's spend ends up on the wrong
    account.
    """
    provider, sep, model = str(value).partition("/")
    if not sep or not provider.strip() or not model.strip():
        raise ValueError(
            "expected provider/model, e.g. openai/text-embedding-3-small"
        )
    if "/" in model and not model.strip("/"):
        raise ValueError("expected provider/model, e.g. openai/text-embedding-3-small")


SETTING_SPECS: dict[str, SettingSpec] = {
    "cors_origins": SettingSpec(
        key="cors_origins",
        value_type="string_list",
        description=(
            "Allowed CORS origins for the API. Editing this value sets a "
            "restart-required flag; a rolling restart is needed for the new "
            "list to take effect."
        ),
        default_factory=lambda: env_settings.cors_origins_list,
    ),
    "session_timeout_minutes": SettingSpec(
        key="session_timeout_minutes",
        value_type="int",
        description="Session/JWT expiry window in minutes.",
        default_factory=lambda: env_settings.JWT_EXPIRY_HOURS * 60,
    ),
    "max_upload_size_mb": SettingSpec(
        key="max_upload_size_mb",
        value_type="int",
        description="Maximum accepted upload size per file, in megabytes.",
        default_factory=lambda: 50,
    ),
    "require_approval_before_phase2": SettingSpec(
        key="require_approval_before_phase2",
        value_type="bool",
        description="Require explicit operator approval before entering pipeline phase 2.",
        default_factory=lambda: True,
    ),
    "default_llm_provider": SettingSpec(
        key="default_llm_provider",
        value_type="string",
        description="Fallback LLM provider when a pipeline step does not specify one.",
        default_factory=lambda: "openai",
    ),
    # Trace-viewer deep links (app/observability/trace_viewer.py). These are
    # the presentation half of tracing — where the "View trace" link points —
    # and are safe to change at runtime: they are read per request, a wrong
    # value costs a dead link, never data. The emission half (OTLP endpoint,
    # protocol, content capture) is deliberately NOT here: exporters are
    # wired once at boot, and content capture is a security posture that
    # should change via reviewed deployment, not a web toggle.
    "trace_viewer": SettingSpec(
        key="trace_viewer",
        value_type="string",
        description=(
            "Trace viewer preset for run-page deep links: jaeger, phoenix, "
            "tempo, langsmith, custom, or off. langsmith and custom require "
            "trace_viewer_url_template. Applies immediately."
        ),
        default_factory=lambda: (env_settings.TRACE_VIEWER or "off"),
        choices=("jaeger", "phoenix", "tempo", "langsmith", "custom", "off"),
    ),
    "trace_viewer_base_url": SettingSpec(
        key="trace_viewer_base_url",
        value_type="string",
        description=(
            "Viewer UI base URL, opened by the operator's browser (hence "
            "usually localhost, not a compose service name). Used as {base} "
            "in templates."
        ),
        default_factory=lambda: env_settings.TRACE_VIEWER_BASE_URL,
        validator=_validate_viewer_base_url,
    ),
    "trace_viewer_url_template": SettingSpec(
        key="trace_viewer_url_template",
        value_type="string",
        description=(
            "Overrides the preset's URL shape; may use {base} and "
            "{trace_id}. Required for the langsmith and custom presets."
        ),
        default_factory=lambda: env_settings.TRACE_VIEWER_URL_TEMPLATE,
        validator=_validate_viewer_template,
    ),
    # The one model the PLATFORM routes (blueprint S4a): the query
    # embedding behind knowledge search. It is here rather than in an
    # agent's llm.steps because knowledge search is the platform's
    # feature — an agent that could route it could spend the platform's
    # budget on a model of its choosing. The gateway reads this row at
    # request time, so an edit takes effect on the next kb_embed call
    # with nothing restarted.
    "kb.embed_model": SettingSpec(
        key="kb.embed_model",
        value_type="string",
        description=(
            "Embedding model for knowledge-base search, as provider/model "
            "(e.g. openai/text-embedding-3-small). Read by the LLM gateway "
            "at request time; applies immediately. Ignored while the "
            "gateway is in keyless (stub) mode."
        ),
        default_factory=lambda: env_settings.LIBRERUN_KB_EMBED_MODEL,
        validator=_validate_provider_model,
    ),
    # The knowledge base's vector-store key (K8a, D36): the platform's,
    # since knowledge search is the platform's feature — ``KbCapability``
    # uses it for every agent granted ``kb``, so it is a platform admin's to
    # set, and no agent declares it. Sealed into the secrets store; the
    # environment's PINECONE_API_KEY, or its _FILE (K2), serves while no
    # row is set.
    "kb.pinecone_api_key": SettingSpec(
        key="kb.pinecone_api_key",
        value_type="secret",
        env_var="PINECONE_API_KEY",
        description=(
            "Pinecone API key for knowledge-base search. Write-only: stored "
            "encrypted in the secrets store and never shown again, only its "
            "fingerprint. While it is not set here, the environment's "
            "PINECONE_API_KEY applies. Applies to the next search."
        ),
        default_factory=lambda: None,
    ),
    # Sign-in (K6). Read by routers/auth.py on every sign-in, so an edit
    # applies to the next one with nothing restarted. The defaults read
    # ``app.config.settings`` when asked rather than the object this module
    # imported, so they follow whatever settings object is current.
    "auth.google_client_id": SettingSpec(
        key="auth.google_client_id",
        value_type="string",
        description=(
            "Google OAuth client id for Google sign-in. Not a secret: Google "
            "sign-in verifies the ID token against the client id alone. "
            "Applies to the next sign-in."
        ),
        default_factory=lambda: _config.settings.GOOGLE_CLIENT_ID,
    ),
    "auth.azure_client_id": SettingSpec(
        key="auth.azure_client_id",
        value_type="string",
        description=(
            "Microsoft Entra ID application (client) id for Microsoft "
            "sign-in. Applies to the next sign-in."
        ),
        default_factory=lambda: _config.settings.AZURE_CLIENT_ID,
    ),
    "auth.azure_tenant_id": SettingSpec(
        key="auth.azure_tenant_id",
        value_type="string",
        description=(
            "Microsoft Entra ID tenant: its UUID for a single-tenant "
            "application, or common for a multi-tenant one (blank means "
            "common). Applies to the next sign-in."
        ),
        default_factory=lambda: _config.settings.AZURE_TENANT_ID,
    ),
    "auth.azure_client_secret": SettingSpec(
        key="auth.azure_client_secret",
        value_type="secret",
        env_var="AZURE_CLIENT_SECRET",
        description=(
            "Microsoft Entra ID client secret for Microsoft sign-in. "
            "Write-only: stored encrypted in the secrets store and never "
            "shown again, only its fingerprint. While it is not set here, "
            "the environment's AZURE_CLIENT_SECRET applies. Applies to the "
            "next sign-in."
        ),
        default_factory=lambda: None,
    ),
}


def list_specs() -> list[SettingSpec]:
    return list(SETTING_SPECS.values())


def get_spec(key: str) -> SettingSpec:
    spec = SETTING_SPECS.get(key)
    if spec is None:
        raise KeyError(f"unknown setting: {key}")
    return spec


async def _cache_get(key: str) -> Any | None:
    redis = await get_redis()
    raw = await redis.get(f"{CACHE_PREFIX}{key}")
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


async def _cache_set(key: str, value: Any) -> None:
    redis = await get_redis()
    await redis.set(f"{CACHE_PREFIX}{key}", json.dumps(value), ex=CACHE_TTL_SECONDS)


async def _cache_invalidate(key: str) -> None:
    redis = await get_redis()
    await redis.delete(f"{CACHE_PREFIX}{key}")


# Cached in place of a value when the DB has no override row. Defaults are
# NEVER cached: they derive from live ``env_settings`` (default_factory
# closures), and caching a computed default would freeze it for the TTL —
# wrong the moment anything (a test fixture, a config reload) swaps the
# settings object, and pure loss in production where env can't change
# without a restart anyway. The sentinel still spares the DB round-trip.
_NO_OVERRIDE = "__no_override__"


def _refuse_secret(spec: SettingSpec, instead: str) -> None:
    """The plain paths below cache in Redis and store in ``app_settings``,
    and a secret may reach neither (L31)."""
    if spec.value_type == "secret":
        raise TypeError(f"{spec.key} is a secret setting: use {instead}")


async def get_setting(db: AsyncSession, key: str) -> Any:
    """Return the effective value for ``key`` (DB override or live default)."""
    spec = get_spec(key)
    _refuse_secret(spec, "get_secret_setting, which never caches it in Redis")
    cached = await _cache_get(key)
    if cached == _NO_OVERRIDE:
        return spec.coerce(spec.default)
    if cached is not None:
        return cached
    row = await db.get(AppSetting, key)
    if row is None:
        await _cache_set(key, _NO_OVERRIDE)
        return spec.coerce(spec.default)
    value = spec.coerce(row.value)
    await _cache_set(key, value)
    return value


async def set_setting(
    db: AsyncSession, key: str, value: Any, user: User
) -> AppSetting:
    spec = get_spec(key)
    _refuse_secret(spec, "set_secret_setting, which stores it encrypted")
    coerced = spec.coerce(value)
    row = await db.get(AppSetting, key)
    if row is None:
        row = AppSetting(key=key, value=coerced, updated_by=user.id)
        db.add(row)
    else:
        row.value = coerced
        row.updated_by = user.id
    await db.flush()
    await _cache_invalidate(key)
    if key == "cors_origins":
        await _set_cors_restart_required()
    return row


async def reset_setting(db: AsyncSession, key: str) -> None:
    _refuse_secret(get_spec(key), "clear_secret_setting")  # validates
    row = await db.get(AppSetting, key)
    if row is not None:
        await db.delete(row)
        await db.flush()
    await _cache_invalidate(key)
    if key == "cors_origins":
        await _set_cors_restart_required()


# ----- secret settings (K6) ---------------------------------------------------


def _secret_spec(key: str) -> SettingSpec:
    spec = get_spec(key)
    if spec.value_type != "secret":
        raise TypeError(f"{key} is not a secret setting")
    return spec


def _environment_value(spec: SettingSpec) -> str:
    """The fallback, read where it is used and bound nowhere (L29). Through
    the settings model, so a ``<NAME>_FILE`` delivery counts."""
    value = secret_files.reveal(getattr(_config.settings, spec.env_var or "", None))
    return value if value.strip() else ""


@dataclass(frozen=True)
class SecretState:
    """Everything the API shows of a secret setting, which is not its value.

    ``source``: ``runtime`` (the store holds a row a configured key opens),
    ``unreadable`` (it holds one no configured key opens — the environment
    serves meanwhile), ``env`` (no row, and the environment has a value) or
    ``unset``. ``set`` is whether the store holds a row, so ``updated_at``
    and ``updated_by`` describe that row. ``fingerprint`` is null unless
    ``runtime``.
    """

    set: bool
    source: str
    fingerprint: str | None
    updated_at: datetime | None
    updated_by: UUID | None


def secret_state(spec: SettingSpec, meta: "secrets_service.SecretMeta | None") -> SecretState:
    if meta is not None:
        return SecretState(
            set=True,
            source="runtime" if meta.readable else "unreadable",
            fingerprint=meta.fingerprint if meta.readable else None,
            updated_at=meta.updated_at,
            updated_by=meta.updated_by,
        )
    return SecretState(
        set=False,
        source="env" if _environment_value(spec) else "unset",
        fingerprint=None,
        updated_at=None,
        updated_by=None,
    )


# What ``get_secret_setting`` last resolved per key, in this process: for
# the one synchronous reader, ``last_secret_setting`` (K8a). Values stay in
# this process, as the store's own cache's do (L31).
_LAST_RESOLVED: dict[str, str] = {}


async def get_secret_setting(db: AsyncSession, key: str) -> str:
    """The value in effect for a secret setting: the store's row, else the
    environment, else ``""`` (L29). For the code that uses it — never for a
    response, a log line or a cache but the store's own."""
    spec = _secret_spec(key)
    value = await secrets_service.get_secret(
        db, secrets_service.PLATFORM, key, env=lambda: _environment_value(spec)
    )
    _LAST_RESOLVED[key] = value
    return value


def last_secret_setting(key: str) -> str:
    """What ``get_secret_setting`` last resolved for ``key`` in this
    process, else the environment's value — for a caller that cannot
    await, such as ``KbCapability.available()``. Its callers refresh it
    where they can: a search reads the setting afresh, and the runner reads
    it once before a phase granted ``kb`` (K8a, D36)."""
    spec = _secret_spec(key)
    if key in _LAST_RESOLVED:
        return _LAST_RESOLVED[key]
    return _environment_value(spec)


async def set_secret_setting(
    db: AsyncSession, key: str, value: Any, user: User
) -> "secrets_service.SecretWrite":
    """Coerce, then seal it into the store. Raises ``ValueError`` naming the
    setting (never the value) and ``SecretsStoreUnconfigured`` with no key.
    The caller commits, then calls ``notify_secret_setting``."""
    spec = _secret_spec(key)
    return await secrets_service.set_secret(
        db, secrets_service.PLATFORM, key, spec.coerce(value), user_id=user.id
    )


async def clear_secret_setting(db: AsyncSession, key: str) -> bool:
    """Remove the store's row, so the environment applies again. The caller
    commits, then calls ``notify_secret_setting``."""
    _secret_spec(key)
    return await secrets_service.unset_secret(db, secrets_service.PLATFORM, key)


async def notify_secret_setting(key: str) -> None:
    """Tell every process a secret setting changed: the route's call once
    the write and its audit are committed (``secrets_service.notify_change``)."""
    _secret_spec(key)
    await secrets_service.notify_change(secrets_service.PLATFORM, key)


async def get_secret_state(db: AsyncSession, key: str) -> SecretState:
    spec = _secret_spec(key)
    metas = await secrets_service.list_secrets(db, secrets_service.PLATFORM)
    return secret_state(spec, next((m for m in metas if m.name == key), None))


@dataclass
class EffectiveSetting:
    key: str
    value: Any
    default_value: Any
    value_type: str
    description: str
    is_default: bool
    updated_at: datetime | None
    updated_by: UUID | None
    # A secret's state; None for every other type.
    secret: SecretState | None = None


async def get_all_settings(db: AsyncSession) -> list[EffectiveSetting]:
    """Return every registered setting with its current effective value.

    A secret setting carries its ``SecretState`` and no value: its row is
    read from ``secrets`` without the ciphertext (``list_secrets``), since
    ``app_settings`` never holds one.
    """
    rows = (await db.execute(select(AppSetting))).scalars().all()
    overrides = {r.key: r for r in rows}
    secret_rows: dict[str, secrets_service.SecretMeta] = {}
    if any(spec.value_type == "secret" for spec in SETTING_SPECS.values()):
        secret_rows = {
            meta.name: meta
            for meta in await secrets_service.list_secrets(db, secrets_service.PLATFORM)
        }
    out: list[EffectiveSetting] = []
    for spec in SETTING_SPECS.values():
        if spec.value_type == "secret":
            state = secret_state(spec, secret_rows.get(spec.key))
            out.append(
                EffectiveSetting(
                    key=spec.key,
                    value=None,
                    default_value=None,
                    value_type=spec.value_type,
                    description=spec.description,
                    is_default=not state.set,
                    updated_at=state.updated_at,
                    updated_by=state.updated_by,
                    secret=state,
                )
            )
            continue
        row = overrides.get(spec.key)
        value = spec.coerce(row.value) if row is not None else spec.default
        out.append(
            EffectiveSetting(
                key=spec.key,
                value=value,
                default_value=spec.default,
                value_type=spec.value_type,
                description=spec.description,
                is_default=row is None,
                updated_at=row.updated_at if row else None,
                updated_by=row.updated_by if row else None,
            )
        )
    return out


# ----- CORS restart signalling -------------------------------------------------

async def _set_cors_restart_required() -> None:
    redis = await get_redis()
    await redis.set(CORS_RESTART_FLAG_KEY, "1")


async def cors_restart_required() -> bool:
    redis = await get_redis()
    return bool(await redis.get(CORS_RESTART_FLAG_KEY))


async def clear_cors_restart_flag() -> None:
    redis = await get_redis()
    await redis.delete(CORS_RESTART_FLAG_KEY)
