# Run Contract v1 reference agent on the LibreRun agent SDK (blueprint S4).
# Build context: the REPOSITORY ROOT, so the SDK installs from its source
# tree (it is not published anywhere yet):
#   docker build -f backend/agents/_examples/echo_container/Dockerfile -t librerun-echo-agent .
#   docker run --rm -p 8090:8090 librerun-echo-agent
# agents.compose.yaml builds it exactly this way under the demo profile.
FROM docker.io/library/python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f

# OCI image labels (blueprint S9). Static here because they are facts
# about the SOURCE, not about one build: the title, what the image is,
# and the SPDX expression for what this image carries, from the licence
# scope map (REUSE.toml; L36). The build-dependent labels — source, revision, version, created
# — are left to whoever builds: a release publishes no image (L37), and
# only the builder knows its source. Writing the repository down HERE is
# exactly what the publish-purity check exists to prevent, so it stays
# out (gap I6).
LABEL org.opencontainers.image.title="librerun-example-echo" \
      org.opencontainers.image.description="LibreRun example agent: the reference echo container on the Python SDK." \
      org.opencontainers.image.licenses="Apache-2.0"
COPY sdk/python/librerun-agent /opt/librerun-agent
RUN pip install --no-cache-dir "/opt/librerun-agent[uvicorn,otel]"
COPY backend/agents/_examples/echo_container/echo_agent.py /app/echo_agent.py
EXPOSE 8090
CMD ["python", "/app/echo_agent.py", "--port", "8090"]
