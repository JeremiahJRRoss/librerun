"""The demo agent's model calls: one per step, through the platform's gateway.

Agent-local: the prompts, the schemas and the parsing of what comes back
are this agent's. Which model answers a step is not — the manifest's
``llm.steps[]`` with this tenant's overrides, resolved by the gateway at
request time (L25) — and how often a step retries is this tenant's
``max_retries_per_step`` setting, read when the phase starts (K5b).
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

import structlog

from app.capabilities import LlmCapability, LlmError
from app.logging_pii import user_content

# This agent's own manifest — where its LLM steps and their defaults live
# from S4a on. Read here rather than through the chassis registry so the
# service still works when constructed directly (unit tests, tooling).
_MANIFEST_PATH = Path(__file__).resolve().parent / "agent.yaml"

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Step schemas — provider-native structured output for the highest-stakes
# pipeline steps. Keyed by step_id (not provider); each adapter translates
# this into its native format (OpenAI response_format json_schema, Anthropic
# tool-use). Normalizers still run afterwards as a safety net.
# ---------------------------------------------------------------------------

_STRING_ARRAY = {"type": "array", "items": {"type": "string"}}

_RESOLUTION_SECTION = {
    "type": "object",
    "properties": {
        "text": {"type": "string"},
        "citations": {"type": "array", "items": {"type": "integer"}},
    },
    "required": ["text", "citations"],
    "additionalProperties": False,
}

_WORKS_CITED_ITEM = {
    "type": "object",
    "properties": {
        "id": {"type": "integer"},
        "title": {"type": "string"},
        "url": {"type": "string"},
        "doc_type": {"type": "string"},
        "relevance_score": {"type": "number"},
        "summary": {"type": "string"},
    },
    "required": ["id", "title", "url", "doc_type", "relevance_score", "summary"],
    "additionalProperties": False,
}

STEP_SCHEMAS: dict[str, dict] = {
    "refine_problem_statement": {
        "type": "object",
        "properties": {
            "refined_problem_statement": {"type": "string"},
            "key_signals": _STRING_ARRAY,
            "suspected_root_causes": _STRING_ARRAY,
            "research_focus_areas": _STRING_ARRAY,
        },
        "required": [
            "refined_problem_statement",
            "key_signals",
            "suspected_root_causes",
            "research_focus_areas",
        ],
        "additionalProperties": False,
    },
    "generate_resolution_plan": {
        "type": "object",
        "properties": {
            "mitigation": _RESOLUTION_SECTION,
            "resolution": _RESOLUTION_SECTION,
            "avoidance": _RESOLUTION_SECTION,
            "works_cited": {
                "type": "object",
                "properties": {
                    "vendor_a": {"type": "array", "items": _WORKS_CITED_ITEM},
                    "vendor_b": {"type": "array", "items": _WORKS_CITED_ITEM},
                },
                "required": ["vendor_a", "vendor_b"],
                "additionalProperties": False,
            },
        },
        "required": ["mitigation", "resolution", "avoidance", "works_cited"],
        "additionalProperties": False,
    },
    "assess_skills": {
        "type": "object",
        "properties": {
            "skills": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "description": {"type": "string"},
                        "relevance_weight": {"type": "number"},
                        "source": {
                            "type": "string",
                            "enum": [
                                "vendor_docs",
                                "internal_kb",
                                "public_web",
                                "llm_knowledge",
                            ],
                        },
                    },
                    "required": ["name", "description", "relevance_weight", "source"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["skills"],
        "additionalProperties": False,
    },
    "generate_followup_questions": {
        "type": "object",
        "properties": {
            "questions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "question": {"type": "string"},
                        "rationale": {"type": "string"},
                        "expected_impact": {"type": "string"},
                    },
                    "required": ["question", "rationale", "expected_impact"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["questions"],
        "additionalProperties": False,
    },
}


# Words a provider uses when it is the STRUCTURED-OUTPUT feature it will
# not accept, rather than anything else about the request. Matching on a
# provider's prose is inexact, and the alternative is worse: retrying
# every 400 without the schema would mask real errors and pay twice for
# them. So the test is narrow — a 400 the gateway classed as the
# provider's own refusal, for a call that actually sent the parameter,
# naming the parameter.
_UNSUPPORTED_SCHEMA_MARKERS = (
    "response_format",
    "json_schema",
    "structured output",
    "structured_output",
)


def _rejected_response_format(exc: LlmError, kwargs: dict) -> bool:
    if "response_format" not in kwargs:
        return False
    if getattr(exc, "status", None) != 400:
        return False
    if getattr(exc, "code", "") != "provider_refused":
        return False
    message = str(getattr(exc, "message", "") or exc).lower()
    return any(marker in message for marker in _UNSUPPORTED_SCHEMA_MARKERS)


def _resolved_field(response: dict, field: str) -> str:
    """One field of the gateway's ``librerun`` envelope, or ""."""
    resolved = (response or {}).get("librerun")
    if isinstance(resolved, dict) and resolved.get(field):
        return str(resolved[field])
    return ""


def _provider_of(response: dict, declared: dict | None) -> str:
    """Which provider answered.

    The gateway says so, in the ``librerun`` envelope it puts on the
    reply: it is the process that RESOLVED the step, so it is the only
    one that knows whether this tenant overrode the packaged provider.

    This used to return the manifest's declared value unconditionally,
    on the reasoning that an OpenAI-shaped reply names no provider. True,
    and it made every drift row for an overridden step name the wrong
    provider — in the batch whose whole point is that a tenant may
    override it (Codex P2). The declaration is now only the fallback, for
    a reply from something older than this envelope.
    """
    resolved = (response or {}).get("librerun")
    if isinstance(resolved, dict) and resolved.get("provider"):
        return str(resolved["provider"])
    return (declared or {}).get("provider", "") or ""


class LLMService:
    """One model call per step, through the platform's gateway.

    Until S4a this class held three provider SDK clients, read API keys
    out of the ``llm`` capability and branched on the step's configured
    provider. None of that is here any more, and none of it should be:
    a provider credential in this process is a credential every
    in-process agent can read, and choosing the model is the admin's job,
    not the agent's (L25).

    What is left is what an agent actually owns — its prompts, its
    schemas, and the parsing of what comes back.
    """

    step_schemas: dict[str, dict] = STEP_SCHEMAS

    # Class-level defaults so the attributes always resolve, including for
    # instances built without ``__init__`` (tests that build one bare).
    llm_capability = LlmCapability()
    max_retries = 2

    def __init__(self, llm_capability=None, max_retries: int = 2):
        # Blueprint B13/S4a: model calls go through the run's GRANTED
        # ``llm`` capability, whose mere attribute access enforces the
        # manifest grant. The default keeps direct construction working
        # for unit tests.
        self.llm_capability = llm_capability or LlmCapability()
        # How many times ``call_with_retry`` tries a step again: this
        # tenant's ``max_retries_per_step``, clamped by the agent when the
        # phase starts (K5b). The default is the value it had before.
        self.max_retries = max_retries
        # What the gateway actually used, per step, on the most recent
        # call. The steps and the orchestrator's span attributes ask for
        # it, and "what answered" is a better answer than "what we asked
        # for" — with an admin editing models at request time they can
        # differ.
        self._used: dict[str, dict] = {}

    @classmethod
    def declared_steps(cls) -> dict[str, dict]:
        """The manifest's ``llm.steps``, keyed by id — this agent's own
        declaration of what it calls and the defaults it ships."""
        if cls._declared is None:
            import yaml

            with open(_MANIFEST_PATH, encoding="utf-8") as f:
                manifest = yaml.safe_load(f) or {}
            steps = ((manifest.get("llm") or {}).get("steps")) or []
            cls._declared = {
                str(step.get("id")): dict(step)
                for step in steps
                if isinstance(step, dict) and step.get("id")
            }
        return cls._declared

    _declared: dict[str, dict] | None = None

    def get_step_config(self, step_id: str) -> dict:
        """What answered this step last, else what the manifest declares.

        Raises ``KeyError`` for a step that calls no model — the
        orchestrator uses that to tell an LLM step from a search one.
        """
        used = self._used.get(step_id)
        if used:
            return used
        declared = self.declared_steps().get(step_id)
        if declared is None:
            raise KeyError(step_id)
        return declared

    async def call(self, step_id: str, messages: list[dict]) -> dict:
        """One step's model call, and the JSON it returned.

        The gateway decides which model answers, redacts everything the
        model will read, pays for it and records the span. Keyless mode
        is its business too: this agent ships a fixture per step and
        hands it over as ``librerun.stub_reply``, which the gateway
        honours **only** when the resolved provider is the stub — so a
        keyless run still goes through the real redaction, the real span
        and the real cost instead of stepping around them, and the
        report it produces is the one the fixtures describe.
        """
        kwargs: dict[str, Any] = {}
        schema = self.step_schemas.get(step_id)
        if schema is not None:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": step_id,
                    "strict": True,
                    "schema": schema,
                },
            }
        from .stub_llm import fixture_for, has_fixture

        if has_fixture(step_id):
            kwargs["librerun"] = {"stub_reply": fixture_for(step_id)}

        try:
            response = await self.llm_capability.complete(step_id, messages, **kwargs)
        except LlmError as exc:
            if not _rejected_response_format(exc, kwargs):
                logger.error(
                    "llm_call_failed",
                    step_id=step_id,
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
                raise
            # The model an admin chose does not support structured
            # outputs. Before S4a each provider integration retried
            # without the constraint and let the parser and the
            # normalizers do the work; routing every call through one
            # OpenAI-shaped door dropped that, so a configuration that
            # used to DEGRADE began to fail the run outright (Codex P2).
            # The fallback is restored where it now belongs — once, only
            # for a refusal that names this feature, and said out loud so
            # an operator can see which step is running unconstrained.
            logger.warning(
                "llm_structured_output_unsupported",
                step_id=step_id,
                error=str(exc),
                fallback="retrying without response_format; the reply is "
                "parsed and normalized as unstructured JSON",
            )
            kwargs.pop("response_format", None)
            try:
                response = await self.llm_capability.complete(
                    step_id, messages, **kwargs
                )
            except Exception as retry_exc:  # noqa: BLE001
                logger.error(
                    "llm_call_failed",
                    step_id=step_id,
                    error=str(retry_exc),
                    error_type=type(retry_exc).__name__,
                )
                raise
        except Exception as exc:  # noqa: BLE001 — one log line, then re-raise
            logger.error(
                "llm_call_failed",
                step_id=step_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise

        choice = (response.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        content = message.get("content")
        used = {
            "provider": _provider_of(response, self.declared_steps().get(step_id)),
            "model": response.get("model")
            or _resolved_field(response, "model")
            or (self.declared_steps().get(step_id) or {}).get("model", ""),
        }
        self._used[step_id] = used
        logger.info(
            "llm_call_complete",
            step_id=step_id,
            model=used["model"],
            usage=(response.get("usage") or {}).get("total_tokens"),
        )
        if content is None:
            raise ValueError(
                f"step {step_id!r} got a reply with no content — a tool call "
                f"where JSON was expected"
            )
        return self._parse_json(
            content,
            step_id=step_id,
            provider=used["provider"],
            model=used["model"],
        )

    @staticmethod
    def _repair_json(text: str) -> str:
        """Attempt to fix common JSON malformations from LLM responses.

        Repairs applied (in order):
        1. Remove trailing commas before } or ]
        2. Insert missing commas between } { or ] [ or "value" "key" patterns
        3. Remove control characters that break parsing
        """
        # Remove control characters (except newline, tab) that LLMs sometimes emit
        cleaned = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', text)

        # Remove trailing commas: ,} or ,]
        cleaned = re.sub(r',\s*([}\]])', r'\1', cleaned)

        # Insert missing commas between adjacent elements:
        #   }\n  { or ]\n  [ or "value"\n  "key"
        cleaned = re.sub(r'(})\s*\n(\s*{)', r'\1,\n\2', cleaned)
        cleaned = re.sub(r'(])\s*\n(\s*\[)', r'\1,\n\2', cleaned)
        # "..." followed by newline + whitespace + "..." (missing comma between object entries)
        cleaned = re.sub(r'(")\s*\n(\s*")', r'\1,\n\2', cleaned)
        # number or true/false/null followed by newline + whitespace + "..." 
        cleaned = re.sub(r'(\d|true|false|null)\s*\n(\s*")', r'\1,\n\2', cleaned)

        return cleaned

    @staticmethod
    def _parse_json(
        text: str,
        *,
        step_id: str = "",
        provider: str = "",
        model: str = "",
    ) -> dict:
        """Extract and parse JSON from an LLM response.

        Handles the common formats we see in practice:

        1. Raw JSON: ``{"k": "v"}``
        2. ``\\u0060\\u0060\\u0060json`` fenced blocks
        3. Bare ``\\u0060\\u0060\\u0060`` fences (no language tag)
        4. Preamble text before the fence ("Here is the analysis: \\u0060\\u0060\\u0060json …")
        5. Trailing commentary after the closing brace
        6. Nested markdown inside string values (preserved; only the outer
           fence is stripped)
        7. Malformed JSON (trailing commas, missing commas) — repaired before parsing
        """
        if not isinstance(text, str):
            raise TypeError("_parse_json expects a string")

        candidate = text.strip()

        # 1. Try to pull out a fenced block anywhere in the response.
        fence_match = re.search(
            r"```(?:[a-zA-Z0-9_+-]+)?\s*\n?(.*?)\n?\s*```",
            candidate,
            re.DOTALL,
        )
        if fence_match:
            candidate = fence_match.group(1).strip()
        else:
            # 2. No fence — find the first balanced {...} or [...] block.
            obj_match = re.search(r"(\{.*\}|\[.*\])", candidate, re.DOTALL)
            if obj_match:
                candidate = obj_match.group(1).strip()

        # First attempt: parse as-is
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass

        # Second attempt: repair common malformations and retry.
        repaired = LLMService._repair_json(candidate)
        try:
            return json.loads(repaired)
        except json.JSONDecodeError as e:
            # Emit enough context to diagnose without re-running the case.
            # Use repr() so control chars, unescaped quotes, and stray bytes
            # are visible in logs.
            start = max(0, e.pos - 100)
            end = min(len(repaired), e.pos + 100)
            logger.error(
                "llm_json_parse_failed",
                step_id=step_id or "<unknown>",
                provider=provider or "<unknown>",
                model=model or "<unknown>",
                pos=e.pos,
                line=e.lineno,
                col=e.colno,
                total_len=len(repaired),
                context=user_content(repaired[start:end]),
            )
            raise


# Statuses worth trying again. A refusal is deterministic — an
# undeclared step, an unrewritable string carrying personal data, a
# capability the manifest does not grant — so retrying one spends the
# backoff to be told the same thing, and hides the refusal behind a
# timeout. Status 0 is the gateway not answering at all.
_RETRYABLE_STATUSES = frozenset({0, 408, 409, 425, 429})


def _retryable(exc: LlmError) -> bool:
    return exc.status in _RETRYABLE_STATUSES or exc.status >= 500


async def call_with_retry(
    llm: LLMService,
    step_id: str,
    messages: list[dict],
    max_retries: int | None = None,
    backoff: int = 5,
) -> dict:
    # The service carries the run's retry count (K5b): the steps name no
    # count, so the tenant's setting is what they retry by.
    if max_retries is None:
        max_retries = llm.max_retries
    # There are no provider SDKs here to name exception classes from
    # (blueprint S4a): this agent calls the gateway, and the gateway
    # raises one error with the code and status on it. The three lazy
    # `import anthropic / openai / google.genai` lines that used to sit
    # here outlived the clients they belonged to — an import of a
    # package the image no longer installs, on a path no unit test
    # takes, which failed the first real run with
    # ModuleNotFoundError rather than anything about a model.
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            return await llm.call(step_id, messages)
        except LlmError as e:
            if not _retryable(e):
                raise
            last_exc = e
            if attempt == max_retries:
                raise
            await asyncio.sleep(backoff * (2 ** attempt))
        except (asyncio.TimeoutError, json.JSONDecodeError) as e:
            last_exc = e
            if attempt == max_retries:
                raise
            await asyncio.sleep(backoff * (2 ** attempt))
    assert last_exc is not None
    raise last_exc


def for_run(llm_capability=None, max_retries: int = 2) -> LLMService:
    """One service per invocation, holding that run's ``llm`` capability
    and its retry count (``max_retries_per_step``, K5b).

    This was a process-wide singleton whose capability was rebound on
    every run, justified by a comment saying the capability was
    stateless. That stopped being true in this batch: the capability
    carries the invocation's RUN TOKEN now, which is the run's identity
    at the gateway. With two in-process runs overlapping, the second
    binding replaced the first while the first was still executing, so
    its remaining model calls presented the other run's token — spend
    and telemetry attributed to the wrong run and the wrong tenant, and
    that tenant's model overrides applied (Codex P1).

    ``_used``, the model that answered each step, was shared the same
    way and is now per run as well. What is genuinely process-wide is
    the manifest's declared steps, and those stay on a class-level cache
    where nothing run-scoped can reach them.
    """
    return LLMService(llm_capability, max_retries=max_retries)
