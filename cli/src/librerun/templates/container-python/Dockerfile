# __AGENT_NAME__ — a Run Contract v1 agent on the LibreRun agent SDK.
# Build context: the REPOSITORY ROOT, so the SDK installs from its source
# tree (it is not published anywhere yet). agents.compose.yaml builds it
# exactly this way; by hand:
#   docker build -f backend/agents/__PACKAGE__/Dockerfile -t librerun-__AGENT_ID__ .
#   docker run --rm -p 8090:8090 librerun-__AGENT_ID__
#
# The image holds no provider key and no provider account: the only
# credential it is ever given is this agent's LibreRun gateway key
# (OPENAI_API_KEY in the compose service — blueprint S4a, D10), and every
# model call carries the invocation's run token beside it.
# The base is pinned by tag and digest (R14), so a rebuild cannot change it
# underneath you. LibreRun moves the digest with
# scripts/refresh_image_digests.py, in a reviewed change; move yours the
# same way when you take a newer base.
FROM docker.io/library/python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f
COPY sdk/python/librerun-agent /opt/librerun-agent
RUN pip install --no-cache-dir "/opt/librerun-agent[uvicorn,otel]"
COPY backend/agents/__PACKAGE__/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY backend/agents/__PACKAGE__/agent.py /app/agent.py
EXPOSE 8090
CMD ["python", "/app/agent.py", "--port", "8090"]
