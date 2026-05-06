# syntax=docker/dockerfile:1.6
#
# FOREX 28-pair dashboard — production image for Fly.io free tier.
# Slim Python 3.11 base, deps installed from pyproject.toml, app runs
# as a non-root user on port 8080 with a /api/cycle healthcheck.

FROM python:3.11-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# System deps: curl is used by the HEALTHCHECK; build-essential is
# kept minimal — pandas/numpy ship manylinux wheels for 3.11 so no
# compiler is needed at install time.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps first for better layer caching.
COPY pyproject.toml ./
RUN pip install --upgrade pip \
    && pip install \
        "fastapi>=0.115.0" \
        "uvicorn[standard]>=0.30.0" \
        "yfinance>=0.2.40" \
        "pandas>=2.2.0" \
        "numpy>=1.26.0" \
        "slowapi>=0.1.9"

# Application code.
COPY app ./app
COPY static ./static

# Pre-create the runtime state dir (gitignored) so the app can write
# its in-memory cache snapshots even on a read-only filesystem layer.
RUN mkdir -p /app/state /app/reports

# Run as non-root.
RUN groupadd --system --gid 1001 forex \
    && useradd  --system --uid 1001 --gid forex --home-dir /app --shell /sbin/nologin forex \
    && chown -R forex:forex /app
USER forex

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8080/api/cycle >/dev/null || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
