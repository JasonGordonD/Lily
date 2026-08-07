# Minimal container for running the LILY agent on LiveKit Agents.
# Modeled on the Lovebirds fleet Dockerfile (no prmpt_common vendoring).
# syntax=docker/dockerfile:1

ARG PYTHON_VERSION=3.13
FROM python:${PYTHON_VERSION}-slim AS base

# Keep Python from buffering stdout/stderr so crashes always emit logs.
ENV PYTHONUNBUFFERED=1

# Disable pip version check to speed up builds.
ENV PIP_DISABLE_PIP_VERSION_CHECK=1

# Non-privileged user the app runs under.
ARG UID=10001
RUN adduser \
    --disabled-password \
    --gecos "" \
    --home "/app" \
    --shell "/sbin/nologin" \
    --uid "${UID}" \
    appuser

# Build dependencies for Python packages with native extensions.
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    python3-dev \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependency files first, for efficient layer caching.
COPY requirements.txt requirements-voice-identity.txt ./

# Durable voice recognition is an active product feature, not an optional
# local extra. Install CPU PyTorch explicitly (avoids CUDA image bloat), then
# the application and ECAPA/SpeechBrain dependencies.
RUN pip install --no-cache-dir \
      --index-url https://download.pytorch.org/whl/cpu \
      torch torchaudio \
  && pip install --no-cache-dir \
      -r requirements.txt \
      -r requirements-voice-identity.txt

# All remaining application files (excludes .dockerignore entries).
COPY . .

RUN chown -R appuser:appuser /app

USER appuser

# Pre-download the Silero VAD model so startup never fetches at runtime.
RUN python -c "from livekit.plugins.silero import VAD; VAD.load()"

# Fail the image build if durable voice identity cannot load, and bake the
# ECAPA model into the image so the first live session never downloads it.
RUN python -c "import lily_voice_embedder as v; assert v.lily_voice_embedder_available()"

CMD ["python", "lily_agent.py", "start"]
