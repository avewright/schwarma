#!/usr/bin/env bash
# deploy/backup.sh — Automated PostgreSQL backup for Schwarma Hub
#
# Usage:
#   ./deploy/backup.sh                # interactive / cron
#   BACKUP_DIR=/mnt/backups ./deploy/backup.sh
#
# Environment variables:
#   PGHOST       — PostgreSQL host      (default: localhost)
#   PGPORT       — PostgreSQL port      (default: 5432)
#   PGUSER       — PostgreSQL user      (default: schwarma)
#   PGDATABASE   — database name        (default: schwarma)
#   PGPASSWORD   — password (or use .pgpass / PGPASSFILE)
#   BACKUP_DIR   — where to store dumps (default: ./backups)
#   RETENTION_DAYS — delete backups older than N days (default: 30)
#   BACKUP_FORMAT — pg_dump format: custom|plain|directory (default: custom)
#
# Cron example (daily at 02:00):
#   0 2 * * * PGPASSWORD=secret /opt/schwarma/deploy/backup.sh >> /var/log/schwarma-backup.log 2>&1

set -euo pipefail

# ── Configuration ────────────────────────────────────────────────────────

PGHOST="${PGHOST:-localhost}"
PGPORT="${PGPORT:-5432}"
PGUSER="${PGUSER:-schwarma}"
PGDATABASE="${PGDATABASE:-schwarma}"
BACKUP_DIR="${BACKUP_DIR:-./backups}"
RETENTION_DAYS="${RETENTION_DAYS:-30}"
BACKUP_FORMAT="${BACKUP_FORMAT:-custom}"

TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
FILENAME="schwarma_${TIMESTAMP}"

case "${BACKUP_FORMAT}" in
    custom)    EXT=".dump" ;;
    plain)     EXT=".sql"  ;;
    directory) EXT=""      ;;
    *)         echo "ERROR: Unknown BACKUP_FORMAT '${BACKUP_FORMAT}'" >&2; exit 1 ;;
esac

BACKUP_PATH="${BACKUP_DIR}/${FILENAME}${EXT}"

# ── Preflight ────────────────────────────────────────────────────────────

mkdir -p "${BACKUP_DIR}"

echo "[$(date -u +%FT%TZ)] Starting backup → ${BACKUP_PATH}"

if ! command -v pg_dump &>/dev/null; then
    echo "ERROR: pg_dump not found. Install postgresql-client." >&2
    exit 1
fi

# ── Dump ─────────────────────────────────────────────────────────────────

pg_dump \
    --host="${PGHOST}" \
    --port="${PGPORT}" \
    --username="${PGUSER}" \
    --dbname="${PGDATABASE}" \
    --format="${BACKUP_FORMAT}" \
    --file="${BACKUP_PATH}" \
    --verbose \
    --no-owner \
    --no-privileges

BACKUP_SIZE="$(du -sh "${BACKUP_PATH}" | cut -f1)"
echo "[$(date -u +%FT%TZ)] Backup complete: ${BACKUP_PATH} (${BACKUP_SIZE})"

# ── Retention ────────────────────────────────────────────────────────────

if [ "${RETENTION_DAYS}" -gt 0 ]; then
    DELETED=$(find "${BACKUP_DIR}" -maxdepth 1 -name "schwarma_*" \
        -mtime "+${RETENTION_DAYS}" -print -delete | wc -l)
    if [ "${DELETED}" -gt 0 ]; then
        echo "[$(date -u +%FT%TZ)] Pruned ${DELETED} backup(s) older than ${RETENTION_DAYS} days"
    fi
fi

echo "[$(date -u +%FT%TZ)] Done."
