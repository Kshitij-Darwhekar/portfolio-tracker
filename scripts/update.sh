#!/usr/bin/env bash
# Update the deployed app to the latest pushed version:
#   1. back up the DB first (safety net)
#   2. pull origin/master
#   3. rebuild + restart the container
# Run manually after you push changes:
#   ~/Projects/portfolio-tracker/scripts/update.sh
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-$HOME/Projects/portfolio-tracker}"
cd "$PROJECT_DIR"

echo "==> Backing up DB before update…"
bash "$PROJECT_DIR/scripts/backup.sh" || echo "WARN: backup failed — continuing anyway"

echo "==> Pulling latest from origin/master…"
git pull origin master

echo "==> Rebuilding and restarting…"
docker compose up -d --build

echo "==> Done. Recent logs:"
docker compose logs --tail=15
