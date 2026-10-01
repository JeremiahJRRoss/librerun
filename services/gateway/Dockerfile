# ============================================================================
# LibreRun Gateway — the one process that holds a provider credential
# ============================================================================
#
# Built from the REPOSITORY ROOT, not from this directory: the gateway
# shares the chassis's redaction pipeline and its two export walkers, so
# it needs `backend/app` importable. It does not run the API — there is
# no alembic, no report rendering, no vector store, no bootstrap — and
# the backend, in turn, has no provider SDK. That is the whole point of
# the split (blueprint S4a, L23).
#
#   ./compose.sh up -d            # the gateway is a DEFAULT service
#   docker build -f services/gateway/Dockerfile .

# --- Build stage ---
FROM docker.io/library/python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f AS builder

WORKDIR /build
# Install the LOCK, hashed, and nothing else (A3; LIC-09, C-21), as the
# backend image installs its own: requirements.txt states what the gateway
# accepts, requirements.lock.txt what it runs. --require-hashes refuses any
# file whose hash the lock does not hold, so a changed wheel fails the build
# instead of shipping. Both files are copied, so the image carries its
# provenance; scripts/lock_requirements.sh regenerates the lock.
COPY services/gateway/requirements.txt services/gateway/requirements.lock.txt ./
RUN pip install --no-cache-dir --prefix=/install --require-hashes -r requirements.lock.txt

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
LABEL org.opencontainers.image.title="librerun-gateway" \
      org.opencontainers.image.description="The LibreRun LLM gateway: the one process that holds a provider credential." \
      org.opencontainers.image.licenses="AGPL-3.0-only"

# libgomp1 is Presidio/spaCy's at runtime — the gateway runs the same
# redaction pipeline the intake does.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /install /usr/local

# The spaCy model Presidio's NER stage needs, exactly as the backend
# image installs it. NOT best-effort: without the model the regex stages
# still run, so redaction DEGRADES rather than failing — an outbound
# request would keep a name the intake pipeline removes, and every test
# of the redaction would still pass. A gate that reports success by not
# looking is worse than no gate, so a failed download fails the build.
RUN python -m spacy download en_core_web_lg \
    && python -c "import spacy; spacy.load('en_core_web_lg')"

WORKDIR /app
# The shared chassis modules the gateway imports: pii_service, otlp_walk,
# walkers, config. Copied whole rather than cherry-picked so a future
# import does not fail only in the image.
COPY backend/app /app/app
COPY services/gateway/gateway /app/gateway
COPY services/gateway/stub /app/stub

RUN useradd --create-home --shell /usr/sbin/nologin librerun \
    && chown -R librerun:librerun /app
USER librerun

# LiteLLM's cost table is the one its pinned wheel bundles
# (model_prices_and_context_window_backup.json): without this, it fetches
# the table from GitHub at every start-up (THIRD_PARTY.md), an outbound
# request the gateway never needs. The LLM span's librerun.cost_usd reads
# the same table either way.
ENV LITELLM_LOCAL_MODEL_COST_MAP=True

EXPOSE 8090

# One worker: the key reconciliation at boot is a whole-deployment pass,
# and N workers would run it N times against the same rows.
CMD ["uvicorn", "gateway.main:app", "--host", "0.0.0.0", "--port", "8090"]
