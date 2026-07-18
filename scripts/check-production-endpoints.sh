#!/usr/bin/env bash
set -Eeuo pipefail

: "${BACKEND_ENV_PATH:?Set BACKEND_ENV_PATH to the production backend .env}"
BASE_URL="${BASE_URL:-http://127.0.0.1:8008}"

metrics_token="$(sed -n 's/^METRICS_API_TOKEN=//p' "${BACKEND_ENV_PATH}" | tail -n 1)"
if [[ ! "${metrics_token}" =~ ^[0-9A-Za-z_-]{32,}$ ]]; then
  echo "METRICS_API_TOKEN is missing or malformed." >&2
  exit 2
fi

curl --fail --silent --show-error "${BASE_URL}/livez" >/dev/null
curl --fail --silent --show-error "${BASE_URL}/readyz" >/dev/null

unauthorized_status="$(curl --silent --output /dev/null --write-out '%{http_code}' "${BASE_URL}/internal/metrics")"
if [[ "${unauthorized_status}" != "401" ]]; then
  echo "Expected unauthenticated metrics to return 401, got ${unauthorized_status}." >&2
  exit 1
fi

metrics_file="$(mktemp)"
trap 'rm -f "${metrics_file}"' EXIT
curl --fail --silent --show-error \
  --header "Authorization: Bearer ${metrics_token}" \
  "${BASE_URL}/internal/metrics" \
  >"${metrics_file}"
grep -q '^raceframe_jobs' "${metrics_file}"
grep -q '^raceframe_workers' "${metrics_file}"

echo "Liveness, readiness, metrics denial, and authenticated metrics checks passed."
