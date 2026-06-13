#!/usr/bin/env bash
# Back up the portfolio SQLite DB: timestamped snapshot + retention, plus an
# optional off-device copy (e.g. a Nextcloud-synced folder, so a disk failure
# on the Pi doesn't lose everything).
#
# Run from cron, e.g. nightly at 02:13:
#   13 2 * * *  /home/kshit/Projects/portfolio-tracker/scripts/backup.sh >> /home/kshit/portfolio-backup.log 2>&1
set -euo pipefail

# --- config (override via environment) ---
DB_PATH="${DB_PATH:-/DATA/AppData/portfolio-tracker/portfolio.db}"
BACKUP_DIR="${BACKUP_DIR:-/DATA/AppData/portfolio-tracker/backups}"
# Set NEXTCLOUD_DIR to a folder Nextcloud syncs, to get the DB off the Pi.
# Leave empty to skip the off-device copy.
NEXTCLOUD_DIR="${NEXTCLOUD_DIR:-}"
KEEP="${KEEP:-7}"   # number of local snapshots to retain

if [ ! -f "$DB_PATH" ]; then
  echo "ERROR: DB not found at $DB_PATH" >&2
  exit 1
fi

mkdir -p "$BACKUP_DIR"
stamp="$(date +%Y-%m-%d_%H%M%S)"
dest="$BACKUP_DIR/portfolio-$stamp.db"

# Prefer sqlite3's online .backup (consistent even while the app is writing);
# fall back to cp if sqlite3 isn't installed (sudo apt install sqlite3).
if command -v sqlite3 >/dev/null 2>&1; then
  sqlite3 "$DB_PATH" ".backup '$dest'"
else
  cp "$DB_PATH" "$dest"
fi
echo "[$(date '+%F %T')] backup written: $dest ($(du -h "$dest" | cut -f1))"

# Off-device copy (e.g. Nextcloud), if configured and present.
if [ -n "$NEXTCLOUD_DIR" ] && [ -d "$NEXTCLOUD_DIR" ]; then
  cp "$dest" "$NEXTCLOUD_DIR/portfolio-latest.db"
  echo "[$(date '+%F %T')] copied off-device: $NEXTCLOUD_DIR/portfolio-latest.db"
fi

# Retention: keep the newest $KEEP snapshots, delete the rest.
ls -1t "$BACKUP_DIR"/portfolio-*.db 2>/dev/null | tail -n +"$((KEEP + 1))" | xargs -r rm -f
echo "[$(date '+%F %T')] retained newest $KEEP snapshots in $BACKUP_DIR"
