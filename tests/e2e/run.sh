#!/usr/bin/env bash
# Day 14 — full-pipeline integration test (Milestone 1).
#
# Boots the whole stack (backend, AI service, one RQ worker), runs
# tests/e2e/test_pipeline.py (upload -> storage -> queue -> worker -> extraction ->
# chunking -> embedding -> pgvector -> READY -> retrieval -> RAG generation,
# driven purely through public HTTP — no database seeding, no manual
# /v1/process or /v1/embed), then tears everything back down and reports
# the milestone gate.
#
# Prereqs: knowflow-pg, knowflow-minio and knowflow-redis containers running;
# backend/ai-service dependencies installed.
#
#   ./tests/e2e/run.sh

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LOG=/tmp/kf-e2e-logs
mkdir -p "$LOG"

say() { printf '== %s\n' "$*"; }
die() { printf 'FATAL: %s\n' "$*" >&2; exit 1; }

PIDS=()
cleanup() {
  trap - EXIT INT TERM
  local pid
  for pid in "${PIDS[@]:-}"; do
    # the services are setsid'd so each is its own process group; signal the
    # whole group (npm -> tsx watch -> node all die, not just the parent)
    kill -- -"$pid" 2>/dev/null || kill "$pid" 2>/dev/null || true
  done
  wait 2>/dev/null || true
  say "servers + worker stopped (logs in $LOG/)"
}
trap cleanup EXIT INT TERM

# ---- prereq: infra containers ----
docker exec knowflow-pg psql -U knowflow -d knowflow -tAc "SELECT 1;" >/dev/null 2>&1 \
  || die "Postgres (knowflow-pg) not reachable — start the Docker containers first"
docker exec knowflow-redis redis-cli -n 0 PING 2>/dev/null | grep -q PONG \
  || die "Redis (knowflow-redis) not reachable"
curl -s --max-time 3 -o /dev/null http://127.0.0.1:9000 \
  || die "MinIO (knowflow-minio) not reachable on :9000"

say "checking :3000 / :8001 are free"
ss -ltn 2>/dev/null | grep -qE ':(3000|8001)\b' \
  && die "a server already listens on :3000 or :8001 — stop it first"

boot() { # boot <logfile> <cwd> <cmd...> — one setsid process group per service
  local logfile="$1" cwd="$2"; shift 2
  setsid bash -c 'cd "$1" && shift && exec "$@"' _ "$cwd" "$@" \
    > "$LOG/$logfile" 2>&1 < /dev/null &
  PIDS+=("$!")
  printf '  pid %s -> %s\n' "$!" "$LOG/$logfile"
}
wait_http() { # wait_http <url> <label>
  local url="$1" label="$2" tries=60
  while (( tries-- )); do
    curl -s --max-time 2 -o /dev/null "$url" && return 0
    sleep 0.5
  done
  die "$label never came up -> glance at $LOG/*.log"
}

say "boot backend (:3000)"
boot backend.log "$ROOT/backend" npm run dev
wait_http "http://127.0.0.1:3000/health" "backend"
echo "  backend healthy"

say "boot ai-service (:8001)"
boot ai-service.log "$ROOT/ai-service" .venv/bin/uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8001
wait_http "http://127.0.0.1:8001/health" "ai-service"
echo "  ai-service healthy"

say "boot one RQ worker"
boot worker.log "$ROOT/ai-service" .venv/bin/python -m app.queue.worker
sleep 3
ps -p "${PIDS[-1]}" >/dev/null || die "worker died on boot -> tail -20 $LOG/worker.log"
echo "  worker alive"

say "run the full-pipeline integration test"
cd "$ROOT"
"$ROOT/ai-service/.venv/bin/python" -m pytest -q -s tests/e2e/test_pipeline.py

say "milestone gate: PASSED — upload -> storage -> queue -> worker -> "
say "                 extraction -> chunking -> embedding -> pgvector -> READY"
say "                 retrieval -> RAG generation (extractive baseline)"
echo "  (driven through public HTTP only; the DB writes came straight from the"
echo "   worker pipeline, verified on a second connection, retrieval + generation included)"
exit 0