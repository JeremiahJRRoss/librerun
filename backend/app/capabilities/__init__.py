"""The capability layer (blueprint B13, locked decision L11(c)).

Agents reach platform services through ONE narrow, granted surface
instead of importing chassis internals. The six capabilities:

- ``llm``        — provider credentials (the chassis owns the keys;
                   agents own their provider clients and prompts).
- ``kb``         — internal knowledge-base search (tenant-scoped
                   Pinecone query; moved here from the demo agent in B13).
- ``run_store`` — run-scoped key-value store (Redis-backed) for
                   whatever intermediate state an agent wants to keep.
- ``progress``   — live step progress for the run page, plus the
                   orchestrator factory for step timing/spans.
- ``audit``      — tenant-scoped audit rows (LLM schema drift etc.),
                   attributed to the run's owner by the chassis.
- ``pii``        — the intake PII pipeline on demand (blueprint S4):
                   ``redact(text)`` strips data an agent fetched at
                   runtime the way intake strips what a user typed.

Every argument an agent hands a capability that the chassis persists is
walked at the boundary (``app.services.run_boundary``): free text is
redacted, keys and numbers are checked and a flagged one refuses the
write with ``PiiRefused`` — ``pii_in_audit``, ``pii_in_store``,
``pii_in_progress`` — naming the argument and the path, never the value.

Beside the six sits ``config`` (K5a, L32): the run's own configuration —
its declared LLM steps and settings with this tenant's values — which is
the agent's data, not a platform service, so it is no capability, is in
no grant list and needs none (MCP: ``config_get``). ``secrets`` (K8a, D20)
sits beside it on the same terms: the tool secrets the manifest declares
in ``secrets[]``, this tenant's values, by name (MCP: ``secret_get``).

The manifest ``capabilities:`` list is the grant: accessing an
ungranted member raises ``CapabilityNotGranted``. In-process agents get
a ``Capabilities`` instance on their ``AgentInput`` (the fast path);
container agents consume ``kb``/``run_store``/``audit``/``pii`` through
the run-scoped MCP server (``app/routers/mcp.py``) authenticated by their
Run Contract bearer token. ``progress`` stays on the run contract's SSE
stream — agent→platform events are not tool-shaped. ``llm`` is
in-process-only: container agents hold their own keys by design.
"""
from __future__ import annotations

import json
import time as _time
from typing import Any
from uuid import UUID

import structlog

logger = structlog.get_logger(__name__)

# The one failure an agent sees from a model call, and the name it has
# for it: the agent asks the capability, so it should not have to import
# a chassis service to catch what the capability raises. Carries the
# gateway's own code (``unknown_step``, ``pii_in_identifier``,
# ``gateway_unreachable``, …) and its HTTP status, which is how a caller
# tells a refusal it should not retry from a failure it should.
from app.services.gateway_client import GatewayError as LlmError  # noqa: E402

# What ``caps.secrets.get`` raises (K8a): a name the manifest does not
# declare, and a declared name with no value in this tenant. Each carries
# the name, never a value, and is the agent's to catch — an absent search
# key is "no web results", not a failed run.
from app.services.tool_secrets_service import (  # noqa: E402
    SecretNotDeclared,
    SecretNotSet,
)

KNOWN_CAPABILITIES = ("llm", "kb", "run_store", "progress", "audit", "pii")

# Pre-S1 grant spellings, accepted for one release (blueprint S1, L18) and
# removed at v1.1: a manifest that grants ``case_store`` gets ``run_store``.
CAPABILITY_ALIASES = {"case_store": "run_store"}


def normalize_grants(grants) -> list[str]:
    """Map deprecated grant spellings to the current ones, in order,
    without duplicates."""
    out: list[str] = []
    for g in grants:
        name = CAPABILITY_ALIASES.get(g, g)
        if name not in out:
            out.append(name)
    return out


class CapabilityNotGranted(RuntimeError):
    """The agent's manifest does not grant this capability."""

    def __init__(self, name: str, grants: frozenset[str]):
        super().__init__(
            f"capability {name!r} is not granted by this agent's manifest "
            f"(granted: {sorted(grants) or '[]'}) — add it to the manifest's "
            f"capabilities: list"
        )


class LlmCapability:
    """Model calls, through the gateway (blueprint S4a, L23).

    The agent names a **step** and hands over messages; the platform
    decides which model answers it, redacts what the model will read,
    pays for it and records the span. What the agent does *not* get is a
    provider credential: this object used to hand one out, and a
    credential an in-process agent can read is a credential every
    in-process agent has, because they all share this process. So the
    gateway holds them, over HTTP, and ``credential()`` is gone.

    Keyless mode is no longer the agent's business either. It used to
    ask ``stub_mode()`` and answer from its own fixtures; now the
    gateway answers, so a keyless run exercises the real pipeline — the
    redaction, the span, the token counts, the cost — instead of
    stepping around it.
    """

    def __init__(self, run_token: str | None = None, seconds_left=None):
        self._run_token = run_token
        # A CALLABLE, not a number: the budget shrinks as the invocation
        # runs, and a value snapshotted when the façade was built would
        # tell the last step of a phase it had the whole phase left.
        self._seconds_left = seconds_left
        # Answered once per façade (see ``stub_mode``).
        self._stub_mode: bool | None = None

    def _budget(self) -> float | None:
        return self._seconds_left() if self._seconds_left else None

    async def stub_mode(self) -> bool:
        """True when the platform is running keyless (blueprint B14).

        Kept as a *statement of fact* an agent may want to put in its
        output — "this report came from fixtures" — not as a branch an
        agent should take instead of calling: ``complete`` works keyless
        and is the path that produces a span and a cost.

        It asks the GATEWAY, over ``/healthz``, because the gateway is
        the process that decides. It used to read this process's own
        ``settings.LIBRERUN_STUB_LLM`` — and ``LIBRERUN_STUB_LLM`` moved
        to the gateway's environment in this very batch, so in the
        standard deployment the backend's copy is its default ``False``
        no matter what the operator set. An agent using this method as
        the statement of fact it is documented to be would have labelled
        fixture-backed output as provider-backed (Codex P2). The compose
        comment next to that variable says "keyless mode is the
        gateway's to decide"; this now asks the process that decides
        instead of contradicting it.

        Asynchronous for that reason, and answered once per façade: a
        pipeline that mentions it in every step should not pay an HTTP
        round trip per step.
        """
        if self._stub_mode is None:
            from app.services import gateway_client

            health = await gateway_client.health()
            # Unreachable: say nothing rather than claim fixtures. The
            # call that follows will fail on its own and say why.
            self._stub_mode = bool((health or {}).get("stub", False))
        return self._stub_mode

    async def complete(self, step: str, messages: list[dict], **kwargs) -> dict:
        """One model call for ``step``, as an OpenAI chat completion.

        The step must be declared in the agent's manifest
        (``llm.steps[]``); the gateway refuses an id that is not, because
        an invented one would slip past the provider, model, token and
        timeout choices the admin made for the declared ones. Everything
        else about the call — which model, at what temperature, within
        what budget — is the tenant's configuration, resolved at request
        time, which is why changing a model in the admin UI needs no
        redeploy.
        """
        from app.services import gateway_client

        return await gateway_client.chat(
            run_token=self._run_token,
            step=step,
            messages=messages,
            seconds_left=self._budget(),
            **kwargs,
        )


class KbCapability:
    """Tenant-scoped internal knowledge-base search. Chassis-owned since
    B13 — agents call it, in-process via this object or over MCP as the
    ``kb_search`` tool.

    The query embedding is an agent-triggered model call, so from S4a it
    goes through the gateway like every other one (step ``kb_embed``,
    routed by the platform's own ``kb.embed_model`` setting): outbound
    redaction, tenant and cost attribution, and the stub, which returns
    deterministic vectors so keyless search gives stable results. No
    provider URL is left in this module.
    """

    def __init__(self, tenant_id: UUID, run_token: str | None = None, seconds_left=None):
        self._tenant_id = tenant_id
        self._run_token = run_token
        self._seconds_left = seconds_left

    def _budget(self) -> float | None:
        return self._seconds_left() if self._seconds_left else None

    # The vector store's key is the platform's secret setting (K8a, D36):
    # a platform admin sets it in Admin -> Settings, and the environment's
    # PINECONE_API_KEY serves while it is not set.
    PINECONE_KEY_SETTING = "kb.pinecone_api_key"

    def available(self) -> bool:
        from app.config import settings
        from app.services import app_settings_service

        # The embedding side is the gateway's now, and the gateway is a
        # default service — so what this asks about is the vector store
        # alone. A deployment with no Pinecone has no knowledge base
        # whatever its model configuration says. Synchronous, so it reads
        # the key as last resolved in this process: the runner resolves it
        # before a phase granted ``kb``, and ``search`` afresh.
        return bool(
            app_settings_service.last_secret_setting(self.PINECONE_KEY_SETTING)
            and settings.PINECONE_ENVIRONMENT
        )

    async def _refresh_key(self) -> str:
        """The vector store's key as it stands now: the store's row, else the
        environment's (``get_secret_setting``), which ``available`` then
        reads. Private: the key is the platform's, and the façade an agent
        holds hands it to the vector store alone."""
        from app.database import async_session
        from app.services import app_settings_service

        async with async_session() as db:
            return await app_settings_service.get_secret_setting(
                db, self.PINECONE_KEY_SETTING
            )

    # OpenInference's retriever convention; more per span is noise.
    MAX_STAMPED_DOCUMENTS = 20

    def stamp(self, results: list) -> int:
        """Stamp ``retrieval.documents.N.document.{id,content,score}`` on
        the CURRENT span for up to ``MAX_STAMPED_DOCUMENTS`` results
        (blueprint S2). Call it from inside the step that ran the search
        so the documents land on that step's span; the orchestrator no
        longer guesses at result shapes. Each result is a dict (or has
        ``model_dump``): ``url`` or ``id`` → id, ``snippet``, ``content``
        or ``text`` → content (1 KB cap), ``relevance_score`` or
        ``score`` → score. Works for this capability's own results and
        for an agent's own retrieval alike. Returns how many were
        stamped; a no-op with no recording span."""
        from opentelemetry import trace

        span = trace.get_current_span()
        if not span.is_recording():
            return 0
        stamped = 0
        for doc in results:
            if stamped >= self.MAX_STAMPED_DOCUMENTS:
                break
            if hasattr(doc, "model_dump"):
                doc = doc.model_dump()
            if not isinstance(doc, dict):
                continue
            base = f"retrieval.documents.{stamped}.document"
            ident = doc.get("url") or doc.get("id")
            if ident:
                span.set_attribute(f"{base}.id", str(ident))
            content = doc.get("snippet") or doc.get("content") or doc.get("text")
            if content:
                span.set_attribute(f"{base}.content", str(content)[:1000])
            score = doc.get("relevance_score", doc.get("score"))
            if score is not None:
                try:
                    span.set_attribute(f"{base}.score", float(score))
                except (TypeError, ValueError):
                    pass
            stamped += 1
        return stamped

    async def search(self, queries: list[str], top_k: int = 10) -> list[dict]:
        """Plain-dict results: {title, url, snippet, relevance_score}.
        Failures degrade to [] (logged) — search is an enrichment, never
        the run's fate. Stamp them with ``stamp`` from the calling step."""
        from app.config import settings
        from app.logging_pii import user_content
        from app.services import gateway_client

        api_key = await self._refresh_key()
        if not (api_key and settings.PINECONE_ENVIRONMENT):
            return []
        try:
            from pinecone import Pinecone  # type: ignore

            pc = Pinecone(api_key=api_key)
            index = pc.Index(settings.PINECONE_INDEX_NAME)
            namespace = f"tenant_{self._tenant_id}"
            out: list[dict] = []
            # One gateway call for the whole batch: the platform's
            # embedding step is bounded to what this search already
            # allows (queries and their length), and the gateway
            # enforces exactly those bounds.
            vectors = await gateway_client.embed(
                run_token=self._run_token,
                inputs=list(queries),
                seconds_left=self._budget(),
            )
            for q, vec in zip(queries, vectors):
                if not vec:
                    continue
                res = index.query(
                    vector=vec,
                    top_k=top_k,
                    namespace=namespace,
                    include_metadata=True,
                )
                for m in res.get("matches", []):
                    md = m.get("metadata") or {}
                    out.append(
                        {
                            "title": md.get("title", "KB entry"),
                            "url": md.get("url", ""),
                            "snippet": (md.get("text") or "")[:500],
                            "relevance_score": float(m.get("score", 0.5)),
                        }
                    )
            return out
        except Exception as e:  # noqa: BLE001
            # Names the index: a query against one that does not exist (a
            # deployment upgraded past the S2 default without pinning its
            # own — docs/platform/Install.md §10) must be diagnosable from this line.
            logger.warning(
                "kb_search_failed",
                index=settings.PINECONE_INDEX_NAME,
                error=user_content(str(e)),
            )
            return []



def _redis_client():
    """A per-call Redis client (async context manager). Capabilities are
    invoked from many event loops (request handlers, background runners,
    the MCP endpoint) — the module-global pooled client's loop affinity
    would poison cross-loop use, and capability call rates don't justify
    a pool. Connection kwargs are the chassis's own, so these clients
    inherit the deliberate MAINT_NOTIFICATIONS suppression rather than
    polluting wire-level traces with failed probes."""
    import redis.asyncio as aioredis

    from app.config import settings
    from app.redis import _client_kwargs

    return aioredis.from_url(settings.REDIS_URL.get_secret_value(), **_client_kwargs())

class RunStoreCapability:
    """Run-scoped KV (Redis hash ``run:{id}:kv``). Values are JSON."""

    def __init__(self, run_id: UUID):
        from app.services import run_boundary

        self._key = run_boundary.run_kv_key(run_id)

    async def get(self, key: str) -> Any:
        async with _redis_client() as redis:
            raw = await redis.hget(self._key, key)
        return None if raw is None else json.loads(raw)

    async def set(self, key: str, value: Any) -> None:
        from app.services import run_boundary

        # The key becomes the hash's field name — an identifier, refused
        # when flagged; the value is walked (blueprint S4, ``pii_in_store``).
        key = run_boundary.check_name(
            key, argument="key", reason=run_boundary.REASON_STORE, path="run_store.key"
        )
        value = run_boundary.walk_value(
            value, argument="value", reason=run_boundary.REASON_STORE
        )
        async with _redis_client() as redis:
            # Run scratch space is not a durable store: without an expiry
            # nothing ever reclaims these hashes, since no code path
            # deletes them when a run finishes. The write and that expiry
            # are ONE fact about one key, so they go in one round trip —
            # this used to be two awaits, and a crash between them left an
            # immortal key.
            await run_boundary.run_hash_write(
                redis, self._key, key, json.dumps(value)
            )

    async def all(self) -> dict[str, Any]:
        async with _redis_client() as redis:
            raw = await redis.hgetall(self._key)
        return {k: json.loads(v) for k, v in raw.items()}


class ProgressCapability:
    """Live step progress for the run page (Redis hash the progress
    endpoint reads), plus the orchestrator factory — the chassis's
    step-timing/span machinery that in-process agents drive."""

    def __init__(self, run_id: UUID):
        self._run_id = run_id

    async def update(
        self, step_id: str, status: str, detail: str | None = None
    ) -> None:
        from app.services import run_boundary

        # One write path for the progress hash (blueprint S4): the step id
        # is its field name and is refused when flagged
        # (``pii_in_progress``); the detail is redacted.
        async with _redis_client() as redis:
            await run_boundary.progress_write(
                redis, self._run_id, step_id, status, detail
            )

    def orchestrator(self, *args, **kwargs):
        """A ``PipelineOrchestrator`` for this run — imported here so
        agents never import ``app.services`` themselves."""
        from app.services.orchestrator import PipelineOrchestrator

        return PipelineOrchestrator(*args, **kwargs)

    def pipeline(self, llm, step_kinds: dict | None = None):
        """The in-process pipeline context (blueprint B13): an async
        context manager yielding ``(run, orchestrator)`` for this run.
        The capability owns the DB session, the Redis handle, and the
        commit on clean exit — the agent drives steps and returns pure
        results, importing no chassis infrastructure. ``run`` is None
        when the run's row has vanished (the agent should error out)."""
        from contextlib import asynccontextmanager

        run_id = self._run_id

        @asynccontextmanager
        async def _ctx():
            from app.database import async_session
            from app.models import Run
            from app.redis import get_redis
            from app.services.orchestrator import PipelineOrchestrator

            redis = await get_redis()
            async with async_session() as db:
                run = await db.get(Run, run_id)
                orch = PipelineOrchestrator(
                    db, redis, llm, step_kinds=step_kinds or {}
                )
                yield run, orch
                await db.commit()

        return _ctx()


class _WalkedReport:
    """A drift report after the walk: the dict the audit service stores."""

    def __init__(self, data: dict):
        self._data = data

    def to_dict(self) -> dict:
        return self._data


class AuditCapability:
    """Tenant-scoped audit writes. Owns its DB session — agents never
    touch ``app.database``.

    Attribution is not an argument (blueprint S4): a row an agent emits
    carries the run owner's ``user_id`` and email, stamped here from the
    run — the id the runner (or the run-token record) handed over, the
    email resolved from the user row at write time — so no column an
    agent can fill escapes the walk. Every argument IS walked
    (``pii_in_audit``): the action type as an identifier, the detail as
    a JSON value.
    """

    def __init__(self, run_id: UUID, tenant_id: UUID, user_id: UUID | None = None):
        self._run_id = run_id
        self._tenant_id = tenant_id
        self._user_id = user_id

    async def _attribution(self, db) -> tuple[UUID | None, str | None]:
        from app.models import Run, User

        user_id = self._user_id
        if user_id is None:
            run = await db.get(Run, self._run_id)
            user_id = getattr(run, "user_id", None)
        email = None
        if user_id is not None:
            user = await db.get(User, user_id)
            email = getattr(user, "email", None)
        return user_id, email

    async def log_schema_drift(self, reports: list) -> None:
        from app.database import async_session
        from app.services import run_boundary
        from app.services.audit_service import log_schema_drift

        if not reports:
            return
        walked = [
            _WalkedReport(
                run_boundary.walk_value(
                    r.to_dict(), argument="reports", reason=run_boundary.REASON_AUDIT
                )
            )
            for r in reports
        ]
        try:
            async with async_session() as db:
                async with db.begin():
                    await log_schema_drift(
                        db, self._tenant_id, self._run_id, walked
                    )
        except Exception as e:  # noqa: BLE001
            logger.warning("audit_capability_write_failed", error=str(e))

    async def log(self, action_type: str, detail: dict) -> bool:
        """A generic tenant-scoped audit row (e.g. the demo agent's
        ``blocked_request``), attributed to the run's owner by the
        chassis. Owns its session; write failures are logged and
        reported via the return value, never raised — audit must not
        decide a run's fate, but a caller that reports success to an
        agent needs to know whether the row landed. A walk refusal IS
        raised (``PiiRefused``, ``pii_in_audit``): the row was never
        going to be written."""
        from app.database import async_session
        from app.models import ActivityAuditLog
        from app.services import run_boundary

        action_type = run_boundary.check_name(
            action_type,
            argument="action_type",
            reason=run_boundary.REASON_AUDIT,
            path="audit_log.action_type",
        )
        detail = run_boundary.walk_value(
            detail, argument="detail", reason=run_boundary.REASON_AUDIT
        )
        try:
            async with async_session() as db:
                async with db.begin():
                    user_id, user_email = await self._attribution(db)
                    db.add(
                        ActivityAuditLog(
                            tenant_id=self._tenant_id,
                            user_id=user_id,
                            user_email=user_email,
                            action_type=action_type,
                            detail=detail,
                        )
                    )
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("audit_capability_write_failed", error=str(e))
            return False


class PiiCapability:
    """The intake PII pipeline on demand (blueprint S4, gap J3): what an
    agent fetches at runtime can be stripped the way intake strips what
    a user typed — the same recognizers, the same placeholders. Served to
    containers as the MCP ``redact`` tool under the same ``pii`` grant."""

    async def redact(self, text: str) -> str:
        from app.services import pii_service

        return pii_service.redact(str(text))[0]


class ConfigCapability:
    """This run's configuration, read at call time (K5a, L32).

    ``steps()`` is the agent's declared ``llm.steps[]`` with this tenant's
    admin overrides applied; ``settings()`` is its declared ``settings[]``
    with this tenant's values, ``{key: value}``. Both are read when called,
    never at construction: an admin's edit must reach the next read, which
    is the point of configuration being data (L25, L32).

    Not a capability. It reads nothing but the run's own agent's
    configuration in the run's own tenant, so it sits outside the grant
    list and outside the members ``__getattr__`` guards — the same answer
    the MCP ``config_get`` tool gives a container, which needs no grant
    either.
    """

    def __init__(self, tenant_id: UUID, agent_id: str):
        self._tenant_id = tenant_id
        self._agent_id = agent_id

    async def steps(self) -> list[dict]:
        from app.agents.registry import get_manifest
        from app.database import async_session
        from app.services import agent_step_config_service as step_configs

        async with async_session() as db:
            overrides = await step_configs.overrides_for(
                db, self._tenant_id, self._agent_id
            )
        return step_configs.effective_steps(get_manifest(self._agent_id), overrides)

    async def settings(self) -> dict[str, Any]:
        from app.agents.registry import get_manifest
        from app.database import async_session
        from app.services import agent_settings_service as agent_settings

        async with async_session() as db:
            values = await agent_settings.values_for(
                db, self._tenant_id, self._agent_id
            )
        return agent_settings.effective_values(get_manifest(self._agent_id), values)


class SecretsCapability:
    """This run's tool secrets, by name (K8a, D20).

    ``get(name)`` answers a name the manifest declares in ``secrets[]``
    with this tenant's value: the ``tenant`` row, else every tenant's
    default (the ``agent`` row), else — for an in-process agent alone —
    the upper-cased name in the backend's environment. It raises
    ``SecretNotDeclared`` for any other name and ``SecretNotSet`` when
    none of those has a value, and logs neither the name nor the value.

    A value it returns is the one exception L31 makes: delivered to the
    declaring run in its own tenant, and to nothing else. The runner
    scrubs it from everything the run persists — the value joins the
    run's scrub set as it is handed over — and the row's ``last_used_at``
    is stamped at most hourly.

    Not a capability, like ``config``: the agent's own data, in no grant
    list and needing none.
    """

    def __init__(self, tenant_id: UUID, agent_id: str, *, env_fallback: bool):
        self._tenant_id = tenant_id
        self._agent_id = agent_id
        self._env_fallback = env_fallback

    async def get(self, name: str) -> str:
        from app.agents.registry import get_manifest
        from app.services import run_boundary
        from app.services import tool_secrets_service as tool_secrets

        manifest = get_manifest(self._agent_id)
        if manifest is None or name not in manifest.secrets:
            raise SecretNotDeclared(name)
        found = await tool_secrets.resolve(
            self._tenant_id, self._agent_id, name, env_fallback=self._env_fallback
        )
        if found is None:
            raise SecretNotSet(name)
        if found.row is not None:
            await tool_secrets.stamp(found.row, name)
        run_boundary.add_scrub(found.value)
        return found.value


class Capabilities:
    """The per-run façade. Attribute access enforces the manifest grant —
    on the six members; ``config`` and ``secrets`` are the run's own and
    need none."""

    def __init__(
        self,
        *,
        run_id: UUID,
        tenant_id: UUID,
        agent_id: str,
        grants: list[str],
        user_id: UUID | None = None,
        run_token: str | None = None,
        deadline_seconds: int | None = None,
        secrets_env_fallback: bool = False,
    ):
        self.run_id = run_id
        self.tenant_id = tenant_id
        self.agent_id = agent_id
        # A plain attribute, so ``__getattr__`` never sees it (K5a).
        self.config = ConfigCapability(tenant_id, agent_id)
        # The same for the run's tool secrets (K8a): no grant.
        self.secrets = SecretsCapability(
            tenant_id, agent_id, env_fallback=secrets_env_fallback
        )
        self._grants = frozenset(grants)
        # The invocation's wall-clock budget, the same number
        # ``agent_runner`` wraps the phase in. Every call the façade
        # makes to the gateway takes its transport ceiling from what is
        # left of it, so the ceiling can never fire before the step
        # timeout the admin configured (Codex P1, blueprint S4a).
        self._deadline_seconds = deadline_seconds
        self._started_at = _time.monotonic()
        # The invocation's run token (blueprint S4a). Every model call
        # the façade makes presents it, so the gateway can attribute the
        # spend to this run and this tenant — which an agent key alone
        # could never say, and which this process holds no other way.
        self._run_token = run_token
        self._members: dict[str, Any] = {
            "llm": LlmCapability(run_token, self.seconds_left),
            "kb": KbCapability(tenant_id, run_token, self.seconds_left),
            "run_store": RunStoreCapability(run_id),
            "progress": ProgressCapability(run_id),
            "audit": AuditCapability(run_id, tenant_id, user_id),
            "pii": PiiCapability(),
        }

    def seconds_left(self) -> float | None:
        """What is left of the invocation's budget, or None when it has
        none (a façade built outside a phase). Never negative: at zero
        the phase deadline is cancelling this task anyway."""
        if self._deadline_seconds is None:
            return None
        return max(0.0, self._started_at + self._deadline_seconds - _time.monotonic())

    def granted(self, name: str) -> bool:
        return name in self._grants

    def __getattr__(self, name: str) -> Any:
        # Only called for names not found normally — i.e. the members.
        # ``caps.case_store`` keeps working for one release (S1 alias).
        name = CAPABILITY_ALIASES.get(name, name)
        members = self.__dict__.get("_members", {})
        if name in members:
            if name not in self.__dict__["_grants"]:
                raise CapabilityNotGranted(name, self.__dict__["_grants"])
            return members[name]
        raise AttributeError(name)


def for_run(
    *,
    run_id: UUID,
    tenant_id: UUID,
    agent_id: str,
    grants: list[str],
    user_id: UUID | None = None,
    run_token: str | None = None,
    deadline_seconds: int | None = None,
    secrets_env_fallback: bool = False,
) -> Capabilities:
    """The runner's constructor: one façade per run, granted per the
    agent's manifest ``capabilities:`` list. ``agent_id`` is the run's
    agent, whose configuration ``config`` reads (K5a) — required, because
    a façade that knew no agent could only answer with some other one's.
    ``user_id`` is the run's owner, the attribution the audit capability
    stamps. ``deadline_seconds`` is the invocation's budget — what bounds
    every model call this façade makes. ``secrets_env_fallback`` lets
    ``secrets.get`` fall back to the backend's environment (K8a): the
    runner sets it for an in-process agent, and nothing else does — the
    MCP tool reads rows alone (K8-12)."""
    grants = normalize_grants(grants)
    unknown = [g for g in grants if g not in KNOWN_CAPABILITIES]
    if unknown:
        # Descriptive/declarative slugs (e.g. the demo agent's ``web-search``) are
        # still legal and grant nothing, so this can't be an error — but
        # a TYPO'd real capability looks exactly the same and would only
        # surface as a puzzling CapabilityNotGranted mid-run, so say so
        # loudly with the valid names alongside.
        logger.warning(
            "capability_grants_unknown",
            unknown=unknown,
            known=list(KNOWN_CAPABILITIES),
            hint="these grant nothing; if one is a typo of a real "
            "capability the agent will fail when it reaches for it",
        )
    return Capabilities(
        run_id=run_id,
        tenant_id=tenant_id,
        agent_id=agent_id,
        grants=grants,
        user_id=user_id,
        run_token=run_token,
        deadline_seconds=deadline_seconds,
        secrets_env_fallback=secrets_env_fallback,
    )
