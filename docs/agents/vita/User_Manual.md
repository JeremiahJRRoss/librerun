# VITA — User Manual

> **Vocabulary note:** the platform calls each investigation a **run** (blueprint S1, decision L18): the API serves `/api/v1/runs`, the UI says *Run*, and run labels are `RUN-NNNN` on new tenants or `VITA-NNNN` on tenants created before the rename. What VITA investigates *inside* a run is still a vendor interoperability case — that word survives only there.

**Vendor Interoperability Troubleshooting Agent**

Version 1.1 · April 2026

| | |
|---|---|
| Audience | Customers and platform administrators |
| Application version | VITA 1.0 |
| Companion documents | `docs/platform/Install.md`; the agent's manifest, `backend/agents/vita_v1/agent.yaml` |

---

## Contents

**Part I — Customer Guide**

1. Introduction
2. Getting started
3. Creating a run
4. Investigation lifecycle
5. Understanding results
6. Managing runs
7. Exporting reports
8. PII redaction
9. Providing feedback

**Part II — Administrator Guide**

10. Administrator overview
11. LLM pipeline configuration
12. Administration panels
13. Application settings

**Part III — Reference**

14. Troubleshooting
15. Frequently asked questions
16. Glossary

---

# Part I — Customer Guide

## 1. Introduction

VITA (Vendor Interoperability Troubleshooting Agent) is an AI-powered self-service portal that investigates technical integration issues between two vendor systems. Instead of filing support tickets and waiting for responses, VITA investigates interoperability problems immediately by combining your knowledge of the issue with automated research across both vendors' documentation.

### What VITA does

- Validates inputs and classifies the integration type, error signals, and input quality.
- Refines your description into a formal problem statement for review before research begins.
- Constructs targeted search queries and searches public documentation from both vendors simultaneously.
- Searches your organization's internal knowledge base (if configured) for past resolutions.
- Identifies technical skills needed to resolve the issue with weighted relevance scores.
- Generates a three-part resolution plan: immediate mitigation, root cause resolution, and future avoidance.
- Cites every recommendation to a specific documentation source.
- Produces three targeted follow-up questions to surface information gaps.

### What VITA does not do

- VITA does not make changes to your systems. It provides recommendations for you to implement.
- VITA does not contact vendors on your behalf. It researches their public documentation.
- VITA does not store your original unredacted files. Only PII-redacted versions are persisted after your preview and confirmation.

---

## 2. Getting started

### 2.1 Accessing VITA

Open your browser and navigate to the URL provided by your administrator (default: `http://localhost:3000`). VITA works in Chrome, Firefox, Safari, and Edge. The desktop experience is recommended.

### 2.2 Signing in

VITA supports three sign-in methods. Your administrator determines which are available.

| Method | How to use | When available |
|--------|-----------|----------------|
| Email/password | Enter your email and password, then click **Sign in**. | Always available when `CREDENTIALS_ENABLED=true`. |
| Google SSO | Click **Continue with Google** and complete the OAuth flow. | Button is enabled only when `GOOGLE_CLIENT_ID` is configured. |
| Microsoft SSO | Click **Continue with Microsoft** and complete the OAuth flow. | Button is enabled only when `AZURE_CLIENT_ID` is configured. |

If SSO buttons appear grayed out, the corresponding OAuth credentials have not been configured. Contact your administrator.

If you receive an "Access denied" or "Email not allowed" message after SSO sign-in, your email domain or address has not been approved in the tenant's auth configuration.

> **Note:** JWT tokens are stored in browser memory only. Refreshing the page requires re-authentication. This is a deliberate security measure for applications handling sensitive infrastructure data.

### 2.3 The dashboard

After signing in, you land on the Dashboard at `/dashboard`.

| Element | Description |
|---------|-------------|
| **+ New Run** button | Opens the 7-step run wizard. Located in the top-right corner. |
| Run table | Lists your runs with columns: Run #, Vendor A, Vendor B, Severity, Status, Created. Click any row to open the run. |
| Status filter tabs | Filter by: all, refining, awaiting_approval, investigating, complete, error. |
| Search bar | Searches across vendor names and problem statement text (case-insensitive substring match). |
| Pagination | Page controls at the bottom (20 runs per page). |

Admins see all runs across the tenant. Customers see only their own runs.

---

## 3. Creating a run

The run wizard at `/runs/new` guides you through seven steps. A progress bar at the top shows your current position. Use **Back** and **Next** to navigate between steps.

> **Important:** The quality of VITA's investigation depends directly on the quality of information you provide.

### Step 1 — Vendor details

Identify the two vendor systems involved. Each vendor has four fields:

| Field | Required | Description |
|-------|----------|-------------|
| Name | Yes | The vendor's name (e.g., Cribl, Splunk, Salesforce). |
| Product | No | The specific product (e.g., Stream, Enterprise, Sales Cloud). |
| Feature | No | The integration feature (e.g., Splunk HEC Destination, REST API). |
| Observation | No | What you observe on this vendor's side (error messages, symptoms). |

> **Tip:** Be specific. "Cribl Stream Splunk HEC Destination" produces better results than "Cribl."

### Step 2 — Logs

Upload or paste log output from both vendors. A PII redaction banner reminds you that sensitive data will be automatically masked.

For each vendor, you can:

- **Upload a log file** — Click the file input. Supported formats: `.log`, `.txt`, `.jsonl`, `.out`, `.err`. Maximum 10 MB per vendor. The PII redaction preview runs on the upload and places the redacted content into the textarea for your review; a toast notification reports how many PII items were redacted.
- **Paste log text** — Paste directly into the textarea. Pasted content is redacted server-side when you submit, so sensitive values never reach the database either way.

Logs are optional. You can proceed without them, but results will be less specific.

### Step 3 — Use case

Describe what you are trying to accomplish with this integration and what a successful outcome looks like. Minimum 50 characters, maximum 5,000. A live character counter is displayed below the field.

| Good example | Poor example |
|-------------|-------------|
| We are forwarding observability data from Cribl Stream to Splunk via HEC for centralized log analysis. Events should arrive within 5 seconds with all fields preserved. | We're trying to connect two systems. |

### Step 4 — Problem statement

Describe the specific issue. Focus on what is happening versus what should be happening. Minimum 20 characters, maximum 5,000.

| Good example | Poor example |
|-------------|-------------|
| Backpressure errors on the Cribl Stream Splunk destination, events dropped intermittently during peak hours. Started after upgrading Splunk to 9.2. | It doesn't work. |

> **Tip:** Include when the issue started, any recent changes, specific error messages or codes, and whether the integration ever worked correctly.

### Step 5 — Impact and severity

Describe the business impact (free text) and select an optional severity level:

| Severity | Description |
|----------|-------------|
| Critical | Complete integration failure; production systems affected. |
| High | Partial failure; significant business impact. |
| Medium | Degraded functionality; workaround exists. |
| Low | Cosmetic or minor issue. |

### Step 6 — Configuration files

Optional. Upload configuration files for PII redaction preview. Supported formats: `.yaml`, `.yml`, `.json`, `.xml`, `.toml`, `.ini`, `.conf`, `.env`, `.properties`. Maximum 5 MB per file.

> **Note:** This step is a preview placeholder in the current release. Configuration files can be uploaded after run creation via the file upload endpoint.

### Step 7 — Review and submit

A read-only summary of all inputs. Review each section. Click **Submit Run** to create the run. VITA immediately begins Phase 1 processing as a background task and redirects you to the run detail page.

---

## 4. Investigation lifecycle

After submission, a run moves through a defined lifecycle. The run detail page at `/runs/{id}` polls every 3 seconds and adapts its display based on the current status.

| Status | What is happening | What you see |
|--------|------------------|--------------|
| `submitted` | Run created, queued for processing. | "Queued for processing…" message. |
| `refining` | Phase 1 running: validate inputs (Step 0) and refine problem statement (Step 2). | Pulsing animation: "Analyzing your inputs…" |
| `awaiting_approval` | Refinement complete. Waiting for your review. | Refined statement review panel (see §4.1). |
| `investigating` | Phase 2 running: Steps 3–9 (search, analyze, generate plan). | Real-time progress tracker (see §4.2). |
| `complete` | Investigation finished. | Full results view (see §5). |
| `error` | A pipeline step failed after retries. | Error message with red border. |

### 4.1 The approval gate

When the run reaches `awaiting_approval`, you see the refined statement review panel. This is the quality gate — all Phase 2 research is based on this statement.

The panel displays:

- The refined problem statement text.
- **Key signals:** Specific error codes, log patterns, or metrics, shown as colored tag pills.
- **Suspected root causes:** 2–4 hypotheses ranked by likelihood.
- **Research focus areas:** Specific documentation topics VITA will search per vendor.

You have two options:

| Action | What happens |
|--------|-------------|
| **Approve & Begin Research** | Status changes to `investigating`. Phase 2 starts as a background task. |
| **Edit Statement** | The statement becomes an editable textarea. Click **Save & Re-refine** to send your edit. Only Step 2 re-runs — not the full pipeline. A new refined statement is generated incorporating your changes. Status returns to `awaiting_approval`. |

> **Important:** Review the refined statement carefully. If it mischaracterizes the integration or the failure mode, the documentation search and resolution plan will be off-target.

### 4.2 Real-time progress tracker

During the `investigating` status, the progress tracker polls every 2 seconds and displays a vertical step list:

| Indicator | Meaning |
|-----------|---------|
| ○ | Pending — not yet started. |
| ◐ (blue) | Running — currently executing. |
| ● (green) | Complete — finished, with duration in milliseconds. |
| ⊘ | Skipped — step was not needed (e.g., no internal KB configured). |
| ✗ (red) | Error — step failed, with error detail shown. |

Steps displayed: Query construction (Vendor A), Query construction (Vendor B), Search internal KB, Search public resources, Assess skills, Generate resolution plan, Generate follow-up questions.

The tracker stops polling when the run reaches `complete` or `error`.

---

## 5. Understanding results

When the investigation completes, the run detail page displays five sections. Each section includes per-section feedback controls (thumbs up / thumbs down).

### 5.1 Refined problem statement

Displayed at the top in a card with feedback controls. This is the statement that guided all research.

### 5.2 Works cited

Separate tables for Vendor A and Vendor B. Each entry contains:

| Column | Description |
|--------|-------------|
| # | Sequential citation ID. Used for inline references in the resolution plan. |
| Title | Linked to the original URL (opens in a new tab). |
| Type | Badge: `troubleshooting_guide`, `api_reference`, `kb_article`, `community_post`, or `release_notes`. |
| Relevance | Visual progress bar (0.0 to 1.0). |
| Summary | 2–3 sentence explanation of why this document is relevant. |

> **Tip:** Include the works-cited list when filing vendor support tickets. It demonstrates thorough preparation and helps support engineers skip first-tier triage.

### 5.3 Skills cited

A grid of skill cards, each showing: skill name, description, source badge (`vendor_docs`, `internal_kb`, `public_web`, or `llm_knowledge`), and a relevance weight progress bar (0.0 to 1.0). Skills that bridge both vendors are weighted higher.

### 5.4 Resolution plan

Three collapsible sections (all open by default), each with inline citation superscripts:

| Section | Purpose | Typical contents |
|---------|---------|-----------------|
| Immediate mitigation | Reduce impact right now. | Workarounds, temporary configurations, monitoring adjustments. |
| Root cause resolution | Fix the underlying issue. | Configuration changes with specific values, upgrade instructions, CLI commands. |
| Future avoidance | Prevent recurrence. | Alerting thresholds, health check configurations, capacity planning. |

Citation references appear as clickable `[N]` superscripts. Clicking scrolls to the corresponding entry in the works-cited table.

> **Important:** The resolution plan is a recommendation, not a guarantee. Test in a non-production environment first and follow your organization's change management process.

### 5.5 Follow-up questions

Three cards, each showing the question (bold), rationale (why it matters), and expected impact (how the answer would change the recommendation). These identify specific gaps in the evidence.

---

## 6. Managing runs

### Soft delete

Runs can be deleted from the run detail page. This is a soft delete — the run's `deleted_at` timestamp is set, hiding it from the dashboard. Only the run owner or an admin can delete a run. A `run_delete` event is logged to the audit trail.

### Searching and filtering

The dashboard provides status tabs, keyword search (case-insensitive substring match across vendor names and problem statement), and pagination. Admin users see all tenant runs; customer users see only their own.

---

## 7. Exporting reports

On the results page, an **Export** dropdown offers HTML and PDF options.

### How export works

1. Click **HTML** or **PDF**. VITA creates an asynchronous report generation task and returns a task ID.
2. The frontend polls the task status every 2 seconds.
3. When the task completes, the report opens in a new browser tab. The download is authenticated via the bearer token and transmitted as a blob.
4. If the task fails, an error message is displayed via toast notification.

Reports contain: run metadata, refined problem statement, works cited for both vendors, skills cited, all three resolution plan sections with clickable citation links, and follow-up questions. Reports are rendered from a Jinja2 HTML template with inline CSS. PDF generation uses WeasyPrint; if it fails, the system falls back to HTML format.

---

## 8. PII redaction

VITA runs a five-stage PII redaction pipeline on all uploaded content before anything reaches the database.

### The five stages

| Stage | What it catches | Examples |
|-------|----------------|---------|
| 1. Regex patterns | High-confidence structured PII. | SSNs, credit cards, phone numbers, API keys (`sk-*`, `AKIA*`, `ghp_*`, `xox*`). |
| 2. IP/URL masking | Network identifiers. | IPv4, IPv6, URLs with query parameters. |
| 3. Presidio NER | Named entities via NLP model. | Email addresses, person names, locations, organizations. |
| 4. Second-pass regex | Broader patterns missed by Stage 1. | Base64 blobs (40+ chars), JDBC/database connection strings. |
| 5. Confidence scoring | Threshold gate (default 0.7). | Only detections at or above the threshold are applied. |

Mask format: `[REDACTED_{TYPE}_{N}]` where `TYPE` is the PII category and `N` is a sequential counter. Example: `[REDACTED_EMAIL_1]`, `[REDACTED_IP_2]`.

The redaction preserves document structure (indentation, key-value pairs) so VITA can still analyze configuration settings and log patterns. Your original unredacted files exist only in your browser during the preview. Nothing is stored until you confirm submission.

---

## 9. Providing feedback

Every section of the results page has thumbs-up (👍) and thumbs-down (👎) buttons. Clicking either immediately saves feedback via a background API call.

When you click thumbs-down, an optional comment textarea appears below the buttons. Your comment is saved on blur or when you submit the negative rating again.

Feedback metadata includes: run ID, section type, rating (positive/negative), and optional comment. Administrators can view aggregate feedback metrics on the Feedback Dashboard (see §12.3).

---

# Part II — Administrator Guide

## 10. Administrator overview

Admin functions are accessible from the **Admin** link in the navigation bar (visible only to admin-role users). The admin home page at `/admin` has two sections.

**Agents** is built from the agents actually installed, so it grows and shrinks with the tree rather than naming any one agent. Each installed agent that ships a config gets a card linking to its own editor:

| Page | Route | Description |
|------|-------|-------------|
| Agent Config | `/admin/agents/{agent_id}/config` | That agent's page, for this tenant: its **Steps** tab sets the provider, model, temperature, max tokens, and timeout per pipeline step, and its **Settings** tab the agent's own settings (§11). For VITA: Admin → Agents → VITA. |

**System** is a fixed set of five cards:

| Page | Route | Description |
|------|-------|-------------|
| Users & Access | `/admin/users` | User list with role editing, session revocation, and invitations. |
| Auth Configuration | `/admin/auth-config` | Toggle SSO providers; manage domain and email allowlists. |
| Feedback Dashboard | `/admin/feedback` | Aggregate positive/negative rates per section with recent negatives. |
| Activity Audit Log | `/admin/audit-log` | Paginated, filterable log of all user and admin actions. |
| Application Settings | `/admin/settings` | Runtime-tunable settings: CORS origins, upload limits, pipeline flags, and the session timeout. |

There is no Vendor Registry page. `/admin/vendors` and `/admin/llm-config` were both removed in Phase 4, superseded by the generic `/agents` router; `backend/tests/test_admin_removals.py` keeps them gone.

Session lifetime is `session_timeout_minutes` under Application Settings. It defaults to the `JWT_EXPIRY_HOURS` environment value and overrides it once set.

Every administrative action is logged to the `activity_audit_log` table with the admin's user ID, email, IP address, action type, and structured detail.

---

## 11. LLM pipeline configuration

The agent page at `/admin/agents/vita-v1/config` (Admin → Agents → VITA) has three tabs. The **Steps** tab is a table of the pipeline's model steps, each row editable, for this tenant alone; the two search steps (5 and 6) call no model and are not listed. The **Settings** tab is below, under "Agent settings", and the **Secrets** tab under "The Secrets tab".

| Column | What it controls |
|--------|-----------------|
| Step (description) | Human-readable name (read-only). |
| Provider | Dropdown: `openai`, `anthropic`, or `google`. |
| Model | Text input for the model string (e.g., `gpt-4o`, `claude-sonnet-4-6`). |
| Temperature | Number input (0.0–1.0). 0.0 is recommended for all steps. |
| Max tokens | Maximum output length. Increase if responses are truncated. |
| Timeout | Per-step timeout in seconds. The retry wrapper uses exponential backoff, up to `max_retries_per_step` retries (2 by default; see "Agent settings" below). |

### Default model assignments

| Step | Default provider | Default model | Tier |
|------|-----------------|---------------|------|
| 0: Validate & classify | openai | gpt-4o | Mid |
| 1: Log hints (wizard) | openai | gpt-4o | Mid |
| 2: Refine statement | anthropic | claude-sonnet-4-6 | Mid |
| 3–4: Search queries | openai | gpt-4o | Mid |
| 5–6: Search execution | N/A | N/A (no LLM) | — |
| 7: Skills assessment | anthropic | claude-sonnet-4-6 | Mid |
| 8: Resolution plan | anthropic | claude-opus-4-6 | Top-tier |
| 9: Follow-up questions | anthropic | claude-sonnet-4-6 | Mid |

Click **Save steps**: the changes are saved for this tenant, and the gateway uses them from the next model call, with nothing restarted. A `config_change` audit event is logged.

> **Important:** The Step 8 model has the largest impact on output quality since it generates the primary customer-facing resolution plan. Test any model changes on a non-critical run first.

### Agent settings

The **Settings** tab holds the three settings a VITA run reads, each with this tenant's value. The chip beside its heading, **this agent · this tenant**, says whose value it is: an admin of another tenant sees and sets that tenant's values, and a platform admin sees the values of the tenant they belong to.

| Setting | Default | Range | What it changes |
|---------|---------|-------|-----------------|
| Tavily search depth (`tavily_search_depth`) | `advanced` | `basic` or `advanced` | How deep each web search in Step 6 goes. `advanced` is higher quality but slower. |
| Pinecone top K (`pinecone_top_k`) | 10 | 1–50 | How many internal-KB results Step 5 retrieves per query. |
| Max retries per step (`max_retries_per_step`) | 2 | 0–5 | How many times a model step is tried again after a transient failure — the gateway not answering, a timeout, a rate limit, a server error, or a reply that is not JSON — waiting 5, 10, 20… seconds between tries. |

A number outside its range is read as the nearer bound: `pinecone_top_k` saved as 80 searches with 50, and a negative retry count is 0. A run reads its settings once as each phase starts, so a change reaches the next phase to start — an investigation waiting at the approval gate included — and never a phase already running. The run's trace shows the value each step used: `input.value` on the step's span (`step_search_public_resources`, `step_search_internal_kb`, and each model step), even when the internal-KB step is skipped because no knowledge base is configured.

A value this tenant chose is marked **overridden here**, with the agent's default beside it, and its **Reset** returns that one setting to the default. Only a value that differs from the default is stored, so saving the default is the same as Reset, and a later release that changes a default reaches every tenant that never chose a value. Click **Save settings** to save the tab; the `config_change` audit event names the settings whose value changed, never their values. Without a Tavily key, Step 6 finds no web results whatever the depth.

Before K5b these settings were one value for every tenant, kept in a file under the state directory, and a fourth, `contextual_log_hints`, switched a step no run calls. That file is no longer read and its values were not carried over; `docs/platform/Install.md` §10 says what an upgrade does about it.

### The Secrets tab

The **Secrets** tab holds the keys VITA's own tools send — today one, `tavily_api_key`, the Tavily web-search key Step 6 uses. The key has two rows, and neither ever shows its value:

| Row | Chip | Who sets it | When a run of this tenant reads it |
|-----|------|-------------|------------------------------------|
| This tenant's | **this agent · this tenant** | An admin of this tenant. | Whenever it is set. |
| The default | **this agent** | A platform admin. A tenant admin sees the row with its controls closed and "Platform operators only." | When this tenant has no value of its own. |

Each row says whether it is set and, once set, its fingerprint (a 12-character digest that changes with the value, so two values can be compared without showing either), who set it and when, and when a run last read it. Above the rows the tab says where a run of this tenant reads the key now: this tenant's value, the default, or `TAVILY_API_KEY` in the backend's environment, the fallback a deployment set before the tab existed. With none of the three, Step 6 runs no web search.

To set a value, type it into the row's field and click **Set** (**Replace** once a value is set). The field is never filled in for you, and it is emptied as the value is sent. **Clear** removes the row's value, after you confirm, and the next source in that order serves. Each change is audited as a `config_change` event (`surface: agent_secrets`) naming the key, the row and the action, never the value, and the next run reads it with nothing restarted. A value is stored encrypted under the backend's store key; without one, **Set** is refused (`secrets_store_unconfigured`), and the tab says so and points at `docs/platform/Install.md`.

---

## 12. Administration panels

### 12.1 Users and access

The Users page at `/admin/users` shows all registered users in the tenant. For each user:

| Control | Effect |
|---------|--------|
| Role dropdown | Change between `customer` and `admin`. Takes effect on next sign-in. |
| **Revoke** button | Revokes all active sessions for that user. Their next API call returns 401 and they must re-authenticate. |

All role changes and session revocations log audit events (`role_change`, `session_revoke`).

### 12.2 Auth configuration

The Auth Configuration page at `/admin/auth-config` manages the tenant's authentication settings:

| Setting | Description |
|---------|-------------|
| Google SSO enabled | Toggle Google sign-in button visibility and acceptance. |
| Microsoft SSO enabled | Toggle Microsoft sign-in button visibility and acceptance. |
| Email/password enabled | Toggle the credentials provider. |
| Google allowed domains | Comma-separated list (e.g., `company.com,subsidiary.com`). |

Changes save immediately and log an `auth_config_change` audit event.

Session lifetime is not set here. It is `session_timeout_minutes` under Application Settings (§13), which applies to the whole deployment. This page carried a per-tenant "Session timeout (hours)" field until 1.0; nothing ever read it, so it was removed rather than left to look effective.

> **Warning:** Disabling all auth providers locks out all users. Always keep at least one provider enabled.

### 12.3 Feedback dashboard

The Feedback Dashboard at `/admin/feedback` displays:

- **Overall positive rate** — large percentage and total feedback count.
- **Per-section bars** — horizontal bars showing the positive rate for each of the 8 section types.
- **Recent negative feedback** — table with run ID, section type, comment, and date.

Use the **Days** input (default 90) to adjust the time window.

### 12.4 Activity audit log

The Activity Audit Log at `/admin/audit-log` shows a paginated table (50 entries per page) of all actions across the tenant.

| Column | Description |
|--------|-------------|
| Time | Timestamp of the action. |
| User | Email of the acting user. |
| Action | Action type badge (e.g., `sign_in`, `run_create`, `config_change`). |
| Detail | Expandable JSON payload with structured action data. |
| IP | Client IP address. |

Filter by action type using the text input at the top. Supported action types: `sign_in`, `sign_out`, `run_create`, `run_update`, `run_delete`, `file_upload`, `file_validation_fail`, `config_change`, `blocked_request`, `report_generate`, `role_change`, `session_revoke`, `vendor_registry_edit`, `auth_config_change`.

---

## 13. Application settings

The Application Settings page at `/admin/settings` manages five runtime-tunable settings that can be changed without restarting the application (except where noted). VITA's own settings — search depth, top K and retries — are not here: they are on the agent page's Settings tab, for each tenant (§11, "Agent settings").

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `cors_origins` | String list | From `APP_CORS_ORIGINS` env | Allowed CORS origins. **Requires a backend restart** after change. |
| `session_timeout_minutes` | Integer | `JWT_EXPIRY_HOURS × 60` | Session/JWT expiry window in minutes. Applies to sessions created after the change; a session already issued keeps the expiry it was stamped with at sign-in. |
| `max_upload_size_mb` | Integer | 50 | Per-file upload size cap in megabytes. |
| `require_approval_before_phase2` | Boolean | true | Gate Phase 2 on human approval of the refined statement. |
| `default_llm_provider` | String | openai | Fallback LLM provider when a pipeline step does not specify one. |

### How the settings page works

Each setting row displays: the key name, an **override** or **default** badge indicating whether a database override exists, the value type, a description, and the current default value.

Type-aware editors are provided:

| Value type | Editor |
|-----------|--------|
| String list | Tag input with add/remove chips. Press Enter or comma to add. |
| Boolean | Checkbox toggle with Enabled/Disabled label. |
| Integer | Number input. |
| String | Text input. |

Each row has a **Save** button (persists the current value to the database) and a **Reset to default** button (deletes the database override, reverting to the `.env` default).

### CORS restart banner

When `cors_origins` is modified, a yellow banner appears at the top of the settings page:

> **Restart required.** CORS origins were changed; a rolling restart is needed before the new list takes effect.

The `/health` endpoint also reports `cors_restart_required: true` until the backend is restarted.

---

# Part III — Reference

## 14. Troubleshooting

| Problem | Cause | Solution |
|---------|-------|----------|
| "Access denied" after Google SSO | Email domain not in `google_allowed_domains` and email not in `google_allowed_emails`. | Admin adds your domain or email via `/admin/auth-config`. |
| "Access denied" after Microsoft SSO | Azure AD tenant UUID not in `microsoft_allowed_tenants` and email not in `microsoft_allowed_emails`. | Admin adds your tenant UUID or email. |
| SSO buttons are grayed out | `GOOGLE_CLIENT_ID` or `AZURE_CLIENT_ID` not set in the server's `.env` file. | Admin configures OAuth credentials and restarts the backend. |
| Login fails with "Invalid credentials" | Admin account not bootstrapped (`INITIAL_ADMIN_*` unset), or `CREDENTIALS_ENABLED` is false. | Admin sets `INITIAL_ADMIN_EMAIL` / `INITIAL_ADMIN_PASSWORD` in the server's `.env` and restarts the backend (see INSTALL.md). |
| Investigation stuck in "refining" for >60 seconds | LLM provider timeout or rate limit on Step 0 or Step 2. | Wait for retry (up to 2 retries with exponential backoff). If it fails, run status becomes `error`. |
| "File too large" error on log upload | File exceeds the `max_log_size_mb_per_vendor` limit (default 10 MB). | Filter logs to the relevant time window. Remove routine health-check entries. |
| Works cited contains irrelevant documents | Problem statement was too broad, or web search returned tangentially related results. | Edit the refined statement to be more specific. Answer follow-up questions for better targeting. |
| PII redaction flagged a non-sensitive value | Regex patterns over-matched on values that resemble API keys or tokens. | Report false positives to your admin for threshold tuning. |
| Report export takes >30 seconds | WeasyPrint PDF generation is slow for large runs, or WeasyPrint is not installed. | Try HTML export (faster). If PDF always fails, the backend may need WeasyPrint system libraries. |
| Page refresh requires re-login | JWT is stored in browser memory only. | By design. Sign in again after refresh. |
| Cannot see a run I know exists | Runs are tenant-scoped. Customer-role users see only their own runs. | Ask an admin to verify the run exists and check your role. |
| Works cited holds no web results, only internal-KB documents or none | No Tavily key: neither this tenant's `tavily_api_key`, nor the default, nor `TAVILY_API_KEY` in the backend's environment is set, so Step 6 runs no web search. | An admin sets the key on the agent page's Secrets tab (§11, "The Secrets tab"); the next run searches the web. |

---

## 15. Frequently asked questions

**Can VITA access my vendor systems directly?**

No. VITA only searches public documentation via Tavily web search and your internal knowledge base via Pinecone. It never connects to, authenticates with, or modifies any vendor system. Web search needs a Tavily key, which your admin sets on the agent page's Secrets tab (§11); without one, a run finds no web results.

**Is my data shared with AI providers?**

After PII redaction, run inputs are sent to the configured LLM providers (OpenAI, Anthropic, or Google, depending on the pipeline step). Your admin controls which providers are in use via Admin → Agents → VITA → Config.

**Can I use VITA for more than two vendors?**

VITA's workflow is optimized for two-vendor interoperability. For three or more vendors, create separate runs for each vendor pair.

**How current is the documentation VITA searches?**

VITA performs live web searches via Tavily at investigation time, so results reflect the most current public documentation. Internal KB currency depends on when your Pinecone index was last updated.

**Can I share a run with a colleague?**

Admin-role users see all runs. Customer-role users see only their own. Share results by exporting to HTML or PDF.

**What happens if I close my browser mid-investigation?**

The pipeline runs as a backend background task. Your run and all results are saved server-side. Return to the run detail page to see completed results. You will need to sign in again (see §2.2).

**Can I delete a run?**

Yes. Runs are soft-deleted (hidden from the dashboard but retained in the database for audit purposes). Contact your admin if you need a run restored.

**Can I re-run an investigation?**

No. A completed or errored run cannot be re-investigated. Create a new run with updated information.

---

## 16. Glossary

| Term | Definition |
|------|-----------|
| Approval gate | The human review checkpoint between Phase 1 and Phase 2 where the customer confirms or edits the refined problem statement. |
| Run | A single VITA investigation, identified by a `VITA-NNNN` run number, encompassing all inputs, pipeline outputs, feedback, and reports. |
| Run snapshot | The database record storing all pipeline outputs: refined problem, works cited, skills cited, resolution plan, follow-up questions, and classified inputs. |
| Confidence threshold | The minimum confidence score (default 0.7) for a PII detection to be applied. Configurable via `PII_CONFIDENCE_THRESHOLD`. |
| Interoperability | The ability of two vendor systems to exchange data and function together correctly. |
| LLM | Large Language Model. The AI models powering VITA's pipeline (e.g., `gpt-4o`, `claude-sonnet-4-6`, `claude-opus-4-6`). |
| Namespace | A partition in the Pinecone vector database. VITA uses `tenant_{tenant_id}` as the namespace prefix. |
| Phase 1 | Pipeline Steps 0 + 2: input validation and problem statement refinement. Ends at the approval gate. |
| Phase 2 | Pipeline Steps 3–9: query construction, search, skills assessment, resolution plan generation, and follow-up questions. |
| PII | Personally Identifiable Information: API keys, passwords, IP addresses, email addresses, tokens, and other sensitive data. Automatically redacted before storage. |
| Pipeline config | The model each pipeline step runs: the defaults the agent's manifest declares (`llm.steps[]`), with this tenant's overrides from the agent page's Steps tab (Admin → Agents → VITA), which the gateway resolves at each call. |
| Presidio | Microsoft's open-source PII detection framework, used in VITA's Stage 3 NER-based redaction. |
| Redaction | Replacing sensitive values with placeholders (e.g., `[REDACTED_EMAIL_1]`) while preserving document structure. |
| Resolution plan | VITA's primary output: three cited sections (mitigation, resolution, avoidance) addressing the interoperability issue. |
| Soft delete | Setting a `deleted_at` timestamp on a record instead of removing it from the database. The record is hidden from queries but preserved for audit. |
| Tenant | An organizational isolation boundary. All data belongs to a specific tenant. Every database query filters by `tenant_id`. |
| Works cited | Documentation sources referenced by the resolution plan, with relevance scores and document type classification. |
