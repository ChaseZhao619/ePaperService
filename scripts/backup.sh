#!/usr/bin/env bash
set -euo pipefail

DATA_DIR="${EPAPER_DATA_DIR:-/var/lib/epaper-service}"
BACKUP_DIR="${EPAPER_BACKUP_DIR:-/var/backups/epaper-service}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
DEST="${BACKUP_DIR}/epaper-${STAMP}"

mkdir -p "${DEST}"

if [ -f "${DATA_DIR}/epaper.db" ]; then
  cp "${DATA_DIR}/epaper.db" "${DEST}/epaper.db"
fi

if [ -d "${DATA_DIR}/images" ]; then
  tar -C "${DATA_DIR}" -czf "${DEST}/images.tar.gz" images
fi

tar -C "${BACKUP_DIR}" -czf "${DEST}.tar.gz" "epaper-${STAMP}"
rm -rf "${DEST}"

echo "${DEST}.tar.gz"
