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
#
# LILY_ECAPA_ALLOW_FETCH=1 is set for THIS LAYER ONLY — the build is the one
# moment a Hugging Face fetch is correct. At runtime the module defaults to
# HF_HUB_OFFLINE so loading the baked model can never become a network wait.
# The savedir is /app/.cache/lily-ecapa, not /tmp: a tmpfs-mounted /tmp
# shadows anything baked there, which put the cold download back in front of
# recognition (live 2026-08-08: recognition 3m31s and 16 turns late).
RUN LILY_ECAPA_ALLOW_FETCH=1 python -c "import lily_voice_embedder as v; assert v.lily_voice_embedder_available()"

# Prove the baked model loads with the network forbidden — exactly the
# runtime path. A build that can only load ECAPA while online has not
# actually baked it, and would fail open into a multi-minute session stall.
RUN HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
    python -c "import lily_voice_embedder as v; assert v.lily_voice_embedder_available()"

CMD ["python", "lily_agent.py", "start"]
