# Release, migration, and rollback

RaceFrame releases are immutable image digests plus an Alembic schema head.
Application startup never creates or migrates production tables.

## Build and promote

1. Start from a reviewed, clean commit. Run `scripts/release-preflight.sh` on a
   Linux builder and require all CI/CodeQL jobs to pass.
2. Push a signed/versioned `v*` tag. The release workflow publishes commit-tagged
   backend/worker images and a `release-manifest.txt` containing their digests.
3. Compare the manifest source commit with the reviewed commit. Copy full
   `ghcr.io/...@sha256:...` references into the host `.env` files.
4. Pull by digest before the maintenance window; never deploy `latest` or a
   mutable tag. Retain the prior known-good digest and configuration snapshot.

## Rehearse an existing database upgrade

A legacy database created by SQLAlchemy `create_all()` has no Alembic revision.
Never stamp it based only on its age or application version.

1. Take a new encrypted backup, copy it off-host, verify its checksum, and
   restore it into an isolated `*_restore_test` database.
2. Compare table/column/index inventory with revision `20260718_0001`. The most
   reliable acceptance test is running the complete procedure below on the
   restored clone, then exercising application smoke tests and reconciliation.
3. As the bootstrap administrator on the clone, run the checked-in role-init
   script. Stamp the clone only after confirming it is the legacy baseline:

   ```bash
   cd backend
   DATABASE_URL='postgresql+psycopg://raceframe_admin:...@host/db_restore_test' \
     alembic stamp 20260718_0001
   ```

4. Existing objects are owned by the old bootstrap role, so the new migration
   role cannot alter them merely through grants. Inspect every object owned by
   that role, then use the checked-in targeted conversion script against this
   dedicated RaceFrame database:

   ```bash
   POSTGRES_ENV_PATH="$PWD/.env.next" \
   POSTGRES_CONTAINER=raceframe-postgres \
   CONFIRM_DATABASE=raceframe \
   scripts/convert-existing-postgres-roles.sh
   ```

   The script transfers only application relations in `public`, the `public`
   schema, and the explicitly confirmed database. Do not replace it with a
   broad `REASSIGN OWNED`: an init/bootstrap superuser commonly owns template
   or other system-required objects, and PostgreSQL correctly refuses or
   over-broadens that operation. The conversion also grants current and future
   CRUD/sequence access to `raceframe_app` and revokes public schema creation.
5. Run `alembic upgrade head` with `raceframe_owner`. Confirm `alembic current`,
   run `/readyz`, normal organizer/user/worker smoke tests, and dry-run storage
   reconciliation. Test the application DSN has CRUD access but cannot create
   schema objects.
6. Record elapsed time, locks, row preflight failures, and exact rollback steps.
   Correct bad legacy data explicitly; never weaken a constraint merely to make
   the migration pass.

## Production deployment

1. Announce the window. Confirm current digest/revision, free DB/disk space,
   active sessions, queue depth, and a recent successful worker heartbeat.
2. Produce and verify a fresh backup. Confirm the off-host copy exists.
3. Stop/drain the old worker. Wait for current work or allow leases to expire;
   do not leave an old protocol worker polling the hardened backend.
4. On the first Alembic adoption only, repeat the rehearsed role transfer and
   `alembic stamp 20260718_0001` with the exact production backup available.
   Replace any prerelease `WORKER_JOB_LEASE_SECONDS`,
   `WORKER_JOB_RETRY_BASE_SECONDS`, or `WORKER_JOB_RETRY_MAX_SECONDS` entries
   with `WORKER_LEASE_SECONDS`, `WORKER_RETRY_BASE_SECONDS`, and
   `WORKER_RETRY_MAX_SECONDS`; set `WORKER_MAX_ATTEMPTS` explicitly.
5. Set the new backend digest, pull it, then run the one-shot migration using
   the schema-owner DSN:

   ```bash
   cd ~/raceframe/raceframe-backend
   test -r .migration.env && test "$(stat -c %a .migration.env)" = 600
   docker login ghcr.io
   docker compose pull app migrate
   docker compose --profile migration run --rm migrate
   docker compose up -d app
   curl --fail --silent http://127.0.0.1:8008/readyz
   ```

6. Set/pull the matching worker digest and start it. Verify its version and a
   fresh heartbeat in `/internal/metrics`, then exercise one bounded job of each
   applicable type.
7. Verify public search, authorized organizer access, unauthorized organizer
   denial, upload limits, queue age, 5xx logs, R2 reads/writes, and maintenance.
   Observe through at least one normal maintenance interval before closing.

## Application rollback

If code is unhealthy but the schema remains forward-compatible, set the prior
backend and worker digests, pull, and restart in protocol order. Record the
reason and keep the failed digest for investigation.

Database downgrade is a separate destructive decision. Revision `0002`
contains a downgrade for rehearsal, but it removes hardening columns/tables and
their data. Do not run it simply because application rollback is needed. Prefer
a forward fix or compatible prior image. If downgrade is unavoidable:

- stop all writers/workers and preserve logs;
- verify an immediate backup and its off-host checksum;
- prove the downgrade and old application against the newest isolated restore;
- obtain the incident owner's explicit approval;
- run `alembic downgrade 20260718_0001` only with the owner DSN, then validate
  row counts, object references, permissions, and the old application.

For corruption or an irreversible migration, restore into a new database and
switch only after full validation. Never overwrite the existing database in
place during the first recovery attempt.
