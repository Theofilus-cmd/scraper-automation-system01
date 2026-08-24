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

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

USER appuser

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
