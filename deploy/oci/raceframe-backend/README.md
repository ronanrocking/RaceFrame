# RaceFrame backend deployment

This Compose project runs a digest-pinned, non-root backend on
`127.0.0.1:8008`. It expects the external `raceframe-internal` network and a
separately managed PostgreSQL service.

Use two mode-`0600` files:

- `.env`, copied from `.env.example`, contains only the least-privilege
  application DSN and normal runtime configuration;
- `.migration.env`, copied from `.migration.env.example`, contains the
  schema-owner `DATABASE_URL` and is loaded only by the migration profile.

```bash
cp .env.example .env
cp .migration.env.example .migration.env
chmod 0600 .env .migration.env
docker compose config --quiet
docker compose pull
docker compose --profile migration run --rm migrate
docker compose up -d app
curl --fail http://127.0.0.1:8008/readyz
```

Do not build on the production host or use a mutable tag. Copy image digests
from the reviewed release manifest. Existing databases require the rehearsal,
ownership transfer, backup, and stamping procedure in
[`docs/operations/release-and-rollback.md`](../../../docs/operations/release-and-rollback.md).

Lifecycle jobs use `docker compose --profile maintenance run --rm maintenance`.
See the [production runbook](../../../docs/operations/production-runbook.md) for
edge identity, monitoring, timers, and incident procedures.
