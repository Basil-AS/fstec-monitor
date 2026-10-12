FROM ghcr.io/astral-sh/uv:0.12.19 AS uv-bin
FROM python:3.14-slim-trixie AS build

COPY --from=uv-bin /uv /uvx /bin/
RUN apt-get update \
    && apt-get install --no-install-recommends -y git \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

FROM python:3.14-slim-trixie AS runtime
RUN groupadd --gid 1002 app \
    && useradd --uid 1002 --gid 1002 --home-dir /app --no-create-home \
        --shell /usr/sbin/nologin app \
    && install -d -o 1002 -g 1002 /app /data
COPY --from=build --chown=1002:1002 /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    FSTEC_DATABASE_URL=sqlite:////data/fstec-monitor.db \
    FSTEC_STORAGE_DIR=/data/objects
WORKDIR /app
USER 1002:1002

ENTRYPOINT ["fstec-monitor-bot"]
