# syntax=docker/dockerfile:1
# ─────────────────────────────────────────────────────
# Good First Issue Tracker — Shareable Docker Image
#
# This image contains NO secrets. All configuration is
# injected at runtime via environment variables:
#   docker run --env-file .env good-first-issues
#
# Required env vars:  DISCORD_WEBHOOK_URL, GITHUB_TOKEN
# Optional env vars:  POLL_INTERVAL, FIRST_RUN_LOOKBACK_HOURS,
#                      MAX_GITHUB_WORKERS, CHUNK_SIZE, ORG_REPO_LIMIT
# ─────────────────────────────────────────────────────

FROM python:3.12-slim-bookworm

# Install uv — pinned for reproducible builds
COPY --from=ghcr.io/astral-sh/uv:0.7 /uv /uvx /bin/

WORKDIR /app

# Enable bytecode compilation for faster startup
ENV UV_COMPILE_BYTECODE=1

# ── State persistence ──
# Write state to /app/state/ so it can be volume-mounted
# and survive container restarts/redeploys.
ENV STATE_FILE=/app/state/last_run_state.json

# ── Install dependencies (cached layer) ──
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project

# ── Copy application code ──
# NOTE: .env is excluded via .dockerignore — secrets are
# never baked into the image. They are injected at runtime.
COPY . .

# ── Install the project itself ──
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen

# Ensure state directory exists
RUN mkdir -p /app/state

# ── Healthcheck ──
# Uses a heartbeat file written by the app process.
# Coolify requires curl/wget, but since this is not a web server,
# we check if the process is running via a simple PID file check.
# We install curl in the image for Coolify compatibility.
RUN apt-get update && apt-get install -y --no-install-recommends curl procps && rm -rf /var/lib/apt/lists/*

HEALTHCHECK --interval=60s --timeout=5s --start-period=30s --retries=3 \
    CMD pgrep -f "good_first_issue_tracker" > /dev/null || exit 1

# Graceful shutdown — sends SIGINT so the script's KeyboardInterrupt
# handler can save state before exiting.
STOPSIGNAL SIGINT

CMD ["uv", "run", "python", "main.py", "good-first-issues"]
