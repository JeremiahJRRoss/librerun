"""the gateway's three tables: manifest snapshots, step overrides, agent keys

Revision ID: 0014_agent_gateway_tables
Revises: 0013_run_root_trace
Create Date: 2026-09-13 00:00:00.000000

Blueprint S4a (L23/L25): the LLM gateway is a **separate service**, so
everything it needs to authorize and route a call has to be readable
from the database. The in-process agent registry
(``backend/app/agents/registry.py``) is not: it lives in the backend
process and dies with it. Three tables carry what the gateway asks:

``agent_manifests`` — one row per agent id, holding the validated
manifest as JSONB with its sha256. Written by the backend at discovery
(directory, entry point and container alike, the one place every agent
passes through) and read by the gateway at request time for the ``llm``
grant, the ``llm.steps[]`` defaults and ``llm.redact_outbound``. Rows
are never deleted: an agent that disappears is stamped ``absent_at``
and the stamp is cleared when it comes back, so an uninstall does not
take the agent's keys and step overrides with it.

``agent_step_configs`` — the admin's per-step model choices, **keyed by
tenant**. Until this batch the admin route called
``agent.update_step_configs()`` on the process-global agent instance,
whose one implementation wrote one shared file, so a second tenant's
edit silently overwrote the first. The unique key ``(tenant_id,
agent_id, step_id)`` is the fix, and the manifest's ``llm.steps``
defaults are the fallback when no row exists.

``agent_keys`` — the per-agent credential a framework that reads
``OPENAI_API_KEY`` from its environment presents (D10). The value is
never stored: only its sha256, plus the eight characters after
``lr_agent_`` so the admin page can name the key an operator is
holding. ``key_hash`` is unique **across the table**, so a presented
key maps to exactly one agent and two agents can never share a value;
``(agent_id, key_hash)`` is the pair env reconciliation upserts and
deletes by; and one partial unique index per role gives an agent at
most one ``current`` and one ``previous`` key — the cardinality
rotation needs and revocation-by-replacement assumes.

The upgrade also **seeds** ``agent_step_configs`` for every tenant that
exists, from the legacy per-agent ``config.json`` ``pipeline`` block:
the one configuration every tenant's admin read and wrote until now,
edits included. It is resolved the way the legacy store resolved it —
the overlay under ``LIBRERUN_STATE_DIR`` (S2 put that directory in a
persisted volume for exactly this reason) if one exists, else the
packaged file — and inserted ``ON CONFLICT DO NOTHING``, so an upgrade
changes nothing visible for any tenant and a re-run changes nothing at
all. The scan is by convention, not by name: any agent directory on
``LIBRERUN_AGENTS_PATH`` carrying an ``agent.yaml`` and a ``config.json``
with a ``pipeline`` object is seeded, and a tree with no such agent
seeds nothing.

Guarded and idempotent, in the pattern of 002-013: a fresh install
loads the head schema file, which already carries the three tables, and
every statement here no-ops on it.
"""
import json
import os
import re
from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "0014_agent_gateway_tables"
down_revision = "0013_run_root_trace"
branch_labels = None
depends_on = None

# The manifest id charset (``backend/app/agents/manifest.py``), repeated
# rather than imported: a migration is frozen at the revision it was
# written for, and must not change meaning when the validator does.
_AGENT_ID = re.compile(r"^[a-z0-9][a-z0-9-]*$")

# The editable fields, in the order the table declares them.
_STEP_FIELDS = ("provider", "model", "temperature", "max_tokens", "timeout_seconds")


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_manifests (
            agent_id        VARCHAR(100) PRIMARY KEY,
            manifest        JSONB NOT NULL,
            sha256          CHAR(64) NOT NULL,
            source          VARCHAR(32) NOT NULL,
            discovered_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            absent_at       TIMESTAMPTZ
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_step_configs (
            tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            agent_id        VARCHAR(100) NOT NULL,
            step_id         VARCHAR(100) NOT NULL,
            provider        VARCHAR(50),
            model           VARCHAR(200),
            temperature     DOUBLE PRECISION,
            max_tokens      INTEGER,
            timeout_seconds INTEGER,
            updated_by      UUID REFERENCES users(id),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (tenant_id, agent_id, step_id)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_keys (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            agent_id        VARCHAR(100) NOT NULL,
            key_hash        CHAR(64) NOT NULL UNIQUE,
            key_prefix      VARCHAR(8) NOT NULL,
            source          VARCHAR(16) NOT NULL CHECK (source IN ('env', 'admin')),
            role            VARCHAR(16) NOT NULL CHECK (role IN ('current', 'previous')),
            issued_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            issued_by       UUID REFERENCES users(id),
            previous_since  TIMESTAMPTZ,
            previous_until  TIMESTAMPTZ,
            last_used_at    TIMESTAMPTZ
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_keys_agent_hash "
        "ON agent_keys (agent_id, key_hash)"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_keys_current "
        "ON agent_keys (agent_id) WHERE role = 'current'"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_keys_previous "
        "ON agent_keys (agent_id) WHERE role = 'previous'"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_manifests_present "
        "ON agent_manifests (agent_id) WHERE absent_at IS NULL"
    )

    _seed_step_configs(op.get_bind())


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS agent_keys")
    op.execute("DROP TABLE IF EXISTS agent_step_configs")
    op.execute("DROP TABLE IF EXISTS agent_manifests")


# --------------------------------------------------------------------------
# The data migration: the legacy file's pipeline block becomes rows, once
# per tenant. Everything below reads the filesystem the deployment gives
# the backend process; nothing here names an agent.
# --------------------------------------------------------------------------


def _agents_dirs() -> list[Path]:
    """The discovery roots, resolved as ``registry._resolve_agents_dirs``
    resolves them: ``LIBRERUN_AGENTS_PATH`` as an ``os.pathsep`` list when
    set, else the ``agents/`` directory beside ``app/``."""
    configured = os.environ.get("LIBRERUN_AGENTS_PATH", "")
    dirs = [
        Path(part.strip()).expanduser()
        for part in configured.split(os.pathsep)
        if part.strip()
    ]
    if dirs:
        return dirs
    # backend/alembic/versions/ -> backend/ -> backend/agents
    return [Path(__file__).resolve().parents[2] / "agents"]


def _state_root() -> Path:
    """Where the legacy per-agent config overlay lives, resolved as the
    legacy store resolved it."""
    configured = os.environ.get("LIBRERUN_STATE_DIR")
    if configured:
        return Path(configured).expanduser()
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg).expanduser() if xdg else Path("~/.local/state").expanduser()
    return base / "librerun"


def _manifest_id(agent_dir: Path) -> str | None:
    """The ``id:`` of ``agent_dir/agent.yaml``, read without a YAML parser.

    A migration that imported the manifest loader would change meaning
    whenever the loader does. The id is a top-level scalar on the
    charset above, so a line match is exact enough and a file that does
    not offer one is skipped rather than guessed at.
    """
    path = agent_dir / "agent.yaml"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        match = re.match(r"^id:\s*[\"']?([A-Za-z0-9_-]+)[\"']?\s*(?:#.*)?$", line)
        if match:
            value = match.group(1)
            return value if _AGENT_ID.match(value) else None
    return None


def _pipeline_at(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    pipeline = payload.get("pipeline")
    return pipeline if isinstance(pipeline, dict) else {}


def _manifest_steps(path: Path) -> dict:
    """The manifest's ``llm.steps[]`` as ``{step_id: {field: value}}``.

    The same five fields the legacy ``pipeline`` block carried, under the
    same names — this batch moved that block into the manifest without
    renaming anything, which is what makes it a like-for-like comparison
    base.

    ``yaml.safe_load`` is a parser, not the manifest LOADER: nothing here
    depends on how the application validates or defaults a manifest, so
    the migration cannot change meaning when that code does. Any failure
    yields ``{}``, because a malformed manifest must not break an
    upgrade.
    """
    try:
        import yaml

        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    if not isinstance(payload, dict):
        return {}
    steps = ((payload.get("llm") or {}) if isinstance(payload.get("llm"), dict) else {}).get(
        "steps"
    )
    if not isinstance(steps, list):
        return {}
    out: dict = {}
    for step in steps:
        if not isinstance(step, dict):
            continue
        step_id = step.get("id")
        if not isinstance(step_id, str) or not step_id:
            continue
        out[step_id] = {field: step.get(field) for field in _STEP_FIELDS}
    return out


def _legacy_pipeline(agent_dir: Path) -> tuple[dict, dict]:
    """``(effective, packaged)`` step defaults for ``agent_dir``.

    Both, not one, because a row in ``agent_step_configs`` records a
    DIVERGENCE. Returning only the effective block meant that an
    installation which had never edited anything had every packaged
    value written as an explicit tenant override, for every tenant
    present at migration time — pinning the agent's defaults for exactly
    the installations that never chose them, so the author's next
    release can never reach them (Codex P2).

    **And then the comparison base was emptied by this same batch.** The
    packaged ``config.json`` no longer carries a ``pipeline`` block — it
    moved into the manifest — so ``packaged`` read as ``{}`` on the
    shipped tree. On an upgraded installation the persisted overlay
    still holds the OLD full pipeline (an admin who changed one
    unrelated setting has a complete copy of the defaults in there), so
    every field differed from nothing and the pinning came straight
    back, one release later and harder to see. The fix that was checked
    against a tree that no longer exists is not a fix (Codex P2, again).

    So the base is whichever of the two the installation actually has:
    the packaged ``pipeline`` block for a tree still in the old shape (a
    third-party agent that has not migrated), otherwise the manifest's
    ``llm.steps[]``, which is where those very defaults now live.
    """
    overlay = _state_root() / "agents" / agent_dir.name / "config.json"
    packaged = _pipeline_at(agent_dir / "config.json")
    if not packaged:
        packaged = _manifest_steps(agent_dir / "agent.yaml")
    effective = _pipeline_at(overlay) if overlay.exists() else packaged
    return effective, packaged


def _discovered_pipelines() -> list[tuple[str, tuple[dict, dict]]]:
    seen: dict[str, tuple[dict, dict]] = {}
    for root in _agents_dirs():
        try:
            children = sorted(p for p in root.iterdir() if p.is_dir())
        except OSError:
            continue
        for agent_dir in children:
            if agent_dir.name.startswith((".", "_")):
                continue
            agent_id = _manifest_id(agent_dir)
            if not agent_id:
                continue
            effective, packaged = _legacy_pipeline(agent_dir)
            if effective:
                # A later root's agent replaces an earlier one, as discovery does.
                seen[agent_id] = (effective, packaged)
    return sorted(seen.items())


def _seed_step_configs(bind) -> int:
    """Seed every tenant's overrides from the legacy files. Returns the
    number of INSERTs attempted, so a caller can see that the scan found
    something; the conflict clause decides how many land.

    The connection is a parameter rather than ``op.get_bind()`` inside,
    so the seeding can be driven against a real database by a test
    without an Alembic context around it.
    """
    pipelines = _discovered_pipelines()
    if not pipelines:
        return 0
    tenants = [row[0] for row in bind.execute(sa.text("SELECT id FROM tenants")).all()]
    if not tenants:
        return 0
    insert = sa.text(
        "INSERT INTO agent_step_configs "
        "(tenant_id, agent_id, step_id, provider, model, temperature, "
        " max_tokens, timeout_seconds, updated_by) "
        "VALUES (:tenant_id, :agent_id, :step_id, :provider, :model, "
        " :temperature, :max_tokens, :timeout_seconds, NULL) "
        "ON CONFLICT (tenant_id, agent_id, step_id) DO NOTHING"
    )
    attempted = 0
    for agent_id, (pipeline, packaged) in pipelines:
        for step_id, step in pipeline.items():
            if not isinstance(step, dict):
                continue
            if step.get("provider") is None and step.get("model") is None:
                # A step that names neither is not an LLM step — the legacy
                # block also described stages that call no model, and those
                # never become ``llm.steps``, so an override row for one
                # would be inert noise.
                continue
            default = packaged.get(step_id)
            default = default if isinstance(default, dict) else {}
            # Only what this installation actually CHANGED. A value equal
            # to the packaged one is the agent's default, and storing it
            # would pin it (see ``_legacy_pipeline``).
            values = {
                field: (
                    step.get(field)
                    if step.get(field) is not None
                    and step.get(field) != default.get(field)
                    else None
                )
                for field in _STEP_FIELDS
            }
            if all(value is None for value in values.values()):
                continue
            for tenant_id in tenants:
                bind.execute(
                    insert,
                    {
                        "tenant_id": tenant_id,
                        "agent_id": agent_id,
                        "step_id": str(step_id)[:100],
                        **values,
                    },
                )
                attempted += 1
    return attempted
