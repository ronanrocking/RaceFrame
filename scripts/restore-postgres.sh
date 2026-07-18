#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

: "${BACKUP_FILE:?Set BACKUP_FILE to an encrypted .dump.age file}"
: "${AGE_IDENTITY_FILE:?Set AGE_IDENTITY_FILE to the restore private-key file}"
: "${RESTORE_DATABASE:?Set RESTORE_DATABASE; use a new name ending in _restore_test for routine tests}"

POSTGRES_CONTAINER="${POSTGRES_CONTAINER:-raceframe-postgres}"

if [[ ! "${RESTORE_DATABASE}" =~ ^[a-zA-Z_][a-zA-Z0-9_]{0,62}$ ]]; then
  echo "RESTORE_DATABASE is not a safe PostgreSQL identifier" >&2
  exit 2
fi

if [[ "${RESTORE_DATABASE}" != *_restore_test && "${ALLOW_PRODUCTION_RESTORE:-NO}" != "YES" ]]; then
  echo "Routine restores must target a new database ending in _restore_test." >&2
  echo "A production restore additionally requires ALLOW_PRODUCTION_RESTORE=YES and the runbook." >&2
  exit 2
fi

for command_name in docker age; do
  command -v "${command_name}" >/dev/null 2>&1 || {
    echo "Required command is missing: ${command_name}" >&2
    exit 2
  }
done

test -r "${BACKUP_FILE}"
test -r "${AGE_IDENTITY_FILE}"

database_exists="$({
  docker exec "${POSTGRES_CONTAINER}" sh -ceu '
    export PGPASSWORD="${POSTGRES_PASSWORD}"
    exec psql --username "${POSTGRES_USER}" --dbname postgres --tuples-only --no-align \
      --set target_db="$1" \
      --command "SELECT 1 FROM pg_database WHERE datname = :'\''target_db'\''"
  ' -- "${RESTORE_DATABASE}"
} | tr -d '[:space:]')"

if [[ "${database_exists}" == "1" ]]; then
  echo "Refusing to overwrite existing database: ${RESTORE_DATABASE}" >&2
  exit 2
fi

docker exec "${POSTGRES_CONTAINER}" sh -ceu '
  export PGPASSWORD="${POSTGRES_PASSWORD}"
  exec createdb --username "${POSTGRES_USER}" --encoding UTF8 --template template0 "$1"
' -- "${RESTORE_DATABASE}"

if ! age --decrypt --identity "${AGE_IDENTITY_FILE}" "${BACKUP_FILE}" | \
  docker exec -i "${POSTGRES_CONTAINER}" sh -ceu '
    export PGPASSWORD="${POSTGRES_PASSWORD}"
    exec pg_restore --username "${POSTGRES_USER}" --dbname "$1" \
      --exit-on-error --no-owner --no-privileges
  ' -- "${RESTORE_DATABASE}"; then
  echo "Restore failed. The new database was left in place for investigation: ${RESTORE_DATABASE}" >&2
  exit 1
fi

docker exec "${POSTGRES_CONTAINER}" sh -ceu '
  export PGPASSWORD="${POSTGRES_PASSWORD}"
  psql --username "${POSTGRES_USER}" --dbname "$1" --set ON_ERROR_STOP=1 \
    --command "ANALYZE" \
    --command "SELECT count(*) AS application_tables FROM information_schema.tables WHERE table_schema = '\''public'\'' AND table_type = '\''BASE TABLE'\''" \
    --command "SELECT version_num FROM alembic_version"
' -- "${RESTORE_DATABASE}"

printf 'Restore completed into isolated database: %s\n' "${RESTORE_DATABASE}"
printf 'Do not treat this as verified until application smoke checks and row/object reconciliation pass.\n'
