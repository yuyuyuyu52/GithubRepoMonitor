#!/bin/bash
# SQLite online backup with gzip + pruning.
#
# Reads:
#   MONITOR_DB_PATH           default /var/lib/monitor/monitor.db
#   MONITOR_BACKUP_DIR        default /var/lib/monitor/backups
#   MONITOR_BACKUP_KEEP_DAYS  default 14
#
# Exits non-zero on failure so systemd surfaces it.
set -euo pipefail

DB_PATH="${MONITOR_DB_PATH:-/var/lib/monitor/monitor.db}"
BACKUP_DIR="${MONITOR_BACKUP_DIR:-/var/lib/monitor/backups}"
KEEP_DAYS="${MONITOR_BACKUP_KEEP_DAYS:-14}"

if [ ! -f "$DB_PATH" ]; then
  echo "backup.db_missing path=$DB_PATH" >&2
  exit 1
fi

mkdir -p "$BACKUP_DIR"

ts="$(date -u +%Y%m%dT%H%M%SZ)"
target="$BACKUP_DIR/monitor-$ts.db"

# sqlite3 .backup uses the online backup API — safe while the daemon
# holds connections open. Requires the sqlite3 CLI (apt: sqlite3).
sqlite3 "$DB_PATH" ".backup '$target'"
gzip -f "$target"

find "$BACKUP_DIR" -maxdepth 1 -type f -name 'monitor-*.db.gz' \
  -mtime +"$KEEP_DAYS" -print -delete

echo "backup.ok file=$target.gz kept_days=$KEEP_DAYS"
