#!/usr/bin/env bash
# KnowFlow — quick headless screenshots (chromium only, no extra deps).
#
# Requires the compose stack running (docker compose up --build) and `chromium`
# on PATH. Renders the app at 1440x900 and writes docs/screenshots/*.png.
#
#   ./docs/screenshots/capture.sh

set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="$DIR"
BASE="${1:-http://localhost}"
DELAY="${2:-4}"          # seconds to let the SPA settle before the shot

command -v chromium >/dev/null 2>&1 || { echo "chromium not found on PATH"; exit 1; }

shots=(
  "01_login=http://localhost/auth/login"
  "02_register=http://localhost/register"
  "03_documents=http://localhost/documents"
  "04_upload=http://localhost/documents/new"
  "05_search=http://localhost/search"
  "06_chat=http://localhost/chat"
  "07_grafana=http://localhost:3001"
  "08_api_docs=http://localhost:8000/docs"
)

for entry in "${shots[@]}"; do
  name="${entry%%=*}"
  url="${entry#*=}"
  echo "capturing $name <- $url"
  chromium --headless=new --disable-gpu --hide-scrollbars \
    --window-size=1440,900 --virtual-time-budget="$((DELAY * 1000))" \
    --screenshot="$OUT/$name.png" "$url" 2>/dev/null
done

echo "wrote $OUT/*.png"
ls -1 "$OUT"/*.png