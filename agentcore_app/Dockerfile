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

COPY requirements.txt .
# python:3.13-slim's bundled pip/setuptools is old enough that building
# pyaudio from source hits `AttributeError: module 'pkgutil' has no
# attribute 'ImpImporter'` (removed in Python 3.12+, only triggered by the
# legacy pkg_resources-based build path a stale setuptools still uses).
# Upgrading first avoids that path entirely.
RUN pip install --no-cache-dir --upgrade pip setuptools wheel && \
    pip install --no-cache-dir -r requirements.txt

COPY strands_agent.py .

RUN useradd -m -u 1000 appuser && \
    chown -R appuser:appuser /app
USER appuser

ENV CONTAINER_ENV=true

# Required by AgentCore Runtime
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8080/ping').read()"]

CMD ["uvicorn", "strands_agent:app", "--host", "0.0.0.0", "--port", "8080"]
