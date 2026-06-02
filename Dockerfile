# syntax=docker/dockerfile:1.7
# AutoApply worker image for Fly.io.
# Single process, no HTTP port \u2014 the Telegram bot uses long-polling.
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Build deps for cryptography / asyncpg wheels (slim image has no compiler)
# and CA certs for outbound TLS to Gmail SMTP + Neon + Telegram.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        build-essential libpq-dev ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first so source changes don't bust the wheel cache.
COPY requirements.txt ./
RUN pip install -r requirements.txt

# Now the app
COPY . .

# Run alembic upgrade head on deploy via fly.toml [deploy] release_command,
# so the runtime container just starts the bot.
CMD ["python", "main.py"]
