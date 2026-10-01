# The TypeScript reference server (blueprint S5/S5-R).
# Build context: the REPOSITORY ROOT, so every example builds the same way
# and the build lines in the battery and in agents.compose.yaml agree:
#   docker build -f backend/agents/_examples/vercel_ai_answer_ts/Dockerfile \
#       -t librerun-vercel-agent .
#   docker run --rm -p 8090:8090 librerun-vercel-agent
#
# No LibreRun package is installed: this agent serves the Run Contract
# itself, which is the claim the example exists to make. The image holds
# no provider key and no provider account — the only credential it is
# ever given is the agent's LibreRun gateway key (OPENAI_API_KEY in the
# fragment, blueprint S4a, D10), and every model call needs the
# invocation's run token beside it.
FROM docker.io/library/node:22-slim@sha256:43ac6c60b8f89723f746e8a92ce91abd5017e627ce1ddfe4238355d3a30b772c

# OCI image labels (blueprint S9). Static here because they are facts
# about the SOURCE, not about one build: the title, what the image is,
# and the SPDX expression for what this image carries, from the licence
# scope map (REUSE.toml; L36). The build-dependent labels — source, revision, version, created
# — are left to whoever builds: a release publishes no image (L37), and
# only the builder knows its source. Writing the repository down HERE is
# exactly what the publish-purity check exists to prevent, so it stays
# out (gap I6).
LABEL org.opencontainers.image.title="librerun-example-vercel-ai" \
      org.opencontainers.image.description="LibreRun example agent: the Vercel AI SDK speaking Run Contract v1 in TypeScript." \
      org.opencontainers.image.licenses="Apache-2.0"
WORKDIR /app
COPY backend/agents/_examples/vercel_ai_answer_ts/package.json ./package.json
RUN npm install --omit=dev --no-audit --no-fund
COPY backend/agents/_examples/vercel_ai_answer_ts/server.ts ./server.ts
ENV NODE_ENV=production
EXPOSE 8090
CMD ["npx", "--no-install", "tsx", "server.ts"]
