-- ============================================================================
-- LibreRun: VITA — Database Migrations v1.0
-- PostgreSQL 15+ required (for JSONB, gen_random_uuid)
-- All tables are tenant-scoped via tenant_id column
-- Run in order: extensions → tables → indexes → seed data
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
        "credentials_enabled": false,
        "session_timeout_hours": 24
    }',
    data_retention_days INTEGER NOT NULL DEFAULT 365,
    next_case_number    INTEGER NOT NULL DEFAULT 1000,  -- Per-tenant counter; atomically increment via UPDATE ... RETURNING
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
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
-- CASES
-- ============================================================================
CREATE TABLE cases (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id             UUID NOT NULL REFERENCES users(id),
    case_number         VARCHAR(20) NOT NULL,  -- e.g., VITA-1005
    
    -- Vendor A fields
    vendor_a_name       VARCHAR(255) NOT NULL,
    vendor_a_product    VARCHAR(255),
    vendor_a_feature    VARCHAR(255),
    vendor_a_observation TEXT,
    
    -- Vendor B fields
    vendor_b_name       VARCHAR(255) NOT NULL,
    vendor_b_product    VARCHAR(255),
    vendor_b_feature    VARCHAR(255),
    vendor_b_observation TEXT,
    
    -- Customer inputs
    logs_a              TEXT,
    logs_b              TEXT,
    use_case            TEXT NOT NULL,
    problem_statement   TEXT NOT NULL,
    impact_statement    TEXT,
    severity            VARCHAR(20) CHECK (severity IN ('critical', 'high', 'medium', 'low')),
    
    -- Pipeline state
    -- 'submitted' is a brief transient state between DB insert and worker pickup.
    -- The API POST /cases returns 'refining' once the worker begins Phase 1.
    status              VARCHAR(30) NOT NULL DEFAULT 'submitted' 
                        CHECK (status IN ('submitted', 'refining', 'awaiting_approval', 
                                          'investigating', 'complete', 'error')),
    
    -- Soft delete (NULL = active; timestamp = deleted)
    deleted_at          TIMESTAMPTZ,
    
    -- Trace correlation
    trace_id            VARCHAR(64),
    
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    UNIQUE (tenant_id, case_number)
);

-- Per-tenant case number allocation (replaces global sequence)
-- Usage: SELECT allocate_case_number('tenant-uuid') → returns e.g. 'VITA-1005'
CREATE OR REPLACE FUNCTION allocate_case_number(p_tenant_id UUID)
RETURNS VARCHAR(20) AS $$
DECLARE
    v_num INTEGER;
BEGIN
    UPDATE tenants
       SET next_case_number = next_case_number + 1,
           updated_at = NOW()
     WHERE id = p_tenant_id
    RETURNING next_case_number - 1 INTO v_num;
    RETURN 'VITA-' || v_num;
END;
$$ LANGUAGE plpgsql;

-- ============================================================================
-- CASE FILES (logs + configs, always PII-redacted)
-- ============================================================================
CREATE TABLE case_files (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id         UUID NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
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
-- CASE SNAPSHOTS (investigation results — JSONB columns per section)
-- ============================================================================
CREATE TABLE case_snapshots (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id             UUID NOT NULL REFERENCES cases(id) ON DELETE CASCADE UNIQUE,
    tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    
    -- Phase 1 output
    refined_problem     JSONB,  -- { statement, key_signals, suspected_root_causes, research_focus_areas }
    
    -- Phase 2 outputs
    works_cited_a       JSONB,  -- [{ id, title, url, relevance_score, doc_type, summary }]
    works_cited_b       JSONB,  -- same schema
    skills_cited        JSONB,  -- [{ name, description, relevance_weight, source }]
    resolution_plan     JSONB,  -- { mitigation: { text, citations }, resolution: { text, citations }, avoidance: { text, citations } }
    followup_questions  JSONB,  -- [{ question, rationale, expected_impact }]
    
    -- Metadata
    classified_inputs   JSONB,  -- Output from Step 0
    pipeline_config     JSONB,  -- Snapshot of LLM config used for this investigation
    
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ============================================================================
-- CASE REPORTS (generated exports)
-- ============================================================================
CREATE TABLE case_reports (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id         UUID NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    format          VARCHAR(10) NOT NULL CHECK (format IN ('html', 'pdf')),
    storage_path    VARCHAR(500) NOT NULL,
    generated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ============================================================================
-- CASE FEEDBACK (per-section customer ratings — dedicated logging channel)
-- ============================================================================
CREATE TABLE case_feedback (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id         UUID NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    user_id         UUID NOT NULL REFERENCES users(id),
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    trace_id        VARCHAR(64),  -- OTEL trace id for the investigation (viewer deep links)
    
    section_type    VARCHAR(30) NOT NULL 
                    CHECK (section_type IN (
                        'refined_problem', 'works_cited_a', 'works_cited_b',
                        'skills_cited', 'mitigation', 'resolution', 'avoidance',
                        'followup_questions'
                    )),
    citation_id     INTEGER,  -- Nullable; for individual citation relevance feedback
    rating          VARCHAR(10) NOT NULL CHECK (rating IN ('positive', 'negative')),
    comment         TEXT,  -- Optional free-text explanation
    
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ============================================================================
-- ACTIVITY AUDIT LOG (all user actions — dedicated logging channel)
-- ============================================================================
CREATE TABLE activity_audit_log (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id         UUID REFERENCES users(id),  -- Nullable for system events
    user_email      VARCHAR(255),
    
    action_type     VARCHAR(30) NOT NULL 
                    CHECK (action_type IN (
                        'sign_in', 'sign_out', 
                        'case_create', 'case_update', 'case_delete',
                        'file_upload', 'file_validation_fail',
                        'config_change', 'blocked_request',
                        'report_generate', 'role_change', 'session_revoke',
                        'vendor_registry_edit', 'auth_config_change'
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
-- INDEXES
-- ============================================================================

-- Tenant scoping (every query filters by tenant_id; partial index excludes soft-deleted cases)
CREATE INDEX idx_users_tenant ON users(tenant_id);
CREATE INDEX idx_cases_tenant ON cases(tenant_id) WHERE deleted_at IS NULL;
CREATE INDEX idx_cases_tenant_user ON cases(tenant_id, user_id) WHERE deleted_at IS NULL;
CREATE INDEX idx_cases_tenant_status ON cases(tenant_id, status) WHERE deleted_at IS NULL;
CREATE INDEX idx_case_files_case ON case_files(case_id);
CREATE INDEX idx_case_snapshots_case ON case_snapshots(case_id);
CREATE INDEX idx_case_reports_case ON case_reports(case_id);

-- Feedback queries (admin dashboard)
CREATE INDEX idx_feedback_tenant ON case_feedback(tenant_id);
CREATE INDEX idx_feedback_case ON case_feedback(case_id);
CREATE INDEX idx_feedback_section ON case_feedback(tenant_id, section_type);
CREATE INDEX idx_feedback_rating ON case_feedback(tenant_id, rating);
CREATE INDEX idx_feedback_created ON case_feedback(tenant_id, created_at DESC);

-- Audit log queries
CREATE INDEX idx_audit_tenant ON activity_audit_log(tenant_id);
CREATE INDEX idx_audit_action ON activity_audit_log(tenant_id, action_type);
CREATE INDEX idx_audit_user ON activity_audit_log(tenant_id, user_id);
CREATE INDEX idx_audit_created ON activity_audit_log(tenant_id, created_at DESC);

-- Session lookups
CREATE INDEX idx_sessions_user ON sessions(user_id);
CREATE INDEX idx_sessions_expires ON sessions(expires_at) WHERE revoked_at IS NULL;

-- Case number lookups
CREATE INDEX idx_cases_number ON cases(case_number);

-- Vendor registry
CREATE INDEX idx_vendor_registry_tenant ON vendor_registry(tenant_id);

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
CREATE TRIGGER tr_cases_updated BEFORE UPDATE ON cases FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER tr_snapshots_updated BEFORE UPDATE ON case_snapshots FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER tr_vendor_registry_updated BEFORE UPDATE ON vendor_registry FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER tr_tasks_updated BEFORE UPDATE ON async_tasks FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

-- ============================================================================
-- SEED DATA: Default tenant for development
-- ============================================================================
-- No users are seeded. Admin/customer accounts are bootstrapped at backend
-- startup from INITIAL_ADMIN_* / INITIAL_USER_* environment variables
-- (backend/app/scripts/bootstrap_admin.py), which requires this tenant.
INSERT INTO tenants (id, name, slug) VALUES
    ('a0000000-0000-0000-0000-000000000001', 'Development', 'dev');
