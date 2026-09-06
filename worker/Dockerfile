# KnowFlow worker — the RQ consumer that moves documents through the
# two-phase pipeline (extract -> chunk -> embed -> pgvector).
#
# Built with the repo root as build context (see docker-compose.yml) so it can
# COPY from ai-service/ without duplicating the application code.
#
#   docker build -f worker/Dockerfile -t knowflow-worker .
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /srv

COPY ai-service/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY ai-service/app ./app

EXPOSE 8001

# Default: run the RQ worker loop (a separate image from the API server so the
# two can scale independently — see worker/README.md).
CMD ["python", "-m", "app.queue.worker"]
