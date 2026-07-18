# RaceFrame PostgreSQL deployment

This Compose project runs digest-pinned PostgreSQL 16 on
`127.0.0.1:5432`, attached to the external `raceframe-internal` network. The
bootstrap script creates separate administrator, migration-owner, and
least-privilege application roles on a brand-new cluster.

```bash
docker network inspect raceframe-internal >/dev/null 2>&1 || \
  docker network create raceframe-internal
sudo install -d -m 0700 -o 999 -g 999 data
cp .env.example .env
chmod 0600 .env
docker compose config --quiet
docker compose up -d
docker compose logs --tail 100 postgres
```

Initialization scripts run only when `data` is empty. Never remove an existing
data directory to rerun them. Follow the [release and migration
runbook](../../../docs/operations/release-and-rollback.md) to introduce roles
and Alembic safely on a legacy database, and the [backup/restore
runbook](../../../docs/operations/backup-and-restore.md) before production.
