# Use a lightweight Python base image
FROM python:3.12-slim-bookworm

# Install uv from the official pre-built image
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Set working directory
WORKDIR /app

# Enable bytecode compilation
ENV UV_COMPILE_BYTECODE=1

# Copy the lockfile and pyproject.toml first to cache dependency installation
COPY pyproject.toml uv.lock ./

# Install dependencies without installing the project itself
# (since we want to cache dependencies separately from code changes)
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project

# Copy the rest of the application code
COPY . .

# Install the project packages
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen

# Ensure our state directory exists and is writable
RUN mkdir -p /app/state

# Run the script using main.py so it's generalizable to other scripts if needed
CMD ["uv", "run", "python", "main.py", "good-first-issues"]
