# Use ARM64 Python base image for AgentCore Runtime
FROM --platform=linux/arm64 public.ecr.aws/docker/library/python:3.13-slim

# Install system dependencies for PyAudio
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    libasound-dev \
    libportaudio2 \
    libportaudiocpp0 \
    portaudio19-dev \
    gcc \
    python3-dev && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# The lock, not requirements.txt: exact transitive versions, so an image
# rebuilt from the same commit installs the same packages. See the header of
# requirements.lock.txt for why that matters here and how to regenerate it.
# Only the lock is copied, so editing the human-facing requirements.txt
# doesn't invalidate this layer — regenerating the lock does.
COPY requirements.lock.txt .
# python:3.13-slim's bundled pip/setuptools is old enough that building
# pyaudio from source hits `AttributeError: module 'pkgutil' has no
# attribute 'ImpImporter'` (removed in Python 3.12+, only triggered by the
# legacy pkg_resources-based build path a stale setuptools still uses).
# Upgrading first avoids that path entirely.
RUN pip install --no-cache-dir --upgrade pip setuptools wheel && \
    pip install --no-cache-dir -r requirements.lock.txt

# portal.html is read at request time by handoff.py, so it has to ship too.
COPY strands_agent.py handoff.py portal_context.py portal.html ./

RUN useradd -m -u 1000 appuser && \
    chown -R appuser:appuser /app
USER appuser

ENV CONTAINER_ENV=true

# Required by AgentCore Runtime
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8080/ping').read()"]

# Run under opentelemetry-instrument, the AWS-documented entry point for
# AgentCore observability. The wrapper configures the OTel SDK from the
# OTEL_* environment before uvicorn imports the app, and activates the
# auto-instrumentation aws-opentelemetry-distro brings with it — so alongside
# the spans Strands emits for itself (bidi_session, execute_tool, response),
# traces now also carry the FastAPI request, the botocore calls to Bedrock and
# DynamoDB, and the gateway's MCP round trips, which the in-process setup
# never saw.
#
# strands_agent._setup_telemetry stays, and detects that this has already
# happened rather than adding a second exporter to the same provider (which
# would export every span twice). It's the fallback for every other way this
# runs: `python strands_agent.py`, a plain uvicorn command, an older image.
#
# One trap worth knowing, because it fails silently. The wrapper honours
# OTEL_EXPORTER_OTLP_ENDPOINT, and a runtime configured with the conventional
# `http://localhost:4318` will send every batch to a collector that does not
# exist inside a custom container — nothing there listens, and the spans are
# dropped with only a connection error in the log. Either unset that variable
# so the distro applies its own AgentCore default, or point it at the signed
# CloudWatch endpoint the in-process path uses:
# `https://xray.<region>.amazonaws.com/v1/traces`. See DEPLOY.md.
CMD ["opentelemetry-instrument", "uvicorn", "strands_agent:app", \
     "--host", "0.0.0.0", "--port", "8080"]
