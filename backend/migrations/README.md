# RaceFrame database migrations

Alembic is the only supported production schema-change mechanism. Run it with
the migration-owner database URL, never the least-privilege application URL.

Existing databases created by `Base.metadata.create_all()` must first be
verified against revision `20260718_0001`, stamped at that revision, and then
upgraded. Do not run the initial migration against a populated, unstamped
database. See `docs/operations/release-and-rollback.md` for the exact preflight,
backup, stamp, upgrade, and rollback procedure.
