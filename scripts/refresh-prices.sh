#!/usr/bin/env bash
# Trigger a price / NAV refresh so the dashboard is current without anyone
# clicking "Refresh prices". Run from cron, e.g. weekdays at 18:47 (after the
# Indian market close + AMFI NAV publish):
#   47 18 * * 1-5  /home/kshit/Projects/portfolio-tracker/scripts/refresh-prices.sh >> /home/kshit/portfolio-refresh.log 2>&1
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-$HOME/Projects/portfolio-tracker}"
URL="${URL:-http://localhost:8000/api/refresh-prices}"

# Load APP_USERNAME / APP_PASSWORD from .env so this still works when Basic Auth
# is enabled. (.env is gitignored and lives only on the host.)
if [ -f "$PROJECT_DIR/.env" ]; then
  # shellcheck disable=SC1090,SC1091
  set -a; . "$PROJECT_DIR/.env"; set +a
fi

auth=()
if [ -n "${APP_PASSWORD:-}" ]; then
  auth=(-u "${APP_USERNAME:-admin}:${APP_PASSWORD}")
fi

echo "[$(date '+%F %T')] refreshing prices…"
code="$(curl -s -o /tmp/refresh-prices.out -w '%{http_code}' -X POST "${auth[@]}" "$URL" || echo 000)"
if [ "$code" = "200" ]; then
  echo "[$(date '+%F %T')] OK: $(cat /tmp/refresh-prices.out)"
else
  echo "[$(date '+%F %T')] FAILED (HTTP $code): $(cat /tmp/refresh-prices.out 2>/dev/null || true)" >&2
  exit 1
fi
