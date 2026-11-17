# Shared OCI image for wind-hub business services and CLI.
# No service configuration, credentials or mutable runtime state is baked in.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /opt/wind-hub
COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m pip install --no-cache-dir ".[kafka]" \
    && mkdir -p /var/lib/wind-hub \
    && chown 65532:65532 /var/lib/wind-hub

USER 65532:65532
ENTRYPOINT ["python", "-m"]
