# KnowFlow worker image

The worker is the RQ consumer that moves documents through the two-phase
pipeline: **extract → chunk → embed → pgvector**, then marks the document
`READY` for retrieval.

## Why a dedicated image

Earlier in the build the worker simply reused the `knowflow-ai-service` image
with an overridden command. That worked, but it couples two service lifetimes
that should scale independently:

- the API server and the worker have **different scaling profiles** (workers
  pool horizontally under queue load, the API server scales for request
  latency) — separate images make `docker compose up --scale worker=N` and
  k8s-style `Deployment`/`ReplicaSet` splits explicit;
- the worker exposes its **own Prometheus endpoint on `:8001`** (per-job
  metrics: enqueued / extracted / embedded / failed, queue depth), so the
  observability harness scrapes it directly (see `docker-compose.yml` under
  `prometheus.scrape_configs`).

## Build

The build context is the **repo root** (the Dockerfile needs `ai-service/`
code, which is a sibling, not a parent, of `worker/`):

```bash
docker build -f worker/Dockerfile -t knowflow-worker .
```

Or via compose (which also builds `backend`, `ai-service`, `frontend`):

```bash
docker compose up --build worker
```

## Runtime

```bash
docker compose up --scale worker=4
```

The worker reads the same `REDIS_URL` / `QUEUE_NAME` / `DATABASE_URL` /
`S3_*` environment contract as `ai-service` (`app.config`), so the two images
are interchangeable from the queue's point of view — a job enqueued by the
API server is picked up by whichever worker is free.