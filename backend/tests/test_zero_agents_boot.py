"""Blueprint B11: the chassis is healthy with zero agents.

Four invariants:

1. **Import isolation** — importing the whole chassis (``app.main``) must
   not import a single ``agents.*`` module. This is the self-containment
   proof: the chassis reaches agents only through the registry at
   runtime, never at import time. Verified in a subprocess so this test
   file's own imports can't contaminate the check.
2. **Empty discovery is clean** — ``discover_agents`` over an empty
   directory returns 0 without raising, leaving an empty registry.
3. **The API degrades honestly with no agents** — ``GET /agents`` serves
   ``[]`` and ``POST /runs`` refuses with a clear 400 (no hardcoded
   default agent to fall back to, blueprint B11). Checked twice: fast
   in-process against a minimal router harness (quick failure
   localization), and again inside invariant 4 against the real app.
4. **The production app boots through its real lifespan with zero
   agents** — a subprocess imports ``app.main`` and enters the actual
   lifespan (agent discovery, redis client setup, shutdown) via
   ``TestClient``'s context manager, with no database or redis server
   available: the lifespan's external touches are lazy or
   failure-tolerated by design, and that tolerance is part of the boot
   contract this test pins. In CI the workflow physically empties
   ``backend/agents/`` first, so discovery runs against the real,
   emptied directory; on a dev checkout (agents still installed) the
   subprocess redirects ``registry.AGENTS_DIR`` at an empty temp dir —
   the directory contents are the experiment's variable, not a mock of
   the app. Since B12 discovery also consults pip entry points, so the
   subprocess empties that seam too — "zero agents" means neither
   install mode provides one.
"""
from __future__ import annotations

import os
import secrets
import subprocess
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from starlette.testclient import TestClient

from app.agents import registry
from app.database import get_db
from app.middleware import get_current_user
from app.routers import agents as agents_router
from app.routers import runs as runs_router

BACKEND_DIR = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _clean_registry():
    registry._clear_registry_for_tests()
    yield
    registry._clear_registry_for_tests()


def test_importing_the_chassis_imports_no_agent_modules():
    code = (
        "import sys\n"
        "import app.main\n"
        "leaked = [m for m in sys.modules if m == 'agents' or m.startswith('agents.')]\n"
        "assert not leaked, f'chassis import pulled in agent modules: {leaked}'\n"
        "print('clean')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=BACKEND_DIR,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert "clean" in proc.stdout


def test_discovery_over_empty_directory_is_clean(tmp_path):
    empty = tmp_path / "agents"
    empty.mkdir()
    assert registry.discover_agents(empty) == 0
    assert registry.list_agents() == []


def _zero_agent_app() -> TestClient:
    tenant_id = uuid.uuid4()
    user = SimpleNamespace(
        id=uuid.uuid4(), tenant_id=tenant_id, email="u@example.com", role="customer"
    )

    async def _db_dep():
        yield SimpleNamespace()

    async def _user_dep(request: Request):
        request.state.tenant_id = tenant_id
        return user

    app = FastAPI()
    app.include_router(agents_router.router)
    app.include_router(runs_router.router)
    app.dependency_overrides[get_db] = _db_dep
    app.dependency_overrides[get_current_user] = _user_dep
    return TestClient(app)


def test_agents_endpoint_serves_empty_list_with_zero_agents():
    client = _zero_agent_app()
    r = client.get("/agents")
    assert r.status_code == 200
    assert r.json() == []


def test_create_run_refuses_cleanly_with_zero_agents():
    client = _zero_agent_app()
    r = client.post("/runs", json={"anything": 1})
    assert r.status_code == 400
    assert "agent_id is required" in r.json()["detail"]


# The production boot check (invariant 4). Runs in a subprocess so it
# exercises a pristine interpreter exactly the way uvicorn would see it,
# and so this file's own imports and registry fixtures can't leak in.
_REAL_LIFESPAN_CODE = """
import sys
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace

import app.main
from app.agents import registry
from app.database import get_db
from app.middleware import get_current_user

# The agents directory's CONTENTS are the variable under test. CI empties
# backend/agents/ before running this file, so discovery walks the real,
# empty directory. On a dev checkout the real agents are still installed;
# redirect discovery at an empty temp dir instead of deleting anything.
def _has_agent_dirs(root):
    return root.is_dir() and any(
        p.is_dir() and not p.name.startswith(("_", ".")) for p in root.iterdir()
    )

if _has_agent_dirs(registry.AGENTS_DIR):
    registry.AGENTS_DIR = Path(tempfile.mkdtemp())

# Same for the pip install mode (blueprint B12): simulate 'nothing
# installed' through the entry-point seam so a vita-agent (or any other
# agent distribution) present in this environment can't break the
# zero-agent premise. CI environments install none anyway.
registry._agent_entry_points = lambda: []

# Only auth and the DB session are stubbed — the external services Codex
# would call "mocked". Everything else (lifespan, middleware, routers,
# redis client setup) is the production object graph.
from fastapi import Request

tenant_id = uuid.uuid4()
user = SimpleNamespace(
    id=uuid.uuid4(), tenant_id=tenant_id, email="u@example.com", role="customer"
)

async def _db_dep():
    yield SimpleNamespace()

async def _user_dep(request: Request):
    request.state.tenant_id = tenant_id
    return user

app.main.app.dependency_overrides[get_db] = _db_dep
app.main.app.dependency_overrides[get_current_user] = _user_dep

from starlette.testclient import TestClient

# The context manager runs the REAL lifespan: startup (discover_agents,
# get_redis) on enter, shutdown (span flush, close_redis) on exit. Any
# exception in either direction fails this subprocess.
with TestClient(app.main.app) as client:
    assert registry.list_agents() == [], [a.agent_id for a in registry.list_agents()]

    health = client.get("/api/v1/health")
    assert health.status_code == 200, health.text

    agents = client.get("/api/v1/agents")
    assert agents.status_code == 200, agents.text
    assert agents.json() == [], agents.text

    created = client.post("/api/v1/runs", json={"anything": 1})
    assert created.status_code == 400, created.text
    assert "agent_id is required" in created.json()["detail"], created.text

# Import isolation must hold through the whole boot/serve/shutdown cycle,
# not just at import time: with zero agents on disk, nothing may have
# pulled in an ``agents.*`` module.
leaked = [m for m in sys.modules if m == "agents" or m.startswith("agents.")]
assert not leaked, f"zero-agent boot pulled in agent modules: {leaked}"

print("zero-agents-lifespan-ok")
"""


def test_real_app_boots_through_lifespan_with_zero_agents():
    # A real deployment has a real secret; the shipped default is refused
    # outside demo mode since blueprint S3, and this test is about agents.
    env = {**os.environ, "APP_SECRET_KEY": secrets.token_hex(32)}
    proc = subprocess.run(
        [sys.executable, "-c", _REAL_LIFESPAN_CODE],
        cwd=BACKEND_DIR,
        capture_output=True,
        text=True,
        timeout=180,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    assert "zero-agents-lifespan-ok" in proc.stdout
