# __AGENT_NAME__ — Run Contract v1 in TypeScript.
# Build context: the REPOSITORY ROOT, so every agent builds the same way
# and the build lines in agents.compose.yaml agree; by hand:
#   docker build -f backend/agents/__PACKAGE__/Dockerfile -t librerun-__AGENT_ID__ .
#   docker run --rm -p 8090:8090 librerun-__AGENT_ID__
#
# No LibreRun package is installed: this agent serves the Run Contract
# itself (`@librerun/agent` is v1.1). The image holds no provider key and
# no provider account — the only credential it is ever given is this
# agent's LibreRun gateway key (OPENAI_API_KEY in the compose service,
# blueprint S4a, D10), and every model call carries the invocation's run
# token beside it.
# The base is pinned by tag and digest (R14), so a rebuild cannot change it
# underneath you. LibreRun moves the digest with
# scripts/refresh_image_digests.py, in a reviewed change; move yours the
# same way when you take a newer base.
FROM docker.io/library/node:22-slim@sha256:43ac6c60b8f89723f746e8a92ce91abd5017e627ce1ddfe4238355d3a30b772c
WORKDIR /app
COPY backend/agents/__PACKAGE__/package.json ./package.json
RUN npm install --omit=dev --no-audit --no-fund
COPY backend/agents/__PACKAGE__/server.ts ./server.ts
ENV NODE_ENV=production
EXPOSE 8090
CMD ["npx", "--no-install", "tsx", "server.ts"]
