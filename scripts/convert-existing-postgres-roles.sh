#!/usr/bin/env bash
set -Eeuo pipefail

: "${POSTGRES_ENV_PATH:?Set POSTGRES_ENV_PATH to the staged PostgreSQL env file}"
: "${POSTGRES_CONTAINER:?Set POSTGRES_CONTAINER to the exact container name}"
: "${CONFIRM_DATABASE:?Set CONFIRM_DATABASE to the exact dedicated database name}"

if [[ ! -f "${POSTGRES_ENV_PATH}" ]]; then
  echo "The staged PostgreSQL env file is missing." >&2
  exit 2
fi

env_value() {
  local key="$1"
  sed -n "s/^${key}=//p" "${POSTGRES_ENV_PATH}" | tail -n 1
}

postgres_db="$(env_value POSTGRES_DB)"
postgres_user="$(env_value POSTGRES_USER)"
owner_password="$(env_value RACEFRAME_OWNER_PASSWORD)"
app_password="$(env_value RACEFRAME_APP_PASSWORD)"

if [[ "${postgres_db}" != "${CONFIRM_DATABASE}" ]]; then
  echo "CONFIRM_DATABASE does not match POSTGRES_DB." >&2
  exit 2
fi
if [[ ! "${postgres_db}" =~ ^[A-Za-z_][A-Za-z0-9_]*$ || ! "${postgres_user}" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
  echo "Database and bootstrap-role names must be simple PostgreSQL identifiers." >&2
  exit 2
fi
if [[ ! "${owner_password}" =~ ^[0-9a-f]{64}$ || ! "${app_password}" =~ ^[0-9a-f]{64}$ ]]; then
  echo "Generated owner and application passwords are missing or malformed." >&2
  exit 2
fi
if [[ "${postgres_user}" == raceframe_owner || "${postgres_user}" == raceframe_app ]]; then
  echo "The bootstrap role must be distinct from the owner and app roles." >&2
  exit 2
fi

{
  printf "SELECT format('CREATE ROLE raceframe_owner LOGIN PASSWORD %%L', '%s') WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'raceframe_owner');\n" "${owner_password}"
  printf '\\gexec\n'
  printf "SELECT format('CREATE ROLE raceframe_app LOGIN PASSWORD %%L', '%s') WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'raceframe_app');\n" "${app_password}"
  printf '\\gexec\n'
  printf "SELECT format('ALTER ROLE raceframe_owner PASSWORD %%L', '%s');\n" "${owner_password}"
  printf '\\gexec\n'
  printf "SELECT format('ALTER ROLE raceframe_app PASSWORD %%L', '%s');\n" "${app_password}"
  printf '\\gexec\n'
  printf '%s\n' \
    'ALTER ROLE raceframe_owner NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;' \
    'ALTER ROLE raceframe_app NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;'
  # ALTER TABLE ownership also transfers its indexes and owned sequences. Do
  # not alter linked sequences directly: PostgreSQL rejects that operation.
  printf '%s\n' "SELECT format('ALTER TABLE %I.%I OWNER TO raceframe_owner', n.nspname, c.relname) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'v', 'm', 'f') AND pg_get_userbyid(c.relowner) = '${postgres_user}';"
  printf '\\gexec\n'
  printf '%s\n' \
    "ALTER DATABASE \"${postgres_db}\" OWNER TO raceframe_owner;" \
    'ALTER SCHEMA public OWNER TO raceframe_owner;' \
    'REVOKE CREATE ON SCHEMA public FROM PUBLIC;' \
    "GRANT CONNECT ON DATABASE \"${postgres_db}\" TO raceframe_owner, raceframe_app;" \
    'GRANT USAGE, CREATE ON SCHEMA public TO raceframe_owner;' \
    'GRANT USAGE ON SCHEMA public TO raceframe_app;' \
    'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO raceframe_app;' \
    'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO raceframe_app;' \
    'ALTER DEFAULT PRIVILEGES FOR ROLE raceframe_owner IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO raceframe_app;' \
    'ALTER DEFAULT PRIVILEGES FOR ROLE raceframe_owner IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO raceframe_app;'
} | docker exec -i "${POSTGRES_CONTAINER}" psql \
  --set ON_ERROR_STOP=1 \
  --username "${postgres_user}" \
  --dbname "${postgres_db}" \
  >/dev/null

echo "Converted database objects to least-privilege owner/app roles; secret values were not printed."
