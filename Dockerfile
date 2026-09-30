# Один образ на весь репозиторий: target runtime — для api (и будущего worker), dev — для tools.
FROM python:3.12-slim AS base
COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /uvx /bin/
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY apps/api/pyproject.toml apps/api/
COPY packages/core/pyproject.toml packages/core/
COPY packages/llm/pyproject.toml packages/llm/

FROM base AS runtime
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-workspace
COPY . .
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev
# venv уже собран: uv run не должен доустанавливать dev-зависимости
ENV UV_NO_SYNC=1 PATH=/opt/venv/bin:$PATH
WORKDIR /app/apps/api
EXPOSE 8000
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn sobesnik_api.main:app --host 0.0.0.0 --port 8000 --no-access-log"]

FROM base AS dev
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-install-workspace
COPY . .
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen
ENV PATH=/opt/venv/bin:$PATH
