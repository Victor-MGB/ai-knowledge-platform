"""Day-12 RQ worker entrypoint: `python -m app.queue.worker`.

Pops `run_job_process` / `run_job_embed` tasks off the `knowflow` queue and
executes them. One worker process holds one set of service connections for
its whole lifetime (see `app.queue.jobs.default_service`).

Day 30: the worker exposes its own Prometheus scrape endpoint on
`metrics_port` (default 8001). RQ's default `Worker` executes each job in a
forked work-horse whose metric increments would land in copy-on-write memory
invisible to that scrape server, so we use `SimpleWorker` — RQ's in-process
execution mode (no fork), which keeps queue failure/latency metrics countable
and truthful while the job itself runs in the same thread/process
(acceptable for one ingestion worker).
"""

import logging
import os

from ..core.config import get_settings


def main() -> None:
    settings = get_settings()

    # expose the worker's own metrics (queue outcomes, job durations) for the
    # compose Prometheus before we start consuming work
    if settings.metrics_enabled:
        import prometheus_client

        prometheus_client.start_http_server(
            settings.metrics_port, addr="0.0.0.0", registry=prometheus_client.REGISTRY
        )

    # resolve services once up front so a bad config fails at boot, inside the
    # queue worker, not after hundreds of jobs are "queued" and going nowhere
    from .jobs import default_service

    default_service()

    import socket

    from redis import Redis
    from rq import SimpleWorker

    connection = Redis.from_url(settings.redis_url)
    # A worker's RQ name must be unique across the fleet. Containers all run
    # this entrypoint as PID 1, so `knowflow-{pid}` collides on every replica
    # and RQ refuses the stragglers ("active worker already exists") — leaving
    # only ONE worker consuming despite a scaled compose. Anchor the name to
    # the container hostname (unique per replica) + pid for same-host safety.
    worker = SimpleWorker(
        [settings.queue_name],
        connection=connection,
        name=f"knowflow-{socket.gethostname()}-{os.getpid()}",
        log_job_description=False,
    )
    logging.getLogger("rq.worker").setLevel(settings.debug and logging.DEBUG or logging.INFO)
    # Run RQ's embedded scheduler: transient failures requeue with a delay via
    # `enqueue_in` (the job lands in the *scheduled* registry); without a
    # scheduler those jobs are stranded forever and the document never
    # reaches READY even though the retry budget is far from exhausted.
    worker.work(with_scheduler=True)


if __name__ == "__main__":
    main()