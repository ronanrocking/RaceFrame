#!/usr/bin/env bash
set -Eeuo pipefail

: "${BACKEND_ENV_FILE:?Set BACKEND_ENV_FILE to the existing backend .env path}"
: "${RESTORE_DATABASE:?Set RESTORE_DATABASE to an isolated database ending in _restore_test}"
: "${BACKEND_IMAGE:?Set BACKEND_IMAGE to the exact staged image tag or digest}"

if [[ "${RESTORE_DATABASE}" != *_restore_test ]]; then
  echo "RESTORE_DATABASE must end in _restore_test" >&2
  exit 2
fi
if [[ "${CONFIRM_LEGACY_BASELINE:-NO}" != "YES" ]]; then
  echo "Set CONFIRM_LEGACY_BASELINE=YES only after verifying the restored legacy schema and row counts." >&2
  exit 2
fi

legacy_url="$(sed -n 's/^DATABASE_URL=//p' "${BACKEND_ENV_FILE}")"
if [[ -z "${legacy_url}" || "${legacy_url}" != */* ]]; then
  echo "DATABASE_URL is missing or invalid" >&2
  exit 2
fi
clone_url="${legacy_url%/*}/${RESTORE_DATABASE}"

run_alembic() {
  docker run --rm --network raceframe-internal \
    --env "DATABASE_URL=${clone_url}" \
    "${BACKEND_IMAGE}" alembic "$@"
}

run_alembic stamp 20260718_0001
run_alembic upgrade head
run_alembic current
run_alembic check
