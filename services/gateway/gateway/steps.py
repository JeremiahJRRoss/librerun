"""Which model this call actually uses (L25, D13).

The agent names a **step**, never a model: ``X-LibreRun-Step: <id>``, or
a model of the form ``librerun/<id>`` for a framework that can only set
a model string. The gateway resolves that step to a provider, a model,
a temperature, a token limit and a timeout — from the tenant's admin
configuration first, the manifest's own defaults second — at request
time. That is what makes a model change data rather than a deploy: edit
the step in the admin UI and the next call uses it, with nothing
restarted.

Two rules keep the resolution honest:

* **the step must be declared.** An id absent from the agent's *current*
  manifest snapshot is refused. A tenant override never declares a step,
  it only replaces values for one the manifest still declares — so an
  override row left behind by a step that was removed or renamed is
  inert, and the retired step stays uncallable. An invented id would
  otherwise slip past every provider, model, token and timeout choice
  the admin made for the declared ones.
* **there is no platform-wide fallback model.** A call naming no step is
  attributed to the agent's declared ``default`` step when it declares
  one, and refused otherwise. The `model` field of the request is a step
  selector and nothing more: whatever a framework puts there, the
  admin's choice for the step is what gets called.

``kb_embed`` is the one exception, and it belongs to the platform rather
than to any agent: knowledge search's query embedding. It is routed by
the platform's own ``kb.embed_model`` setting, authorized by the ``kb``
grant, and bounded to exactly what ``kb_search`` already lets that grant
cause — so an agent that only searches can cause no embedding spend it
could not already cause by searching.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from gateway import errors
from gateway.config import settings

# The step the platform owns. An agent manifest may not declare it (the
# chassis validator refuses), so this name always means the platform's.
KB_EMBED_STEP = "kb_embed"
PLATFORM_STEPS = (KB_EMBED_STEP,)

# The step id an agent may declare to catch calls that name none.
DEFAULT_STEP_ID = "default"

# The platform setting that routes kb_embed. Written by the settings page
# through app_settings_service, read here at request time, so an edit
# takes effect on the next call with no restart.
KB_EMBED_SETTING = "kb.embed_model"

# The provider name that means "no provider account needed" (D4).
STUB_PROVIDER = "stub"

# Public provider name -> the prefix LiteLLM routes by.
#
# ``google`` is the one that matters and it was silently broken:
# ``AgentConfigMeta`` advertises ``["openai", "anthropic", "google"]``
# (the chassis default and the bundled agent's alike), but LiteLLM
# reaches Google AI Studio as ``gemini/<model>`` and rejects
# ``google/<model>`` outright with "LLM Provider NOT provided". So every
# step an admin configured with the ADVERTISED Google option failed at
# provider resolution, never reaching a model at all.
#
# What hid it is that the credential side was already right:
# ``egress._PROVIDER_KEYS`` maps BOTH ``gemini`` and ``google`` to
# ``GOOGLE_AI_API_KEY``. The duality had been thought about once, on the
# half that does not route, which reads exactly like the whole problem
# having been handled (Codex P1).
_LITELLM_PROVIDER = {"google": "gemini"}

_EDITABLE = ("provider", "model", "temperature", "max_tokens", "timeout_seconds")


@dataclass
class ResolvedStep:
    step_id: str
    provider: str | None = None
    model: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    timeout_seconds: int | None = None
    platform: bool = False

    @property
    def target(self) -> str:
        """What the egress library is asked for: ``provider/model``.

        The name the platform ADVERTISES is not always the name LiteLLM
        routes by, and the two are different vocabularies that happen to
        overlap. ``_LITELLM_PROVIDER`` translates between them here, at
        the one point a provider becomes a route — ``self.provider``
        keeps the admin's own word, so the span, the error messages and
        the configuration page all still say what was chosen.
        """
        if self.provider and self.model:
            provider = _LITELLM_PROVIDER.get(self.provider.lower(), self.provider)
            return f"{provider}/{self.model}"
        return self.model or self.provider or ""


def requested_step_id(step_header: str | None, model: object) -> str | None:
    """The step this call names, or None.

    The header wins over the model string: a framework that can set both
    means the header, and a model of ``librerun/<id>`` is the fallback
    for one that can only set a model.
    """
    header = (step_header or "").strip()
    if header:
        return header
    if isinstance(model, str) and model.startswith("librerun/"):
        return model[len("librerun/") :].strip() or None
    return None


async def _override(
    db: AsyncSession, tenant_id: str, agent_id: str, step_id: str
) -> dict:
    row = (
        await db.execute(
            text(
                "SELECT provider, model, temperature, max_tokens, timeout_seconds "
                "FROM agent_step_configs "
                "WHERE tenant_id = CAST(:t AS UUID) AND agent_id = :a "
                "AND step_id = :s"
            ),
            {"t": tenant_id, "a": agent_id, "s": step_id},
        )
    ).first()
    return dict(row._mapping) if row is not None else {}


async def kb_embed_model(db: AsyncSession) -> str:
    """``provider/model`` for the platform's embedding step.

    The ``kb.embed_model`` setting row when one exists, else
    ``LIBRERUN_KB_EMBED_MODEL``. Never taken from any agent's
    ``llm.steps``: knowledge search is the platform's feature, not the
    agent's, and an agent that could route it could spend the platform's
    budget on a model of its choosing.
    """
    row = (
        await db.execute(
            # ``#>> '{}'`` reads the JSONB scalar AS TEXT, unquoted. A
            # plain ``SELECT value`` over a raw statement returns the JSON
            # source, so the model would arrive spelled '"openai/x"'.
            text("SELECT value #>> '{}' FROM app_settings WHERE key = :k"),
            {"k": KB_EMBED_SETTING},
        )
    ).scalar_one_or_none()
    if isinstance(row, str) and row.strip():
        return row.strip()
    return settings.LIBRERUN_KB_EMBED_MODEL


async def resolve(
    db: AsyncSession,
    principal,
    *,
    step_header: str | None,
    model: object,
    embedding: bool = False,
) -> ResolvedStep:
    step_id = requested_step_id(step_header, model)

    if step_id in PLATFORM_STEPS:
        return await _resolve_platform_step(db, principal, step_id, embedding=embedding)

    principal.require("llm")

    if step_id is None:
        if principal.snapshot.step(DEFAULT_STEP_ID) is None:
            raise errors.bad_request(
                "step_required",
                f"name the step this call belongs to — the header "
                f"'X-LibreRun-Step: <id>' or a model of the form "
                f"'librerun/<id>'. Agent {principal.agent_id!r} declares no "
                f"'{DEFAULT_STEP_ID}' step, and the platform has no fallback "
                f"model of its own to spend on your behalf.",
                param="model",
            )
        step_id = DEFAULT_STEP_ID

    declared = principal.snapshot.step(step_id)
    if declared is None:
        raise errors.bad_request(
            "unknown_step",
            f"agent {principal.agent_id!r} does not declare a step "
            f"{step_id!r}. Declared steps: "
            f"{sorted(s.get('id') for s in principal.snapshot.steps) or 'none'}. "
            f"An id the manifest does not declare has no provider, model or "
            f"limits an admin ever chose.",
            param="model",
        )

    resolved = ResolvedStep(
        step_id=step_id,
        **{field: declared.get(field) for field in _EDITABLE},
    )
    if principal.tenant_id:
        for field, value in (
            await _override(db, principal.tenant_id, principal.agent_id, step_id)
        ).items():
            if value is not None:
                setattr(resolved, field, value)

    if settings.LIBRERUN_STUB_LLM:
        resolved.provider = STUB_PROVIDER
    elif not resolved.provider or not resolved.model:
        raise errors.bad_request(
            "step_not_configured",
            f"step {step_id!r} has no provider and model to call — the "
            f"manifest declares none and this tenant has set none. Choose "
            f"them on the agent's configuration page.",
            param="model",
        )
    return resolved


async def _resolve_platform_step(
    db: AsyncSession, principal, step_id: str, *, embedding: bool
) -> ResolvedStep:
    """``kb_embed``: the platform's own step, bounded to what the ``kb``
    grant already lets an agent cause."""
    principal.require("kb")
    if not embedding:
        raise errors.bad_request(
            "unknown_step",
            f"{step_id!r} is the platform's embedding step for knowledge "
            f"search; it is not a chat step and answers /v1/embeddings only",
            param="model",
        )
    provider, _, name = (await kb_embed_model(db)).partition("/")
    return ResolvedStep(
        step_id=step_id,
        # Keyless mode replaces the PROVIDER, never the model: the span
        # still says which model this call stood in for, and the cost
        # table still prices it, so a keyless run exercises the same
        # telemetry a credentialled one produces.
        provider=STUB_PROVIDER if settings.LIBRERUN_STUB_LLM else (provider or None),
        model=name or None,
        platform=True,
    )


def check_kb_embed_bounds(inputs: list) -> None:
    """The bounds ``kb_search`` imposes, enforced on the embedding call.

    ``kb_search`` truncates an over-long query; the gateway refuses it,
    because a truncating gateway would embed something other than what
    was asked for and charge the run for it.

    These are a SPEND boundary, not a privacy one, so they hold whatever
    ``llm.redact_outbound`` says. Non-string input — the token-id form
    the embeddings API also accepts — is refused here rather than
    measured: it is text encoded past both the redactor and the
    character bound, the platform's own caller never sends it, and
    filtering it out (which this did) counted it as nothing at all, so
    an agent with redaction off could spend without limit through the
    platform's step (Codex P2).
    """
    for index, value in enumerate(inputs):
        if not isinstance(value, str):
            raise errors.bad_request(
                "kb_embed_bounds",
                f"input[{index}] is not text — the platform's embedding step "
                f"takes the query as a string, since a token-id input can be "
                f"neither read nor measured against the bound knowledge "
                f"search applies",
                param="input",
            )
    if len(inputs) > settings.KB_EMBED_MAX_QUERIES:
        raise errors.bad_request(
            "kb_embed_bounds",
            f"the platform's embedding step takes at most "
            f"{settings.KB_EMBED_MAX_QUERIES} inputs (got {len(inputs)}) — the "
            f"same bound knowledge search applies",
            param="input",
        )
    for index, value in enumerate(inputs):
        if len(value) > settings.KB_EMBED_MAX_QUERY_CHARS:
            raise errors.bad_request(
                "kb_embed_bounds",
                f"input[{index}] is longer than the "
                f"{settings.KB_EMBED_MAX_QUERY_CHARS} characters knowledge "
                f"search allows a query",
                param="input",
            )
