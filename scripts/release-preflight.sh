#!/usr/bin/env bash
set -Eeuo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "${repo_root}"

required_commands=(git docker)
for command_name in "${required_commands[@]}"; do
  command -v "${command_name}" >/dev/null 2>&1 || {
    echo "Required command is missing: ${command_name}" >&2
    exit 2
  }
done

if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "Refusing a release from a dirty working tree" >&2
  exit 1
fi

if [[ -n "$(git ls-files --others --exclude-standard)" ]]; then
  echo "Refusing a release with untracked files" >&2
  exit 1
fi

commit="$(git rev-parse --verify HEAD)"
build_date="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

docker build \
  --build-arg "VCS_REF=${commit}" \
  --build-arg "BUILD_DATE=${build_date}" \
  --tag "raceframe-backend:preflight-${commit}" \
  backend

docker build \
  --build-arg "VCS_REF=${commit}" \
  --build-arg "BUILD_DATE=${build_date}" \
  --tag "raceframe-worker:preflight-${commit}" \
  raceframe-worker

printf 'Preflight images built from commit %s\n' "${commit}"
printf 'Run CI and the release workflow; do not deploy these workstation-local tags.\n'
