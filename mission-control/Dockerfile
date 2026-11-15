# One image for the mission-control services; the command picks the service.
#   docker build --platform linux/amd64 -t <registry>/writer:<tag> mission-control
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    MC_SCHEMA_DIR=/app/schema \
    MISSION_CONTROL_CONFIG=/app/deploy/cloudrun.yaml

WORKDIR /app
COPY pyproject.toml ./
COPY mission_control ./mission_control
COPY schema ./schema
COPY deploy ./deploy
RUN pip install --no-cache-dir ".[firestore,pubsub]" gunicorn==23.0.0 \
 && useradd --create-home --uid 10001 app
USER app

# Cloud Run sets $PORT.
CMD exec gunicorn --bind ":${PORT:-8080}" --workers 1 --threads 8 --timeout 60 "mission_control.writer:create()"
