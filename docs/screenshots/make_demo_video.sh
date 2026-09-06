#!/usr/bin/env bash
# Day 35 — record the live full-stack demo (app + Grafana + Prometheus) to an mp4.
#
#   docs/screenshots/make_demo_video.sh [out.mp4]
#
# Env overrides:
#   PYTHON      the interpreter with playwright + httpx (default ai-service venv)
#   DISPLAY     the X display to record        (default :0)
#   SCREEN_SIZE framebuffer size to grab       (default 1366x768)
#   KNOWFLOW_DEMO_EMAIL / KNOWFLOW_DEMO_PASSWORD
#               reuse an existing tenant instead of seeding a fresh one
#
# The compose stack must already be up (frontend :80, grafana :3001, prometheus :9090).

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUT="${1:-$ROOT/docs/screenshots/live-demo.mp4}"
TMP="/tmp/kf-demo-$$.mp4"
PY="${PYTHON:-$ROOT/ai-service/.venv/bin/python}"
DISP="${DISPLAY:-:0}"
SIZE="${SCREEN_SIZE:-1366x768}"

ffmpeg -y -loglevel error \
  -f x11grab -video_size "$SIZE" -framerate 30 -i "$DISP.0" \
  -c:v libx264 -preset veryfast -crf 20 -pix_fmt yuv420p -movflags +faststart \
  "$TMP" &
FFPID=$!
trap 'kill $FFPID 2>/dev/null || true' EXIT

sleep 2
DISPLAY="$DISP" "$PY" "$ROOT/docs/screenshots/capture_demo_video.py"

kill "$FFPID"
wait "$FFPID" 2>/dev/null || true
mv "$TMP" "$OUT"
echo "Recorded $OUT ($(du -h "$OUT" | cut -f1))"