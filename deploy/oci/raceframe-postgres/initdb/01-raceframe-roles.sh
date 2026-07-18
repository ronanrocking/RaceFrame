#!/usr/bin/env bash
set -Eeuo pipefail

: "${POSTGRES_USER:?POSTGRES_USER is required}"
: "${POSTGRES_DB:?POSTGRES_DB is required}"
: "${RACEFRAME_OWNER_PASSWORD:?RACEFRAME_OWNER_PASSWORD is required}"
: "${RACEFRAME_APP_PASSWORD:?RACEFRAME_APP_PASSWORD is required}"

psql --set ON_ERROR_STOP=1 \
  --username "${POSTGRES_USER}" \
  --dbname "${POSTGRES_DB}" \
  --set db_name="${POSTGRES_DB}" \
  --set owner_password="${RACEFRAME_OWNER_PASSWORD}" \
  --set app_password="${RACEFRAME_APP_PASSWORD}" <<'EOSQL'
SELECT format('CREATE ROLE raceframe_owner LOGIN PASSWORD %L', :'owner_password')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'raceframe_owner')\gexec

SELECT format('CREATE ROLE raceframe_app LOGIN PASSWORD %L', :'app_password')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'raceframe_app')\gexec

ALTER ROLE raceframe_owner NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
ALTER ROLE raceframe_app NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;

GRANT CONNECT ON DATABASE :"db_name" TO raceframe_owner, raceframe_app;
GRANT USAGE, CREATE ON SCHEMA public TO raceframe_owner;
GRANT USAGE ON SCHEMA public TO raceframe_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO raceframe_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO raceframe_app;

ALTER DEFAULT PRIVILEGES FOR ROLE raceframe_owner IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO raceframe_app;
ALTER DEFAULT PRIVILEGES FOR ROLE raceframe_owner IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO raceframe_app;
EOSQL
