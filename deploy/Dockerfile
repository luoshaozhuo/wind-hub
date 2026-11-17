# Shared OCI image for wind-hub business services and CLI.
# No service configuration, credentials or mutable runtime state is baked in.
FROM python:3.11-slim AS locked-requirements

ENV PIP_NO_CACHE_DIR=0 \
    PIP_DEFAULT_TIMEOUT=300 \
    PIP_RETRIES=10

WORKDIR /opt/wind-hub
COPY pyproject.toml poetry.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install poetry==2.5.1 poetry-plugin-export==1.10.1
RUN poetry export --only main --extras all --format requirements.txt \
    --output /tmp/requirements.txt

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=0 \
    PIP_DEFAULT_TIMEOUT=300 \
    PIP_RETRIES=10 \
    PYTHONPATH=/opt/wind-hub/src

WORKDIR /opt/wind-hub
COPY src ./src
COPY --from=locked-requirements /tmp/requirements.txt /tmp/requirements.txt
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install --require-hashes -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt \
    && mkdir -p /var/lib/wind-hub \
    && chown 65532:65532 /var/lib/wind-hub

USER 65532:65532
ENTRYPOINT ["python", "-m"]
