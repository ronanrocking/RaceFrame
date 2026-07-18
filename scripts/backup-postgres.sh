#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

: "${AGE_RECIPIENT:?Set AGE_RECIPIENT to the offline backup public key}"
: "${RCLONE_REMOTE:?Set RCLONE_REMOTE to an off-host destination such as remote:raceframe/postgres}"

POSTGRES_CONTAINER="${POSTGRES_CONTAINER:-raceframe-postgres}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/raceframe}"

case "${BACKUP_DIR}" in
  /*) ;;
  *) echo "BACKUP_DIR must be an absolute path" >&2; exit 2 ;;
esac

if [[ "${BACKUP_DIR}" == "/" ]]; then
  echo "Refusing to use / as BACKUP_DIR" >&2
  exit 2
fi

for command_name in docker age rclone sha256sum; do
  command -v "${command_name}" >/dev/null 2>&1 || {
    echo "Required command is missing: ${command_name}" >&2
    exit 2
  }
done

install -d -m 0700 "${BACKUP_DIR}"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
prefix="raceframe-${timestamp}"
database_partial="${BACKUP_DIR}/${prefix}.dump.age.partial"
globals_partial="${BACKUP_DIR}/${prefix}.globals.sql.age.partial"
database_final="${BACKUP_DIR}/${prefix}.dump.age"
globals_final="${BACKUP_DIR}/${prefix}.globals.sql.age"
checksums_final="${BACKUP_DIR}/${prefix}.sha256"

cleanup() {
  rm -f -- "${database_partial}" "${globals_partial}"
}
trap cleanup EXIT

docker exec "${POSTGRES_CONTAINER}" sh -ceu '
  export PGPASSWORD="${POSTGRES_PASSWORD}"
  exec pg_dump --username "${POSTGRES_USER}" --dbname "${POSTGRES_DB}" \
    --format=custom --compress=6 --no-owner --no-privileges
' | age --recipient "${AGE_RECIPIENT}" --output "${database_partial}"

docker exec "${POSTGRES_CONTAINER}" sh -ceu '
  export PGPASSWORD="${POSTGRES_PASSWORD}"
  exec pg_dumpall --username "${POSTGRES_USER}" --globals-only
' | age --recipient "${AGE_RECIPIENT}" --output "${globals_partial}"

test -s "${database_partial}"
test -s "${globals_partial}"
mv -- "${database_partial}" "${database_final}"
mv -- "${globals_partial}" "${globals_final}"
sha256sum "${database_final}" "${globals_final}" >"${checksums_final}"

rclone copyto --immutable "${database_final}" "${RCLONE_REMOTE}/${prefix}.dump.age"
rclone copyto --immutable "${globals_final}" "${RCLONE_REMOTE}/${prefix}.globals.sql.age"
rclone copyto --immutable "${checksums_final}" "${RCLONE_REMOTE}/${prefix}.sha256"

printf 'Backup completed: %s\n' "${prefix}"
printf 'Local files: %s, %s, %s\n' "${database_final}" "${globals_final}" "${checksums_final}"
printf 'Off-host destination: %s/%s.*\n' "${RCLONE_REMOTE}" "${prefix}"
