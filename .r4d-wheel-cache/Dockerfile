FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DEFAULT_TIMEOUT=120 POETRY_REQUESTS_TIMEOUT=120 PYTHONPATH=/opt/wind-hub/src
WORKDIR /opt/wind-hub
COPY pyproject.toml poetry.lock README.md ./
COPY src ./src
RUN --mount=type=bind,from=wheels,source=.,target=/wheels python -m pip install --no-index --find-links=/wheels --require-hashes -r /wheels/requirements.txt && mkdir -p /var/lib/wind-hub && chown 65532:65532 /var/lib/wind-hub
USER 65532:65532
ENTRYPOINT ["python", "-m"]
