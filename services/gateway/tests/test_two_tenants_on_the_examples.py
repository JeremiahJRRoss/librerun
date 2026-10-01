"""Two tenants, three container examples: the RUN TOKEN decides whose
configuration answers (blueprint S4a's two-tenant test, extended by S5-R
to all three container examples).

One container serves every tenant. That is the whole reason the agent key
alone may not call a model: it names an agent and nothing else, so it
cannot say whose data a call is about or whose configuration should
resolve it (D10). The run token beside it is what names the run, the
tenant and the trace.

S4a proved that against a synthetic agent, which proves the gateway's
half. This module proves the OTHER half — that the containers we ship
actually forward the token — by driving the real gateway with the real
manifests of the three shipped examples, on the real step ids they
declare:

* ``echo_container`` and ``llamaindex_summarize`` reach the gateway
  through the Python SDK (``ctx.llm.complete``, ``ctx.llm.client``);
* ``vercel_ai_answer_ts`` reaches it through the Vercel AI SDK's
  ``createOpenAI({ headers })`` — no LibreRun code at all.

Both paths end in the same two headers, so both are checked the same
way: seed two tenants with different models for one of the agent's own
declared steps, present each tenant's token in turn, and read back which
model the gateway resolved. Keyless throughout — the stub answers, and
still says which model it stood in for — so no provider is reached.

What this CANNOT check is the containers' own source, which is not
importable here (one is TypeScript). That half is the container battery
and the demo smoke; this is the contract they both have to satisfy.
"""
from __future__ import annotations

import json
import pathlib
import secrets
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text

from gateway.auth import RUN_TOKEN_HEADER

EXAMPLES_DIR = (
    pathlib.Path(__file__).resolve().parents[3] / "backend" / "agents" / "_examples"
)

# The container examples, by directory. Named rather than globbed: this
# list IS promise 1's container half, and a glob would quietly shrink
# with the tree instead of failing when an example goes missing.
CONTAINER_EXAMPLES = (
    "echo_container",
    "llamaindex_summarize",
    "vercel_ai_answer_ts",
)


def _manifest(directory: str) -> dict:
    """The example's manifest as the CHASSIS loads it — the gateway's
    authority is what the chassis accepted, so a hand-written copy here
    would be testing a fiction."""
    from app.agents.manifest import load_manifest

    return load_manifest(EXAMPLES_DIR / directory).model_dump(mode="json")


def _first_step(manifest: dict) -> str:
    steps = ((manifest.get("llm") or {}).get("steps")) or []
    assert steps, f"{manifest['id']} declares no llm.steps to retarget"
    return steps[0]["id"]


def test_every_container_example_is_present_and_calls_a_model():
    """A census, because everything below is parametrised over this list.

    An example that lost its manifest, its ``llm`` grant or its
    ``llm.steps`` would otherwise take its own check out of the suite and
    read as success — and this list is what the demo's five cards rest on.
    """
    for directory in CONTAINER_EXAMPLES:
        manifest = _manifest(directory)
        assert manifest["runtime"] == "container", directory
        assert "llm" in (manifest.get("capabilities") or []), directory
        assert _first_step(manifest), directory


async def _tenant(session) -> str:
    slug = f"s5r-{uuid.uuid4().hex[:8]}"
    return str(
        (
            await session.execute(
                text("INSERT INTO tenants (name, slug) VALUES (:n, :s) RETURNING id"),
                {"n": slug, "s": slug},
            )
        ).scalar_one()
    )


async def _override(session, tenant_id: str, agent_id: str, step_id: str, model: str):
    await session.execute(
        text(
            "INSERT INTO agent_step_configs (tenant_id, agent_id, step_id, model) "
            "VALUES (CAST(:t AS UUID), :a, :s, :m)"
        ),
        {"t": tenant_id, "a": agent_id, "s": step_id, "m": model},
    )


@pytest.fixture
def keyless(monkeypatch):
    """No provider is reached. The stub still reports the model it stood
    in for, which is exactly the value under test."""
    from gateway import steps

    monkeypatch.setattr(steps.settings, "LIBRERUN_STUB_LLM", True)


@pytest_asyncio.fixture
async def examples_installed(session):
    """The three examples installed the way a deployment installs them:
    their real manifests as snapshots, and one ``LIBRERUN_AGENT_KEY_<ID>``
    each — all in ONE reconcile, because reconciliation deletes the
    env-sourced rows the environment no longer names, so three separate
    calls would leave one agent with a key.

    Returns ``{agent_id: (key, manifest)}``.
    """
    from gateway import keys

    manifests = {_manifest(d)["id"]: _manifest(d) for d in CONTAINER_EXAMPLES}
    for agent_id, manifest in manifests.items():
        await session.execute(
            text(
                "INSERT INTO agent_manifests (agent_id, manifest, sha256, source) "
                "VALUES (:a, CAST(:m AS JSONB), :s, 'directory') "
                "ON CONFLICT (agent_id) DO UPDATE SET manifest = EXCLUDED.manifest, "
                "absent_at = NULL"
            ),
            {"a": agent_id, "m": json.dumps(manifest), "s": "0" * 64},
        )
    minted = {agent_id: keys.mint_key() for agent_id in manifests}
    await keys.reconcile_env_keys(
        session, {keys.env_name_for(a): v for a, v in minted.items()}
    )
    return {a: (minted[a], manifests[a]) for a in manifests}


@pytest.fixture
def mint_run_token(redis_client):
    """A run token exactly as ``app/agents/container.py`` mints one for an
    invocation of ``agent_id`` on behalf of ``tenant_id``."""
    client, written = redis_client

    async def mint(agent_id: str, tenant_id: str, grants: list[str]) -> str:
        token = secrets.token_urlsafe(24)
        trace_id = f"{secrets.randbits(128):032x}"
        key = f"run_token:{token}"
        await client.set(
            key,
            json.dumps(
                {
                    "run_id": str(uuid.uuid4()),
                    "tenant_id": tenant_id,
                    "agent_id": agent_id,
                    "grants": grants,
                    "user_id": str(uuid.uuid4()),
                    "run_number": "RUN-1000",
                    "deadline_seconds": 300,
                    "trace_id": trace_id,
                    "traceparent": f"00-{trace_id}-{secrets.randbits(64):016x}-01",
                    "state": "active",
                }
            ),
            ex=300,
        )
        written.append(key)
        return token

    return mint


@pytest.mark.parametrize("directory", CONTAINER_EXAMPLES)
@pytest.mark.asyncio
async def test_each_containers_call_resolves_the_calling_tenants_model(
    directory, session, client, examples_installed, mint_run_token, keyless
):
    key, manifest = examples_installed[_manifest(directory)["id"]]
    agent_id, step_id = manifest["id"], _first_step(manifest)
    grants = list(manifest.get("capabilities") or [])

    first, second = await _tenant(session), await _tenant(session)
    await _override(session, first, agent_id, step_id, "first-tenants-model")
    await _override(session, second, agent_id, step_id, "second-tenants-model")

    async def answered_for(tenant_id: str) -> dict:
        token = await mint_run_token(agent_id, tenant_id, grants)
        response = await client.post(
            "/v1/chat/completions",
            headers={
                # BOTH credentials, which is the shape every one of these
                # containers sends: the agent key as the bearer (the
                # framework's `apiKey`) and the invocation's run token
                # beside it.
                "Authorization": f"Bearer {key}",
                RUN_TOKEN_HEADER: token,
            },
            json={
                # The model field names a STEP. The containers send this
                # exact form — `ctx.llm.step()` in Python, the template
                # literal in server.ts — because a framework gives you a
                # model string and no per-call headers.
                "model": f"librerun/{step_id}",
                "messages": [{"role": "user", "content": "hello"}],
            },
        )
        assert response.status_code == 200, (directory, response.text)
        return response.json()

    assert (await answered_for(first))["librerun"]["model"] == "first-tenants-model"
    assert (await answered_for(second))["librerun"]["model"] == "second-tenants-model"
    # …and the step is the agent's own, not one the gateway invented.
    assert (await answered_for(first))["librerun"]["step_id"] == step_id


@pytest.mark.parametrize("directory", CONTAINER_EXAMPLES)
@pytest.mark.asyncio
async def test_each_containers_agent_key_alone_cannot_call_a_model(
    directory, client, examples_installed, keyless
):
    """The other half of the same rule, per example: without the run token
    there is no tenant, so there is no configuration to resolve and the
    call is refused — on a single-tenant demo too. A container that
    dropped the token would reach this refusal rather than silently
    borrowing somebody's defaults."""
    key, manifest = examples_installed[_manifest(directory)["id"]]

    response = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={
            "model": f"librerun/{_first_step(manifest)}",
            "messages": [{"role": "user", "content": "hello"}],
        },
    )

    assert response.status_code == 401, (directory, response.text)
    assert response.json()["error"]["code"] == "run_token_required"


@pytest.mark.parametrize("directory", CONTAINER_EXAMPLES)
@pytest.mark.asyncio
async def test_one_containers_key_cannot_spend_under_anothers_token(
    directory, session, client, examples_installed, mint_run_token, keyless
):
    """Three agents, one gateway: a key for one example presented with a
    run token minted for another is a misconfigured container or an
    attempt to spend a neighbour's budget under its name. Neither may
    resolve a step."""
    manifest = _manifest(directory)
    key, _ = examples_installed[manifest["id"]]
    other = next(
        _manifest(d)["id"] for d in CONTAINER_EXAMPLES if _manifest(d)["id"] != manifest["id"]
    )
    tenant = await _tenant(session)
    token = await mint_run_token(other, tenant, ["llm"])

    response = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}", RUN_TOKEN_HEADER: token},
        json={
            "model": f"librerun/{_first_step(manifest)}",
            "messages": [{"role": "user", "content": "hello"}],
        },
    )

    assert response.status_code == 401, (directory, response.text)


# --------------------------------------------------------------------------
# What a shipped example may put on the wire (found by driving one)
# --------------------------------------------------------------------------
#
# These live here rather than with the gateway's own redaction tests
# because they are facts about the EXAMPLES: the first pins the gateway
# behaviour the examples have to live with, and the second pins the
# examples against it. Both exist because the TypeScript example's very
# first full-stack run answered every question from its fallback and said
# only "AI_APICallError" about why — a twenty-minute CI cycle to learn
# one refusal code.


def _tool(parameters: dict) -> dict:
    return {
        "type": "function",
        "function": {
            "name": "service_status",
            "description": "The current status of one of this deployment's services.",
            "parameters": parameters,
        },
    }


SHAPE = {
    "type": "object",
    "properties": {"service": {"type": "string", "description": "the service name"}},
    "required": ["service"],
    "additionalProperties": False,
}


@pytest.mark.asyncio
async def test_a_tool_schema_carrying_a_meta_schema_url_is_refused(
    client, examples_installed, mint_run_token, session, keyless
):
    """`{"$schema": "http://json-schema.org/draft-07/schema#"}` is what a
    zod schema becomes, and it is refused — every call, before any model
    sees it.

    The gateway is not wrong: a schema keyword has to reach the model
    verbatim, so that position cannot be rewritten, and a URL in a
    position that cannot be rewritten is refused rather than passed. It
    is nonetheless the first thing an author writing a TypeScript tool
    walks into, because converting a zod schema emits that key by
    default. Pinned so the day the platform decides differently, this
    test says so and the example's comment gets revisited instead of
    quietly becoming a lie.
    """
    key, manifest = examples_installed["vercel-answer"]
    token = await mint_run_token(
        "vercel-answer", await _tenant(session), list(manifest["capabilities"])
    )

    response = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}", RUN_TOKEN_HEADER: token},
        json={
            "model": "librerun/answer",
            "messages": [{"role": "user", "content": "hello"}],
            "tools": [
                _tool(dict(SHAPE, **{"$schema": "http://json-schema.org/draft-07/schema#"}))
            ],
        },
    )

    assert response.status_code == 400, response.text
    error = response.json()["error"]
    assert error["code"] == "pii_in_identifier", error
    assert error["param"].endswith("$schema"), error


@pytest.mark.asyncio
async def test_the_same_tool_without_it_reaches_the_model(
    client, examples_installed, mint_run_token, session, keyless
):
    """The positive control, and the shape the example actually sends.
    Without it the test above would pass against a gateway that refused
    every tool call for any reason at all."""
    key, manifest = examples_installed["vercel-answer"]
    token = await mint_run_token(
        "vercel-answer", await _tenant(session), list(manifest["capabilities"])
    )

    response = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}", RUN_TOKEN_HEADER: token},
        json={
            "model": "librerun/answer",
            "messages": [{"role": "user", "content": "hello"}],
            "tools": [_tool(SHAPE)],
            "tool_choice": {"type": "function", "function": {"name": "service_status"}},
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["choices"][0]["message"]["tool_calls"], response.text


@pytest.mark.parametrize(
    "source",
    [
        "vercel_ai_answer_ts/server.ts",
        "llamaindex_summarize/agent.py",
    ],
)
def test_no_example_puts_a_meta_schema_url_on_the_wire(source):
    """The cheap half of the same guard, and the one that fails in a
    second rather than in a full-stack run twenty minutes long.

    Every shipped example declares tool and response schemas by hand, and
    none of them carries `$schema`. An author who reaches for the
    idiomatic converter reintroduces it — so this is the line that says
    not to.
    """
    text = (EXAMPLES_DIR / source).read_text()
    assert '"$schema"' not in text and "'$schema'" not in text, (
        f"{source} declares a schema with a `$schema` key. Its value is a "
        f"URL in a position the gateway may not rewrite, so every model "
        f"call carrying it is refused 400 pii_in_identifier."
    )


def test_the_typescript_example_declares_its_schema_rather_than_converting_one():
    """…and names the mechanism, so the rule above is not folklore."""
    source = (EXAMPLES_DIR / "vercel_ai_answer_ts" / "server.ts").read_text()
    package = json.loads(
        (EXAMPLES_DIR / "vercel_ai_answer_ts" / "package.json").read_text()
    )

    assert "jsonSchema" in source, source[:200]
    assert "zod" not in package.get("dependencies", {}), package["dependencies"]
    assert "pii_in_identifier" in source, (
        "the example drops zod for a reason; the reason belongs beside the code"
    )

