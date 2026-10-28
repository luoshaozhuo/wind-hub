FROM python:3.11-slim

WORKDIR /opt/wind-hub
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m pip install --no-cache-dir . \\
    && mkdir -p /var/lib/wind-hub \\
    && chown -R 65532:65532 /var/lib/wind-hub

USER 65532:65532
ENTRYPOINT ["wind-hub-sink-service"]
