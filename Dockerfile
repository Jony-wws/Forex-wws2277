# FX INVESTMENT — full TeamAgent stack in one image.
# Runs orchestrator (= forecast_scanner + paper_traders + 60 agents)
# + watchdog + FastAPI dashboard, sharing /data persistent volume for state.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=UTC

# build deps for pandas/numpy/lxml etc.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential curl ca-certificates tzdata \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install python deps first (better Docker cache)
COPY teamagent/requirements.txt /tmp/requirements.txt
RUN pip install --upgrade pip \
 && pip install -r /tmp/requirements.txt

# Copy application
COPY . /app

# Persistent volume mountpoint (Fly volume mounted here, shared by all 3 procs)
ENV TEAMAGENT_STATE_DIR=/data/state \
    TEAMAGENT_LOGS_DIR=/data/logs \
    PYTHONPATH=/app
RUN mkdir -p /data/state /data/logs

EXPOSE 8080

# Single-container multi-process supervisor (no systemd). All children share
# /data so dashboard reads the same state files orchestrator writes.
CMD ["bash", "/app/infra/fly/start.sh"]
