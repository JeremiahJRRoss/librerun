# ============================================================================
# VITA Frontend — Next.js
# Multi-stage build for smaller production image
# ============================================================================

# --- Dependencies ---
FROM docker.io/library/node:22-slim@sha256:43ac6c60b8f89723f746e8a92ce91abd5017e627ce1ddfe4238355d3a30b772c AS deps
WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm ci

# --- Build ---
FROM docker.io/library/node:22-slim@sha256:43ac6c60b8f89723f746e8a92ce91abd5017e627ce1ddfe4238355d3a30b772c AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
# NEXT_PUBLIC_* values are inlined into the client bundle at build time, not
# read at runtime. Without this ARG/ENV, ``npm run build`` runs with the var
# unset and the fallback in ``src/lib/api.ts`` ("http://localhost:8000/api/v1")
# gets baked into every browser bundle — which then fetches from the visitor's
# laptop in production. The matching ``args:`` entry in ``compose.yaml``
# threads the value through.
ARG NEXT_PUBLIC_API_URL
ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL
# next.config.js's /api/v1 rewrite to BACKEND_INTERNAL_URL is computed here
# too, by the build, and baked into the routes manifest: set only at run
# time it proxies nothing (K blueprint §11, T1). A build ARG is in the
# environment of the RUN below and is not kept in any image.
ARG BACKEND_INTERNAL_URL
# `next build` sends Vercel an anonymous usage report unless told not to.
# A build is not a flow a LibreRun operator signed up for, so it sends none
# (A3; the distribution surface matrix, "Build").
ENV NEXT_TELEMETRY_DISABLED=1
RUN npm run build

# --- Runtime ---
FROM docker.io/library/node:22-slim@sha256:43ac6c60b8f89723f746e8a92ce91abd5017e627ce1ddfe4238355d3a30b772c

# OCI image labels (blueprint S9). Static here because they are facts
# about the SOURCE, not about one build: the title, what the image is,
# and the SPDX expression for what this image carries, from the licence
# scope map (REUSE.toml; L36). The build-dependent labels — source, revision, version, created
# — are left to whoever builds: a release publishes no image (L37), and
# only the builder knows its source. Writing the repository down HERE is
# exactly what the publish-purity check exists to prevent, so it stays
# out (gap I6).
LABEL org.opencontainers.image.title="librerun-web" \
      org.opencontainers.image.description="The LibreRun web UI (Next.js)." \
      org.opencontainers.image.licenses="AGPL-3.0-only"
WORKDIR /app

RUN addgroup --system librerun && adduser --system --ingroup librerun librerun
COPY --from=builder --chown=librerun:librerun /app/.next/standalone ./
COPY --from=builder --chown=librerun:librerun /app/.next/static ./.next/static
COPY --from=builder --chown=librerun:librerun /app/public ./public

USER librerun

EXPOSE 3000
ENV PORT=3000 HOSTNAME="0.0.0.0"

CMD ["node", "server.js"]
