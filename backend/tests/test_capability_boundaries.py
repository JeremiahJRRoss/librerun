"""Blueprint B13: agents reach the platform only through the capability
façade.

Two halves:

1. **The import boundary** — the accept gate's "import-linter" check,
   implemented as an AST walk so it runs in every pytest invocation and
   in CI with no extra tooling: no module under ``agents/vita_v1``
   (tests excluded) may import chassis internals — ``app.config``,
   ``app.database``, ``app.redis``, ``app.services``, ``app.models``.
   The allowed surface is ``app.agents.protocol``, ``app.capabilities``,
   and the logging helpers.
2. **The façade itself** — grant enforcement, the case-store roundtrip,
   and credential lookup.
"""
from __future__ import annotations

import ast
import uuid
from pathlib import Path

import pytest

from app import capabilities as caps_mod
from app.capabilities import CapabilityNotGranted
from app.services import run_boundary

BACKEND_DIR = Path(__file__).resolve().parents[1]
VITA_DIR = BACKEND_DIR / "agents" / "vita_v1"

FORBIDDEN_PREFIXES = (
    "app.config",
    "app.database",
    "app.redis",
    "app.services",
    "app.models",
)


def _imported_names(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            out.append(node.module)
    return out


def test_vita_imports_only_the_facade():
    """The B13 accept gate: VITA touches no chassis internals — services,
    config, models, and infrastructure are reachable only through
    ``app.capabilities`` (and the protocol/logging surface)."""
    violations: list[str] = []
    for path in sorted(VITA_DIR.rglob("*.py")):
        if "tests" in path.parts or "build" in path.parts:
            continue
        for name in _imported_names(path):
            if name.startswith(FORBIDDEN_PREFIXES):
                violations.append(f"{path.relative_to(BACKEND_DIR)}: {name}")
    assert not violations, (
        "agent code imports chassis internals (use the capability façade):\n"
        + "\n".join(violations)
    )


# --------------------------- façade behavior ---------------------------------


def _facade(grants: list[str]) -> caps_mod.Capabilities:
    return caps_mod.for_run(
        run_id=uuid.uuid4(), tenant_id=uuid.uuid4(), agent_id="probe-agent", grants=grants
    )


def test_ungranted_capability_raises():
    caps = _facade(["llm"])
    with pytest.raises(CapabilityNotGranted, match="'kb' is not granted"):
        _ = caps.kb
    # Granted access works.
    assert callable(caps.llm.complete)


def test_the_facade_hands_out_no_provider_credential():
    """Blueprint S4a: ``credential()`` is gone. A provider key an
    in-process agent can read is a key EVERY in-process agent has —
    they all share this process — so the gateway holds them and the
    façade brokers calls instead of secrets."""
    caps = _facade(["llm"])

    assert not hasattr(caps.llm, "credential")
    assert not hasattr(caps.llm, "_PROVIDERS")
    # Nothing on the granted surface returns a secret by another name.
    public = [name for name in dir(caps.llm) if not name.startswith("_")]
    assert public == sorted(["complete", "stub_mode"])


def test_unknown_grant_slugs_are_tolerated():
    """Declarative-era slugs (VITA's ``web-search``) grant nothing but
    must not break façade construction."""
    caps = _facade(["llm", "web-search"])
    assert caps.granted("web-search")
    with pytest.raises(CapabilityNotGranted):
        _ = caps.run_store


class _Pipe:
    """The transaction `run_hash_write` opens, over a local fake's dict.

    Applies what it is queued and records the expiry, so a guard can read
    the ttl back rather than trusting that a call happened.
    """

    def __init__(self, parent):
        self._parent = parent
        self._queued = []

    def hset(self, key, field, value):
        self._queued.append(("hset", key, field, value))
        return self

    def expire(self, key, ttl):
        self._queued.append(("expire", key, ttl))
        return self

    async def execute(self):
        replies = []
        for command in self._queued:
            if command[0] == "hset":
                _, key, field, value = command
                self._parent.h.setdefault(key, {})[field] = value
                replies.append(1)
            else:
                _, key, ttl = command
                self._parent.ttls[key] = ttl
                replies.append(True)
        self._queued = []
        return replies


@pytest.mark.asyncio
async def test_run_store_roundtrip(monkeypatch):
    class _FakeRedis:
        def __init__(self):
            self.h: dict[str, dict[str, str]] = {}
            self.ttls: dict[str, int] = {}

        def pipeline(self, transaction=False):
            assert transaction, "run_hash_write must ask for a transaction"
            return _Pipe(self)

        async def hset(self, key, field, value):
            self.h.setdefault(key, {})[field] = value

        async def hget(self, key, field):
            return self.h.get(key, {}).get(field)

        async def hgetall(self, key):
            return dict(self.h.get(key, {}))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    fake = _FakeRedis()

    # `_redis_client`, NOT `app.redis.get_redis`: the run store opens a
    # per-call client of its own (it is invoked from many event loops),
    # so patching the module-global accessor installed a double that was
    # never reached — this test was quietly exercising the real server
    # while appearing to use a fake, and no assertion about what was SENT
    # could have failed.
    monkeypatch.setattr(caps_mod, "_redis_client", lambda: fake)

    caps = _facade(["run_store"])
    await caps.run_store.set("marker", {"n": 1})
    assert await caps.run_store.get("marker") == {"n": 1}
    assert await caps.run_store.get("absent") is None
    # The double records the expiry rather than discarding it, because a
    # double that throws away what it is told cannot fail when production
    # stops saying it (S5-E, §12 206(d)).
    (key,) = fake.ttls
    assert key.endswith(":kv")
    assert fake.ttls[key] == run_boundary.RUN_KEY_TTL_SECONDS
    assert await caps.run_store.all() == {"marker": {"n": 1}}


def test_vita_manifest_grants_cover_facade_use():
    """VITA's manifest must grant what its code consumes: llm, kb,
    progress, audit."""
    import yaml

    manifest = yaml.safe_load((VITA_DIR / "agent.yaml").read_text())
    grants = set(manifest["capabilities"])
    assert {"llm", "kb", "progress", "audit"} <= grants


# --------------------------- grant enforcement in VITA -----------------------


@pytest.mark.asyncio
async def test_vita_search_service_honours_the_manifest_grant():
    """B13 audit follow-up: VITA must reach kb/llm through the GRANTED
    façade. Constructing the capability classes directly would read the
    same data while bypassing the grant the manifest declares, which
    would make the whole capabilities list decorative for in-process
    agents."""
    from agents.vita_v1.search_service import SearchService

    ungranted = _facade(["llm"])  # no 'kb'
    service = SearchService(ungranted)
    with pytest.raises(CapabilityNotGranted, match="'kb' is not granted"):
        service.pinecone_available()
    with pytest.raises(CapabilityNotGranted, match="'kb' is not granted"):
        await service.search_pinecone(["q"])

    granted = _facade(["llm", "kb"])
    assert SearchService(granted).pinecone_available() in (True, False)


def test_vita_llm_service_uses_the_granted_capability():
    from agents.vita_v1.llm_service import for_run

    caps = _facade(["llm"])
    service = for_run(caps.llm)
    assert service.llm_capability is caps.llm


def test_kb_search_is_bound_to_the_run_tenant(monkeypatch):
    """The façade's kb is already tenant-scoped, so an agent cannot aim a
    search at another tenant's namespace by passing a different id."""
    from agents.vita_v1.search_service import SearchService

    caps = _facade(["kb"])
    seen = {}

    async def _search(queries, top_k=10):
        seen["queries"] = queries
        return []

    monkeypatch.setattr(caps.kb, "search", _search)
    import asyncio

    other_tenant = uuid.uuid4()
    asyncio.run(SearchService(caps).search_pinecone(["q"], other_tenant))
    assert seen["queries"] == ["q"]


def test_case_store_is_a_one_release_alias_of_run_store():
    """Blueprint S1 (L18): a manifest that still grants ``case_store`` gets
    ``run_store``, and ``caps.case_store`` still resolves to the same
    member. Both spellings go at v1.1."""
    caps = _facade(["case_store"])
    assert caps.granted("run_store")
    assert caps.run_store is caps.case_store
    assert caps_mod.normalize_grants(["case_store", "run_store", "kb"]) == ["run_store", "kb"]
    # The alias is not a third capability: ungranted stays ungranted.
    with pytest.raises(CapabilityNotGranted):
        _ = _facade(["kb"]).case_store


def test_kb_stamp_writes_openinference_documents_on_the_current_span():
    """Blueprint S2: ``kb.stamp`` is the one place retrieval documents are
    stamped — for the capability's own results and an agent's own
    retrieval alike. Key variants, the per-span cap, model objects, and
    the no-span no-op are all pinned here."""
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from pydantic import BaseModel

    class _Doc(BaseModel):
        url: str
        snippet: str
        relevance_score: float

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("t")
    kb = caps_mod.KbCapability(uuid.uuid4())

    # No recording span: nothing to stamp, nothing raised.
    assert kb.stamp([{"url": "https://x"}]) == 0

    with tracer.start_as_current_span("step"):
        docs = [
            {"url": "https://a", "snippet": "s" * 2000, "relevance_score": "0.5"},
            {"id": "doc-2", "content": "c2", "score": 0.25},
            {"text": "t3"},  # no id — still a document
            "not a dict",  # skipped, does not consume an index
            _Doc(url="https://d", snippet="s4", relevance_score=0.75),
        ] + [{"url": f"https://more/{i}"} for i in range(30)]
        stamped = kb.stamp(docs)
    assert stamped == caps_mod.KbCapability.MAX_STAMPED_DOCUMENTS
    (span,) = exporter.get_finished_spans()
    a = dict(span.attributes)
    assert a["retrieval.documents.0.document.id"] == "https://a"
    assert len(a["retrieval.documents.0.document.content"]) == 1000
    assert a["retrieval.documents.0.document.score"] == 0.5
    assert a["retrieval.documents.1.document.id"] == "doc-2"
    assert a["retrieval.documents.1.document.content"] == "c2"
    assert a["retrieval.documents.1.document.score"] == 0.25
    assert a["retrieval.documents.2.document.content"] == "t3"
    assert "retrieval.documents.2.document.id" not in a
    assert a["retrieval.documents.3.document.id"] == "https://d"
    assert f"retrieval.documents.{caps_mod.KbCapability.MAX_STAMPED_DOCUMENTS}.document.id" not in a


# --------------------------------------------------------------------------
# S4: the pii grant, and the walk at the façade's write paths
# --------------------------------------------------------------------------

FIXTURE_EMAIL = "pii.fixture@example.com"


def test_pii_is_a_known_capability_granted_like_the_others():
    from app.capabilities import KNOWN_CAPABILITIES, CapabilityNotGranted

    assert "pii" in KNOWN_CAPABILITIES
    caps = _facade(["pii"])
    assert hasattr(caps.pii, "redact")
    with pytest.raises(CapabilityNotGranted):
        _facade(["kb"]).pii


@pytest.mark.asyncio
async def test_pii_redact_returns_the_intake_pipelines_redaction():
    from app.services import pii_service

    text = f"mail me at {FIXTURE_EMAIL}"
    assert await _facade(["pii"]).pii.redact(text) == pii_service.redact(text)[0]
    assert FIXTURE_EMAIL not in await _facade(["pii"]).pii.redact(text)


@pytest.mark.asyncio
async def test_run_store_set_walks_the_value_and_refuses_a_flagged_key(monkeypatch):
    from app.services.pii_service import PiiRefused

    class _FakeRedis:
        def __init__(self):
            self.h: dict[str, dict[str, str]] = {}
            self.ttls: dict[str, int] = {}

        def pipeline(self, transaction=False):
            assert transaction, "run_hash_write must ask for a transaction"
            return _Pipe(self)

        async def hset(self, key, field, value):
            self.h.setdefault(key, {})[field] = value

        async def hget(self, key, field):
            return self.h.get(key, {}).get(field)

        async def hgetall(self, key):
            return dict(self.h.get(key, {}))

        async def expire(self, key, ttl):
            self.ttls[key] = ttl

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    fake = _FakeRedis()
    import app.capabilities as caps_mod

    monkeypatch.setattr(caps_mod, "_redis_client", lambda: fake)
    caps = _facade(["run_store"])
    await caps.run_store.set("note", {"text": f"call {FIXTURE_EMAIL}", "n": 2})
    stored = await caps.run_store.get("note")
    assert FIXTURE_EMAIL not in stored["text"] and stored["n"] == 2
    with pytest.raises(PiiRefused) as exc:
        await caps.run_store.set(FIXTURE_EMAIL, 1)
    assert exc.value.reason == "pii_in_store" and exc.value.argument == "key"
    with pytest.raises(PiiRefused) as exc:
        await caps.run_store.set("contact", {"phone": 2125551234})
    assert exc.value.reason == "pii_in_store" and exc.value.argument == "value"
    # The hash's field names, not only its values, are free of the fixture.
    (fields,) = fake.h.values()
    assert FIXTURE_EMAIL not in " ".join(fields)
    assert "contact" not in fields


@pytest.mark.asyncio
async def test_progress_update_refuses_a_flagged_step_id_and_redacts_the_detail(monkeypatch):
    from app.services.pii_service import PiiRefused

    class _FakeRedis:
        def __init__(self):
            self.h: dict[str, dict[str, str]] = {}
            self.ttls: dict[str, int] = {}

        def pipeline(self, transaction=False):
            assert transaction, "run_hash_write must ask for a transaction"
            return _Pipe(self)

        async def hset(self, key, field, value):
            self.h.setdefault(key, {})[field] = value

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    fake = _FakeRedis()
    import app.capabilities as caps_mod

    monkeypatch.setattr(caps_mod, "_redis_client", lambda: fake)
    caps = _facade(["progress"])
    await caps.progress.update("gather", "running", f"asking {FIXTURE_EMAIL}")
    (fields,) = fake.h.values()
    assert list(fields) == ["gather"]
    assert FIXTURE_EMAIL not in fields["gather"]
    with pytest.raises(PiiRefused) as exc:
        await caps.progress.update(FIXTURE_EMAIL, "running")
    assert exc.value.reason == "pii_in_progress"
    assert FIXTURE_EMAIL not in " ".join(fields)
