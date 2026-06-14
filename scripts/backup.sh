#!/usr/bin/env bash
# Back up the portfolio SQLite DB: timestamped local snapshot + retention, plus
# an optional off-device upload to Nextcloud (via WebDAV) so a disk failure on
# the Pi doesn't lose everything.
#
# Secrets (Nextcloud app password) are read from the project's .env — never put
# them in the crontab. Run from cron, e.g. nightly at 02:13:
#   13 2 * * *  /home/kshit/Projects/portfolio-tracker/scripts/backup.sh >> /home/kshit/portfolio-backup.log 2>&1
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-$HOME/Projects/portfolio-tracker}"

# Load config + secrets from .env (gitignored, host-only).
if [ -f "$PROJECT_DIR/.env" ]; then
  # shellcheck disable=SC1090,SC1091
  set -a; . "$PROJECT_DIR/.env"; set +a
fi

# --- local backup config (override via environment / .env) ---
DB_PATH="${DB_PATH:-/DATA/AppData/portfolio-tracker/portfolio.db}"
BACKUP_DIR="${BACKUP_DIR:-/DATA/AppData/portfolio-tracker/backups}"
KEEP="${KEEP:-7}"                       # number of local snapshots to retain

# --- optional off-device copy to a plain folder (e.g. an rsync/SMB mount) ---
NEXTCLOUD_DIR="${NEXTCLOUD_DIR:-}"      # leave empty to skip

# --- optional off-device upload to Nextcloud via WebDAV ---
# Set these in .env to enable. NEXTCLOUD_APP_PASSWORD must be a Nextcloud
# *app password* (Settings > Security), NOT your account password.
NEXTCLOUD_URL="${NEXTCLOUD_URL:-}"                 # e.g. http://localhost:10081
NEXTCLOUD_USER="${NEXTCLOUD_USER:-}"
NEXTCLOUD_APP_PASSWORD="${NEXTCLOUD_APP_PASSWORD:-}"
NEXTCLOUD_REMOTE_DIR="${NEXTCLOUD_REMOTE_DIR:-Backups/portfolio}"

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

# Optional: plain copy to a mounted/synced folder.
if [ -n "$NEXTCLOUD_DIR" ] && [ -d "$NEXTCLOUD_DIR" ]; then
  cp "$dest" "$NEXTCLOUD_DIR/portfolio-latest.db"
  echo "[$(date '+%F %T')] copied to folder: $NEXTCLOUD_DIR/portfolio-latest.db"
fi

# Optional: upload to Nextcloud via WebDAV — keeps a DATED rolling history (last
# $KEEP days) plus a convenient "latest" pointer, so it's both off-device and
# version-recoverable, and syncs to any device running the Nextcloud client.
if [ -n "$NEXTCLOUD_URL" ] && [ -n "$NEXTCLOUD_USER" ] && [ -n "$NEXTCLOUD_APP_PASSWORD" ]; then
  base="${NEXTCLOUD_URL%/}/remote.php/dav/files/$NEXTCLOUD_USER"
  cred="$NEXTCLOUD_USER:$NEXTCLOUD_APP_PASSWORD"
  remote="$base/$NEXTCLOUD_REMOTE_DIR"
  # Create the remote directory tree (MKCOL per level; already-exists is fine).
  path=""
  IFS='/' read -ra _parts <<< "$NEXTCLOUD_REMOTE_DIR"
  for p in "${_parts[@]}"; do
    [ -z "$p" ] && continue
    path="${path:+$path/}$p"
    curl -fsS -u "$cred" -X MKCOL "$base/$path" >/dev/null 2>&1 || true
  done
  # Upload today's dated copy + overwrite the "latest" pointer.
  daystamp="$(date +%F)"
  for fname in "portfolio-$daystamp.db" "portfolio-latest.db"; do
    code="$(curl -s -o /dev/null -w '%{http_code}' -u "$cred" -T "$dest" "$remote/$fname" || echo 000)"
    if [ "$code" = "201" ] || [ "$code" = "204" ]; then
      echo "[$(date '+%F %T')] uploaded to Nextcloud: $NEXTCLOUD_REMOTE_DIR/$fname (HTTP $code)"
    else
      echo "[$(date '+%F %T')] WARN: Nextcloud upload of $fname failed (HTTP $code)" >&2
    fi
  done
  # Rolling retention: delete dated copies older than $KEEP days. Sweep a week's
  # worth of dates so stragglers from days the Pi was off still get cleaned up.
  # 404s (already gone / never existed) are ignored. Needs GNU date (-d), present
  # on the Pi (Linux); skipped gracefully if unavailable.
  for d in $(seq "$KEEP" "$((KEEP + 6))"); do
    old="$(date -d "$d days ago" +%F 2>/dev/null || true)"
    [ -z "$old" ] && continue
    curl -s -o /dev/null -u "$cred" -X DELETE "$remote/portfolio-$old.db" >/dev/null 2>&1 || true
  done
  echo "[$(date '+%F %T')] Nextcloud: keeping dated backups for the last $KEEP days"
fi

# Retention: keep the newest $KEEP local snapshots, delete the rest.
ls -1t "$BACKUP_DIR"/portfolio-*.db 2>/dev/null | tail -n +"$((KEEP + 1))" | xargs -r rm -f
echo "[$(date '+%F %T')] retained newest $KEEP snapshots in $BACKUP_DIR"
