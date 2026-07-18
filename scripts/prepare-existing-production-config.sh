#!/usr/bin/env bash
set -Eeuo pipefail

: "${BACKEND_ENV_PATH:?Set BACKEND_ENV_PATH to the existing backend .env}"
: "${POSTGRES_ENV_PATH:?Set POSTGRES_ENV_PATH to the existing PostgreSQL .env}"
: "${BACKEND_IMAGE:?Set BACKEND_IMAGE to the exact staged image tag or digest}"
: "${PUBLIC_HOST:?Set PUBLIC_HOST to the public hostname without a scheme}"

if [[ ! -f "${BACKEND_ENV_PATH}" || ! -f "${POSTGRES_ENV_PATH}" ]]; then
  echo "Existing backend and PostgreSQL env files must be regular files." >&2
  exit 2
fi
if [[ "${PUBLIC_HOST}" == *"://"* || "${PUBLIC_HOST}" == */* || "${PUBLIC_HOST}" == *"*"* ]]; then
  echo "PUBLIC_HOST must be one explicit hostname." >&2
  exit 2
fi
if [[ "${BACKEND_IMAGE}" != *@sha256:* && "${ALLOW_LOCAL_IMAGE_TAG:-NO}" != "YES" ]]; then
  echo "BACKEND_IMAGE must be immutable by digest; set ALLOW_LOCAL_IMAGE_TAG=YES only for a host-local release image." >&2
  exit 2
fi

env_value() {
  local key="$1"
  local path="$2"
  sed -n "s/^${key}=//p" "${path}" | tail -n 1
}

required_value() {
  local key="$1"
  local path="$2"
  local value
  value="$(env_value "${key}" "${path}")"
  if [[ -z "${value}" ]]; then
    echo "Required value ${key} is missing from ${path}." >&2
    exit 2
  fi
  printf '%s' "${value}"
}

backend_dir="$(dirname "${BACKEND_ENV_PATH}")"
postgres_dir="$(dirname "${POSTGRES_ENV_PATH}")"

app_name="$(env_value APP_NAME "${BACKEND_ENV_PATH}")"
app_name="${app_name:-RaceFrame}"
r2_account_id="$(required_value CLOUDFLARE_R2_ACCOUNT_ID "${BACKEND_ENV_PATH}")"
r2_bucket_name="$(required_value CLOUDFLARE_R2_BUCKET_NAME "${BACKEND_ENV_PATH}")"
r2_endpoint="$(required_value CLOUDFLARE_R2_ENDPOINT "${BACKEND_ENV_PATH}")"
r2_access_key_id="$(required_value CLOUDFLARE_R2_ACCESS_KEY_ID "${BACKEND_ENV_PATH}")"
r2_secret_access_key="$(required_value CLOUDFLARE_R2_SECRET_ACCESS_KEY "${BACKEND_ENV_PATH}")"
worker_api_token="$(required_value WORKER_API_TOKEN "${BACKEND_ENV_PATH}")"

postgres_db="$(required_value POSTGRES_DB "${POSTGRES_ENV_PATH}")"
postgres_user="$(required_value POSTGRES_USER "${POSTGRES_ENV_PATH}")"
postgres_password="$(required_value POSTGRES_PASSWORD "${POSTGRES_ENV_PATH}")"

raceframe_owner_password="$(openssl rand -hex 32)"
raceframe_app_password="$(openssl rand -hex 32)"
raceframe_secret_key="$(openssl rand -hex 48)"
metrics_api_token="$(openssl rand -hex 48)"

backend_next="${BACKEND_ENV_PATH}.next"
migration_next="${backend_dir}/.migration.env.next"
postgres_next="${POSTGRES_ENV_PATH}.next"
umask 077

printf '%s\n' \
  "RACEFRAME_BACKEND_IMAGE=${BACKEND_IMAGE}" \
  "APP_ENV=production" \
  "APP_NAME=${app_name}" \
  "RACEFRAME_SECRET_KEY=${raceframe_secret_key}" \
  "ALLOWED_HOSTS=${PUBLIC_HOST},127.0.0.1,localhost" \
  "PUBLIC_ORIGINS=https://${PUBLIC_HOST}" \
  "TRUST_PROXY_HEADERS=true" \
  "DATABASE_URL=postgresql+psycopg://raceframe_app:${raceframe_app_password}@raceframe-postgres:5432/${postgres_db}" \
  "CLOUDFLARE_R2_ACCOUNT_ID=${r2_account_id}" \
  "CLOUDFLARE_R2_BUCKET_NAME=${r2_bucket_name}" \
  "CLOUDFLARE_R2_ENDPOINT=${r2_endpoint}" \
  "CLOUDFLARE_R2_ACCESS_KEY_ID=${r2_access_key_id}" \
  "CLOUDFLARE_R2_SECRET_ACCESS_KEY=${r2_secret_access_key}" \
  "CLOUDFLARE_R2_PRESIGNED_URL_TTL_SECONDS=900" \
  "WORKER_API_TOKEN=${worker_api_token}" \
  "METRICS_API_TOKEN=${metrics_api_token}" \
  "WORKER_LEASE_SECONDS=300" \
  "WORKER_MAX_ATTEMPTS=5" \
  "WORKER_RETRY_BASE_SECONDS=10" \
  "WORKER_RETRY_MAX_SECONDS=900" \
  "WORKER_HEARTBEAT_STALE_SECONDS=120" \
  "WORKER_HEARTBEAT_RETENTION_DAYS=30" \
  "MAX_PHOTO_UPLOAD_BYTES=26214400" \
  "MAX_SELFIE_UPLOAD_BYTES=10485760" \
  "MAX_PARTICIPANT_UPLOAD_BYTES=5242880" \
  "MAX_PHOTO_BATCH_FILES=50" \
  "MAX_SELFIE_BATCH_FILES=5" \
  "MAX_PHOTO_REQUEST_BYTES=268435456" \
  "MAX_FORM_REQUEST_BYTES=2097152" \
  "MAX_IMAGE_PIXELS=40000000" \
  "MAX_IMAGE_DIMENSION=12000" \
  "MAX_PARTICIPANT_ROWS=10000" \
  "MAX_PARTICIPANT_COLUMNS=32" \
  "MAX_PARTICIPANT_CELL_CHARS=512" \
  "MAX_SPREADSHEET_UNCOMPRESSED_BYTES=52428800" \
  "MAX_FACE_SEARCH_BACKLOG=100" \
  "MAX_PHOTO_JOB_BACKLOG=10000" \
  "MAX_SEARCH_RESULTS=250" \
  "MAX_SEARCH_FACES_PER_EVENT=25000" \
  "SEARCH_CAPABILITY_TTL_SECONDS=3600" \
  "BIOMETRIC_RETENTION_HOURS=24" \
  "RAW_RESPONSE_RETENTION_HOURS=24" \
  "DELETION_RETRY_BATCH_SIZE=100" \
  "DELETION_TASK_RETENTION_DAYS=7" \
  "PHOTO_THUMBNAIL_MAX_EDGE=640" \
  "PHOTO_THUMBNAIL_QUALITY=72" \
  "FACE_MATCH_SIMILARITY_THRESHOLD=0.45" \
  "FACE_CANDIDATE_SIMILARITY_THRESHOLD=0.36" \
  "FACE_MEDIUM_SIMILARITY_THRESHOLD=0.42" \
  "FACE_STRONG_SIMILARITY_THRESHOLD=0.50" \
  "FACE_REINFORCEMENT_SIMILARITY_THRESHOLD=0.48" \
  "FACE_REINFORCEMENT_MAX_EMBEDDINGS=12" \
  "BIB_ONLY_SEED_PHOTO_LIMIT=10" \
  "BIB_ONLY_SEED_CLUSTER_MIN_PHOTOS=2" \
  "BIB_ONLY_SEED_CLUSTER_MAJORITY_RATIO=0.60" \
  "BIB_ONLY_SEED_CLUSTER_LEAD_RATIO=1.50" \
  >"${backend_next}"

printf '%s\n' \
  "DATABASE_URL=postgresql+psycopg://raceframe_owner:${raceframe_owner_password}@raceframe-postgres:5432/${postgres_db}" \
  >"${migration_next}"

printf '%s\n' \
  "POSTGRES_DB=${postgres_db}" \
  "POSTGRES_USER=${postgres_user}" \
  "POSTGRES_PASSWORD=${postgres_password}" \
  "RACEFRAME_OWNER_PASSWORD=${raceframe_owner_password}" \
  "RACEFRAME_APP_PASSWORD=${raceframe_app_password}" \
  >"${postgres_next}"

chmod 0600 "${backend_next}" "${migration_next}" "${postgres_next}"
echo "Staged production env files with mode 0600; secret values were not printed."
