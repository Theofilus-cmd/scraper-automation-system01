# syntax=docker/dockerfile:1

FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen


FROM python:3.12-slim AS runtime

RUN groupadd --system appuser && useradd --system --gid appuser appuser

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY app ./app
COPY tests ./tests
COPY alembic.ini ./alembic.ini
# Needed for pytest's own config discovery at runtime (NOT for `uv sync`,
# which already ran in the builder stage above). Without this, `docker
# compose exec api pytest` finds no pyproject.toml anywhere from /app
# upward, so [tool.pytest.ini_options] (asyncio_mode="auto", the
# `integration` marker) is silently never applied -- pytest falls back to
# strict asyncio mode and unregistered markers, and every `async def`
# test fails with "async def functions are not natively supported"
# (doc 17 hotfix -- confirmed root cause of that failure, not a pytest
# version issue).
COPY pyproject.toml ./pyproject.toml

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

USER appuser

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
