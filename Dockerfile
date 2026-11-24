FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install uv from official pre-built image
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

# Optimize uv performance & bytecode compilation
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# Install dependencies using uv and locked uv.lock
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --all-extras

COPY app ./app
COPY main.py config.py container.py ./
COPY tests ./tests
COPY README.md ./

# Sync project installation
RUN uv sync --frozen --all-extras

# Ensure virtualenv binaries take precedence
ENV PATH="/app/.venv/bin:$PATH"

RUN useradd --create-home --shell /bin/bash appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=15s --retries=3 \
    CMD sh -c "curl -fsS http://localhost:\${API_PORT:-8000}/health || exit 1"

CMD ["sh", "-c", "exec uvicorn main:app --host \"${API_HOST:-0.0.0.0}\" --port \"${API_PORT:-8000}\""]
