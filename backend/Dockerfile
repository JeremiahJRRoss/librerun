# ============================================================================
# LibreRun Backend — FastAPI
# Multi-stage build for smaller production image
# ============================================================================

# --- Build stage ---
FROM docker.io/library/python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f AS builder

WORKDIR /build
# Install from the LOCK, not the range list. requirements.txt states what we
# are willing to accept; requirements.lock.txt states what we tested. Building
# from the ranges is how a routine rebuild crossed an SDK major and took out
# every Anthropic-backed pipeline step in a deployment, with no code or config
# change to point at. Both files are copied so the image carries its own
# provenance; regenerate the lock with scripts/lock_requirements.sh, which
# resolves under this same Python 3.12.
COPY requirements.txt requirements.lock.txt ./
RUN pip install --no-cache-dir --prefix=/install -r requirements.lock.txt
# The LangGraph adapter and LangGraph itself (blueprint S3): the demo runs
# the LangGraph example beside the bundled agent from this one image
# (LIBRERUN_AGENTS_PATH=agents:agents/_examples), so the adapter the
# example imports must be importable here, not only in the CI battery.
# The adapter's dependency tree gets its own lock. `-c` alone was not enough:
# a constraints file can only constrain packages it lists, so langgraph — which
# the chassis lock never mentions — still resolved freely and a new release
# could change an otherwise identical rebuild. adapters/requirements.lock.txt
# pins that tree, resolved against the chassis lock so every shared package
# agrees; the adapter itself then installs from the local path with --no-deps,
# which is what stops pip resolving anything new at this step.
#
# It is a second lock rather than rows in the chassis one because
# chassis-zero-agents deliberately runs with LangGraph absent.
#
# PIP_CONSTRAINT on the last step is not decoration. `--no-deps` governs
# RUNTIME dependencies; the adapter's [build-system].requires
# (`setuptools>=68`) is resolved by pip in a fresh ISOLATED environment that
# no lock and no `-c` reaches, so an otherwise identical rebuild still picked
# up whatever setuptools had shipped that morning. PIP_CONSTRAINT is the one
# documented hook pip passes INTO that environment; adapters/build-constraints.txt
# pins it, generated from the pyproject's own build-system block.
COPY adapters ./adapters
RUN pip install --no-cache-dir --prefix=/install -r adapters/requirements.lock.txt \
 && PIP_CONSTRAINT=adapters/build-constraints.txt \
    pip install --no-cache-dir --prefix=/install --no-deps ./adapters

# --- Runtime stage ---
FROM docker.io/library/python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f

# OCI image labels (blueprint S9). Static here because they are facts
# about the SOURCE, not about one build: the title, what the image is,
# and the SPDX expression for what this image carries, from the licence
# scope map (REUSE.toml; L36). The build-dependent labels — source, revision, version, created
# — are left to whoever builds: a release publishes no image (L37), and
# only the builder knows its source. Writing the repository down HERE is
# exactly what the publish-purity check exists to prevent, so it stays
# out (gap I6).
LABEL org.opencontainers.image.title="librerun-backend" \
      org.opencontainers.image.description="The LibreRun chassis: intake, run lifecycle, approval gate, PII redaction, reports." \
      org.opencontainers.image.licenses="AGPL-3.0-only AND Apache-2.0 AND MIT"

# System libraries:
#   - libgomp1 is required by Presidio / spaCy NLP models at runtime.
#   - The remaining packages are required by WeasyPrint PDF rendering —
#     without them the report service catches the failure, logs
#     `weasyprint_failed` with fallback="html", and writes <task_id>.html
#     instead, which the download endpoint then serves as text/html. The
#     task still reports "complete", so a user who asked for a PDF simply
#     receives an HTML file.
#     (This comment previously said the fallback kept a misleading .pdf
#     extension. It does not — `report_service.py` rewrites both the path
#     and the format — and that wording had already been copied into
#     docs/Install.md, where it would have sent operators looking for a
#     symptom that never appears.)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 \
    libpango-1.0-0 \
    libpangoft2-1.0-0 \
    libharfbuzz0b \
    libfontconfig1 \
    libffi-dev \
    shared-mime-info \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --from=builder /install /usr/local
COPY . .

# Required for the Presidio NER stage — without this model, _get_presidio()
# silently disables NER and only regex-based PII redaction runs, which misses
# names, emails, locations, and organizations. PII must not reach the DB.
RUN python -m spacy download en_core_web_lg

# Create non-root user (works with both Docker and rootless Podman)
# /app/data/state is the agents' runtime-state root (LIBRERUN_STATE_DIR
# in compose, a named volume): created and chowned here so the volume
# inherits the runtime user's ownership at first mount.
RUN groupadd -r librerun && useradd -r -g librerun -d /app librerun \
    && mkdir -p /app/data/files /app/data/logs /app/data/state \
    && chown -R librerun:librerun /app

# Make entrypoint executable
RUN chmod +x /app/entrypoint.sh

# The image default is the unprivileged user: any override anywhere —
# `docker run <image> alembic ...`, a Kubernetes `command:` (which
# replaces the ENTRYPOINT) — runs as 'librerun'. The ./data/logs
# chown-then-drop self-heal needs a root START, so the compose
# deployment opts in explicitly with `user: "0:0"`; the entrypoint
# then fixes the mount's ownership and immediately re-execs itself as
# 'librerun' via setpriv. Migrations and uvicorn never run as root.
USER librerun

EXPOSE 8000

# ENTRYPOINT (not CMD) so a command override that keeps the entrypoint
# (`docker run <image> alembic ...`, a compose `command:`, Kubernetes
# `args:`) still enters the wrapper: with the compose root-start it
# gets the chown-then-drop; without it, it simply runs as 'librerun'. The
# default CMD 'serve' selects the migrations + bootstrap + uvicorn
# path in entrypoint.sh; any other argv is exec'd verbatim after the
# drop.
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["serve"]
