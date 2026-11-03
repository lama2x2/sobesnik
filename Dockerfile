# Один образ на весь репозиторий: target runtime — для api, worker, bot и web, dev — для tools,
# runtime-gpu — для worker в профиле gpu.
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
COPY apps/bot/pyproject.toml apps/bot/
COPY apps/web/pyproject.toml apps/web/
COPY packages/asr/pyproject.toml packages/asr/
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

# Профиль gpu (008 §4): воркер с whisper на CUDA. CUDA-библиотеки для faster-whisper (CTranslate2 4:
# CUDA 12, cuDNN 9) ставятся pip-пакетами NVIDIA — без тяжёлого образа nvidia/cuda.
FROM runtime AS runtime-gpu
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --python /opt/venv "nvidia-cublas-cu12==12.*" "nvidia-cudnn-cu12==9.*"
ENV LD_LIBRARY_PATH=/opt/venv/lib/python3.12/site-packages/nvidia/cublas/lib:/opt/venv/lib/python3.12/site-packages/nvidia/cudnn/lib

FROM base AS dev
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-install-workspace
COPY . .
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen
ENV PATH=/opt/venv/bin:$PATH
