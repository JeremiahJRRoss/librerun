-- ============================================================================
-- LibreRun — Database schema (head snapshot, alembic revision 0020)
-- PostgreSQL 15+ required (for JSONB, gen_random_uuid)
-- All tables are tenant-scoped via tenant_id column
-- Run in order: extensions → tables → indexes → triggers → seed data
--
-- This file is what a fresh install loads (compose mounts it into the
-- database container's initdb directory as backend/db/schema.sql). It is a snapshot of the schema
-- at alembic head: `alembic upgrade head` against a database created
-- from it must be a no-op, and every migration is written to be one. The
-- database-parity workflow proves that on every change by diffing this
-- file's result against a pre-S1 database upgraded through the chain.
-- ============================================================================

-- Extensions
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- ============================================================================
-- TENANTS
-- ============================================================================
CREATE TABLE tenants (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            VARCHAR(255) NOT NULL,
    slug            VARCHAR(100) NOT NULL UNIQUE,
    auth_config     JSONB NOT NULL DEFAULT '{
        "google_enabled": true,
        "google_allowed_domains": [],
        "google_allowed_emails": [],
        "microsoft_enabled": true,
        "microsoft_allowed_tenants": [],
        "microsoft_allowed_emails": [],
        "credentials_enabled": false
    }',
    data_retention_days INTEGER NOT NULL DEFAULT 365,
    next_run_number INTEGER NOT NULL DEFAULT 1000,  -- Per-tenant counter; atomically increment via UPDATE ... RETURNING
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- Label prefix for this tenant's run numbers (blueprint L19): a fresh
    -- tenant gets RUN-1000, RUN-1001, ...; tenants that predate migration
    -- 012 were backfilled 'VITA' so their existing labels keep counting.
    run_prefix      VARCHAR(12) NOT NULL DEFAULT 'RUN' CHECK (run_prefix ~ '^[A-Z][A-Z0-9]{0,11}$')
);

-- ============================================================================
-- USERS
-- ============================================================================
CREATE TABLE users (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    email           VARCHAR(255) NOT NULL,
    password_hash   VARCHAR(255),  -- NULL for SSO-only users
    role            VARCHAR(20) NOT NULL DEFAULT 'customer' CHECK (role IN ('admin', 'customer')),
    auth_provider   VARCHAR(20) NOT NULL CHECK (auth_provider IN ('google', 'microsoft', 'credentials')),
    display_name    VARCHAR(255),
    is_active       BOOLEAN NOT NULL DEFAULT true,
    last_sign_in    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, email)
);

-- ============================================================================
-- SESSIONS
-- ============================================================================
CREATE TABLE sessions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    token_hash      VARCHAR(255) NOT NULL UNIQUE,
    ip_address      INET,
    user_agent      TEXT,
    expires_at      TIMESTAMPTZ NOT NULL,
    revoked_at      TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ============================================================================
-- APP SETTINGS (runtime-tunable overrides of .env defaults; migration 002)
-- ============================================================================
CREATE TABLE app_settings (
    key             VARCHAR(100) PRIMARY KEY,
    value           JSONB NOT NULL,
    updated_by      UUID REFERENCES users(id),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ============================================================================
-- RUNS — one submission of one agent (blueprint L18: the platform noun)
-- ============================================================================
-- Column order is the upgrade order: the baseline columns first, then the
-- columns migrations 005-009 added, so a database upgraded through the
-- chain and one created from this file dump identically.
CREATE TABLE runs (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id             UUID NOT NULL REFERENCES users(id),
    run_number          VARCHAR(20) NOT NULL,  -- e.g., RUN-1005 (tenants.run_prefix + counter)

    -- Legacy VITA-shaped columns (nullable since migration 010): populated
    -- only when the agent's payload carries the well-known keys; the full
    -- payload always lives in user_inputs. Removal is S2's business.
    vendor_a_name       VARCHAR(255),
    vendor_a_product    VARCHAR(255),
    vendor_a_feature    VARCHAR(255),
    vendor_a_observation TEXT,
    vendor_b_name       VARCHAR(255),
    vendor_b_product    VARCHAR(255),
    vendor_b_feature    VARCHAR(255),
    vendor_b_observation TEXT,
    logs_a              TEXT,
    logs_b              TEXT,
    use_case            TEXT,
    problem_statement   TEXT,
    impact_statement    TEXT,
    severity            VARCHAR(20) CHECK (severity IN ('critical', 'high', 'medium', 'low')),

    -- Pipeline state
    -- 'submitted' is a brief transient state between DB insert and worker pickup.
    -- POST /runs returns 'refining' once the worker begins the first phase.
    status              VARCHAR(30) NOT NULL DEFAULT 'submitted'
                        CHECK (status IN ('submitted', 'refining', 'awaiting_approval',
                                          'investigating', 'complete', 'error')),

    -- Soft delete (NULL = active; timestamp = deleted)
    deleted_at          TIMESTAMPTZ,

    -- Trace correlation: the run's trace id, the root span's, set at
    -- submission and never changed by a phase (migration 004 indexes it)
    trace_id            VARCHAR(64),

    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Root span of the final phase (migration 005) — feedback annotations target it
    phase2_span_id      VARCHAR(16),
    -- Generic agent columns (migration 006). The chassis sets agent_id
    -- explicitly at create time; there is no column default (dropped in 012).
    agent_id            VARCHAR(50),
    user_inputs         JSONB,
    -- The root span's W3C context (migration 013, blueprint S4): traceparent
    -- and tracestate, restored as every phase span's remote parent — one
    -- trace per run, across the approval gate. In place of the retired
    -- phase1_trace_id / phase1_span_id pointer pair (migration 007).
    root_traceparent    VARCHAR(55),
    root_tracestate     VARCHAR(512),
    -- Manifest phase that most recently ran (migration 009); NULL = start of the phase list
    current_phase       VARCHAR(64),
    -- Why the run ended 'error' (migration 017, blueprint S7): a code from
    -- the chassis's closed vocabulary (app/services/run_errors.py) that the
    -- customer page maps to a sentence, and the operator-facing detail —
    -- the agent's own failure text, the exception, the restart's phase —
    -- redacted before it is stored and served on the admin run view only.
    error_code          VARCHAR(40),
    error_detail        TEXT,

    UNIQUE (tenant_id, run_number)
);

-- Per-tenant run number allocation (replaces global sequence)
-- Usage: SELECT allocate_run_number('tenant-uuid') → returns e.g. 'RUN-1005'
CREATE OR REPLACE FUNCTION allocate_run_number(p_tenant_id UUID)
RETURNS VARCHAR(20) AS $$
DECLARE
    v_num INTEGER;
    v_prefix VARCHAR(12);
BEGIN
    UPDATE tenants
       SET next_run_number = next_run_number + 1,
           updated_at = NOW()
     WHERE id = p_tenant_id
    RETURNING next_run_number - 1, run_prefix INTO v_num, v_prefix;
    RETURN v_prefix || '-' || v_num;
END;
$$ LANGUAGE plpgsql;

-- ============================================================================
-- RUN FILES (logs + configs, always PII-redacted)
-- ============================================================================
CREATE TABLE run_files (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id          UUID NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    vendor_side     VARCHAR(1) NOT NULL CHECK (vendor_side IN ('a', 'b')),
    file_type       VARCHAR(20) NOT NULL CHECK (file_type IN ('log', 'config')),
    original_name   VARCHAR(255) NOT NULL,
    storage_path    VARCHAR(500) NOT NULL,  -- Path to PII-redacted version
    file_size_bytes INTEGER NOT NULL,
    mime_type       VARCHAR(100),
    pii_redaction_applied BOOLEAN NOT NULL DEFAULT true,
    uploaded_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ============================================================================
-- RUN SNAPSHOTS (the agent's results — generic JSONB, one row per run)
-- ============================================================================
CREATE TABLE run_snapshots (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id              UUID NOT NULL REFERENCES runs(id) ON DELETE CASCADE UNIQUE,
    tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,

    -- Legacy VITA-shaped mirror columns. Unmapped by the chassis since
    -- blueprint B9 (nothing reads or writes them); kept so an upgraded
    -- database and a fresh one match. Dropping them is S2's business.
    refined_problem     JSONB,
    works_cited_a       JSONB,
    works_cited_b       JSONB,
    skills_cited        JSONB,
    resolution_plan     JSONB,
    followup_questions  JSONB,
    classified_inputs   JSONB,
    pipeline_config     JSONB,  -- Snapshot of the LLM config used for this run

    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Generic agent outputs (migration 006): the first phase's display
    -- payload, the final structured result, and the pre-rendered report.
    analysis            JSONB,
    structured_data     JSONB,
    report_html         TEXT
);

-- ============================================================================
-- RUN REPORTS (generated exports)
-- ============================================================================
CREATE TABLE run_reports (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id          UUID NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    format          VARCHAR(10) NOT NULL CHECK (format IN ('html', 'pdf')),
    storage_path    VARCHAR(500) NOT NULL,
    generated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ============================================================================
-- RUN FEEDBACK (per-section ratings — dedicated logging channel)
-- ============================================================================
-- section_type is validated by the API against the run's agent manifest
-- (migration 011 dropped the VITA-shaped CHECK).
CREATE TABLE run_feedback (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id          UUID NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    user_id         UUID NOT NULL REFERENCES users(id),
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    trace_id        VARCHAR(64),  -- OTEL trace id for the run (viewer deep links)

    section_type    VARCHAR(30) NOT NULL,
    citation_id     INTEGER,  -- Nullable; for individual citation relevance feedback
    rating          VARCHAR(10) NOT NULL CHECK (rating IN ('positive', 'negative')),
    comment         TEXT,  -- Optional free-text explanation

    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ============================================================================
-- ACTIVITY AUDIT LOG (all user actions — dedicated logging channel)
-- ============================================================================
-- The action_type list is the one migrations 003, 012 and 016 install:
-- the pre-S1 values stay legal (audit rows are history and are never
-- rewritten); new rows use the run_* spellings. 016 adds
-- 'pii_detector_degraded' (blueprint S4c) — the row the chassis writes
-- for a tenant it served with the PII detector degraded.
CREATE TABLE activity_audit_log (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id         UUID REFERENCES users(id),  -- Nullable for system events
    user_email      VARCHAR(255),

    action_type     VARCHAR(30) NOT NULL
                    CHECK (action_type IN (
                        'sign_in', 'sign_out',
                        'case_create', 'case_update', 'case_delete',
                        'blocked_request', 'config_change',
                        'vendor_registry_edit', 'role_change', 'session_revoke',
                        'auth_config_change', 'llm_schema_drift',
                        'run_create', 'run_update', 'run_delete',
                        'pii_detector_degraded'
                    )),
    detail          JSONB,  -- Structured detail about the action
    ip_address      INET,

    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ============================================================================
-- VENDOR REGISTRY (optional enrichment)
-- ============================================================================
CREATE TABLE vendor_registry (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    name            VARCHAR(255) NOT NULL,
    doc_base_url    VARCHAR(500),
    pinecone_namespace VARCHAR(255),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, name)
);

-- ============================================================================
-- THE GATEWAY'S THREE TABLES (migration 014)
-- ============================================================================
-- The LLM gateway is a separate service, so what it needs to authorize and
-- route a call has to be readable from the database: the in-process agent
-- registry is not. All three are platform-scoped except the step overrides,
-- which are keyed by tenant because a model choice is a tenant's.

-- One snapshot per agent id, upserted by the backend at discovery
-- (directory, entry point and container alike). Rows are never deleted: an
-- agent that disappears is stamped absent_at and the stamp is cleared when
-- it comes back, so an uninstall does not take that agent's keys and step
-- overrides with it.
CREATE TABLE agent_manifests (
    agent_id        VARCHAR(100) PRIMARY KEY,
    manifest        JSONB NOT NULL,
    sha256          CHAR(64) NOT NULL,
    source          VARCHAR(32) NOT NULL,
    discovered_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    absent_at       TIMESTAMPTZ
);

-- The admin's per-step model choices. The manifest's llm.steps defaults are
-- the fallback when no row exists; a row never declares a step, it only
-- replaces values for one the manifest still declares.
CREATE TABLE agent_step_configs (
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
);

-- A tenant's values for the settings an agent's manifest declares
-- (settings[], migration 018, K5a). The manifest's default is the fallback
-- when no row exists, and a row records a divergence from it only (D17): a
-- value equal to the default is not stored. Only the backend reads it.
CREATE TABLE agent_settings (
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    agent_id        VARCHAR(100) NOT NULL,
    key             VARCHAR(64) NOT NULL,
    value           JSONB NOT NULL,
    updated_by      UUID REFERENCES users(id),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tenant_id, agent_id, key)
);

-- The per-agent credential (D10). The value is never stored: only its
-- sha256 and the eight characters after lr_agent_, so the admin page can
-- name the key an operator is holding. key_hash is unique across the whole
-- table, so a presented key maps to exactly one agent.
CREATE TABLE agent_keys (
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
);

-- ============================================================================
-- ASYNC TASKS (report generation, etc.)
-- ============================================================================
CREATE TABLE async_tasks (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id         UUID NOT NULL REFERENCES users(id),
    task_type       VARCHAR(30) NOT NULL,
    status          VARCHAR(20) NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'running', 'complete', 'error')),
    result_url      VARCHAR(500),
    error_message   TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ============================================================================
-- SECRETS (the encrypted secrets store; migration 019, K6)
-- ============================================================================
-- A secret set in the admin UI: MultiFernet ciphertext under the owning
-- process's store key, never plaintext. key_id is a keyed digest of that key
-- (a row no configured key opens is reported without decrypting anything)
-- and fingerprint a keyed digest of the value, all the API ever shows of it.
-- Keyed by its owners (D32): platform and gateway rows name neither tenant
-- nor agent, agent rows the agent, tenant rows both; one name per owner, NULLs
-- equal. updated_at has no trigger: the service's upsert sets it.
CREATE TABLE secrets (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scope           VARCHAR(16) NOT NULL,
    tenant_id       UUID REFERENCES tenants(id) ON DELETE CASCADE,
    agent_id        VARCHAR(100),
    name            VARCHAR(100) NOT NULL,
    ciphertext      BYTEA NOT NULL,
    key_id          VARCHAR(16) NOT NULL,
    fingerprint     CHAR(12) NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by      UUID REFERENCES users(id) ON DELETE SET NULL,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_by      UUID REFERENCES users(id) ON DELETE SET NULL,
    last_used_at    TIMESTAMPTZ,
    CONSTRAINT ck_secrets_scope_owner CHECK (
           (scope IN ('platform', 'gateway') AND tenant_id IS NULL AND agent_id IS NULL)
        OR (scope = 'agent'  AND tenant_id IS NULL     AND agent_id IS NOT NULL)
        OR (scope = 'tenant' AND tenant_id IS NOT NULL AND agent_id IS NOT NULL)),
    CONSTRAINT uq_secrets_owner_name
        UNIQUE NULLS NOT DISTINCT (scope, tenant_id, agent_id, name)
);

-- ============================================================================
-- GATEWAY STATUS (what the gateway holds; migration 020, K7)
-- ============================================================================
-- One row, written by the gateway at boot and on every change to its own
-- gateway-scope secrets, read by the platform-admin endpoints (D16): the
-- provider names with source and fingerprint, never a value; the public key
-- the browser seals a provider key to, NULL while the gateway's store key is
-- blank. The sealing key's fingerprint has no column: each reader computes it
-- from the PEM (D34).
CREATE TABLE gateway_status (
    id              INTEGER PRIMARY KEY
                    CONSTRAINT ck_gateway_status_singleton CHECK (id = 1),
    version         VARCHAR(32) NOT NULL,
    stub            BOOLEAN NOT NULL,
    providers       JSONB NOT NULL DEFAULT '[]',
    public_key_pem  TEXT,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ============================================================================
-- INDEXES
-- ============================================================================

-- Tenant scoping (every query filters by tenant_id; partial index excludes soft-deleted runs)
CREATE INDEX idx_users_tenant ON users(tenant_id);
CREATE INDEX idx_runs_tenant ON runs(tenant_id) WHERE deleted_at IS NULL;
CREATE INDEX idx_runs_tenant_user ON runs(tenant_id, user_id) WHERE deleted_at IS NULL;
CREATE INDEX idx_runs_tenant_status ON runs(tenant_id, status) WHERE deleted_at IS NULL;
CREATE INDEX idx_run_files_run ON run_files(run_id);
CREATE INDEX idx_run_snapshots_run ON run_snapshots(run_id);
CREATE INDEX idx_run_reports_run ON run_reports(run_id);

-- Feedback queries (admin dashboard)
CREATE INDEX idx_feedback_tenant ON run_feedback(tenant_id);
CREATE INDEX idx_feedback_run ON run_feedback(run_id);
CREATE INDEX idx_feedback_section ON run_feedback(tenant_id, section_type);
CREATE INDEX idx_feedback_rating ON run_feedback(tenant_id, rating);
CREATE INDEX idx_feedback_created ON run_feedback(tenant_id, created_at DESC);

-- Audit log queries
CREATE INDEX idx_audit_tenant ON activity_audit_log(tenant_id);
CREATE INDEX idx_audit_action ON activity_audit_log(tenant_id, action_type);
CREATE INDEX idx_audit_user ON activity_audit_log(tenant_id, user_id);
CREATE INDEX idx_audit_created ON activity_audit_log(tenant_id, created_at DESC);

-- Session lookups
CREATE INDEX idx_sessions_user ON sessions(user_id);
CREATE INDEX idx_sessions_expires ON sessions(expires_at) WHERE revoked_at IS NULL;

-- Run number lookups
CREATE INDEX idx_runs_number ON runs(run_number);

-- Trace and phase correlation (migrations 004-006; 013 dropped the
-- phase1_span_id index with the column)
CREATE INDEX ix_runs_trace_id ON runs(trace_id);
CREATE INDEX ix_runs_phase2_span_id ON runs(phase2_span_id);
CREATE INDEX ix_runs_agent_id ON runs(agent_id);

-- Vendor registry
CREATE INDEX idx_vendor_registry_tenant ON vendor_registry(tenant_id);

-- The gateway's lookups (migration 014). (agent_id, key_hash) is the pair
-- env reconciliation upserts and deletes by; the two partial uniques give
-- an agent at most one current and one previous key, the cardinality
-- rotation needs and revocation-by-replacement assumes.
CREATE UNIQUE INDEX uq_agent_keys_agent_hash ON agent_keys (agent_id, key_hash);
CREATE UNIQUE INDEX uq_agent_keys_current ON agent_keys (agent_id) WHERE role = 'current';
CREATE UNIQUE INDEX uq_agent_keys_previous ON agent_keys (agent_id) WHERE role = 'previous';
CREATE INDEX idx_agent_manifests_present ON agent_manifests (agent_id) WHERE absent_at IS NULL;

-- ============================================================================
-- UPDATED_AT TRIGGER (auto-update on row modification)
-- ============================================================================
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER tr_tenants_updated BEFORE UPDATE ON tenants FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER tr_users_updated BEFORE UPDATE ON users FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER tr_runs_updated BEFORE UPDATE ON runs FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER tr_snapshots_updated BEFORE UPDATE ON run_snapshots FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER tr_vendor_registry_updated BEFORE UPDATE ON vendor_registry FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER tr_tasks_updated BEFORE UPDATE ON async_tasks FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

-- ============================================================================
-- SEED DATA: Default tenant for development
-- ============================================================================
-- No users are seeded. Admin/customer accounts are bootstrapped at backend
-- startup from INITIAL_ADMIN_* / INITIAL_USER_* environment variables
-- (backend/app/scripts/bootstrap_admin.py), which requires this tenant.
-- A fresh tenant numbers its runs RUN-1000, RUN-1001, ... (run_prefix default).
INSERT INTO tenants (id, name, slug) VALUES
    ('a0000000-0000-0000-0000-000000000001', 'Development', 'dev');
