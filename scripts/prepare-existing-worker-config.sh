#!/usr/bin/env bash
set -Eeuo pipefail

: "${WORKER_ENV_PATH:?Set WORKER_ENV_PATH to the existing worker .env}"
: "${WORKER_IMAGE:?Set WORKER_IMAGE to the exact staged image tag or digest}"
: "${WORKER_VERSION:?Set WORKER_VERSION to the release identifier}"

if [[ ! -f "${WORKER_ENV_PATH}" ]]; then
  echo "The existing worker env file is missing." >&2
  exit 2
fi
if [[ "${WORKER_IMAGE}" != *@sha256:* && "${ALLOW_LOCAL_IMAGE_TAG:-NO}" != "YES" ]]; then
  echo "WORKER_IMAGE must be immutable by digest; set ALLOW_LOCAL_IMAGE_TAG=YES only for a host-local release image." >&2
  exit 2
fi

env_value() {
  local key="$1"
  sed -n "s/^${key}=//p" "${WORKER_ENV_PATH}" | tail -n 1
}

required_value() {
  local key="$1"
  local value
  value="$(env_value "${key}")"
  if [[ -z "${value}" ]]; then
    echo "Required value ${key} is missing from ${WORKER_ENV_PATH}." >&2
    exit 2
  fi
  printf '%s' "${value}"
}

backend_base_url="$(required_value BACKEND_BASE_URL)"
worker_api_token="$(required_value WORKER_API_TOKEN)"
google_credentials="$(required_value GOOGLE_APPLICATION_CREDENTIALS)"
face_model_name="$(required_value FACE_MODEL_NAME)"
worker_job_types="$(env_value WORKER_JOB_TYPES)"
worker_job_types="${worker_job_types:-ocr,face_photo_scan}"

worker_next="${WORKER_ENV_PATH}.next"
umask 077
printf '%s\n' \
  "RACEFRAME_WORKER_IMAGE=${WORKER_IMAGE}" \
  "BACKEND_BASE_URL=${backend_base_url}" \
  "WORKER_API_TOKEN=${worker_api_token}" \
  "GOOGLE_APPLICATION_CREDENTIALS=${google_credentials}" \
  "WORKER_POLL_SECONDS=3" \
  "WORKER_JOB_TYPES=${worker_job_types}" \
  "WORKER_ID=raceframe-worker-1" \
  "WORKER_VERSION=${WORKER_VERSION}" \
  "WORKER_HEARTBEAT_SECONDS=30" \
  "WORKER_HTTP_RETRIES=4" \
  "WORKER_HTTP_BACKOFF_SECONDS=0.75" \
  "WORKER_MAX_DOWNLOAD_BYTES=26214400" \
  "WORKER_MAX_IMAGE_PIXELS=40000000" \
  "WORKER_MAX_IMAGE_DIMENSION=12000" \
  "SEARCH_FACE_MIN_EDGE=80" \
  "SEARCH_FACE_MIN_DETECTION_SCORE=0.65" \
  "FACE_MODEL_NAME=${face_model_name}" \
  "FACE_DET_SIZE=640" \
  "FACE_MAX_IMAGE_EDGE=1600" \
  >"${worker_next}"

chmod 0600 "${worker_next}"
echo "Staged worker env with mode 0600; secret values were not printed."
