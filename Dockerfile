FROM python:3.13-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir ".[postgres]"
RUN useradd --system --uid 10001 monitor && mkdir -p /data && chown -R monitor:monitor /data /app
USER monitor
ENV FSTEC_STORAGE_DIR=/data/objects
CMD ["fstec-monitor", "run"]
