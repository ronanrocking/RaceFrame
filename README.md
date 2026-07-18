# RaceFrame

RaceFrame is a race-photo discovery service that combines bib OCR with face
evidence. Organizers create events and rosters, photographers upload race
photos, and participants search published events with a bib/name and optional
temporary selfie evidence.

## Architecture

- `backend/`: FastAPI, server-rendered pages, SQLAlchemy, Alembic, R2 access,
  rate limits, audit logs, retention maintenance, and worker control plane.
- `raceframe-worker/`: serial Google Vision OCR and InsightFace/ONNX processing.
  It has no database or R2 credentials and downloads only signed objects.
- PostgreSQL 16: system of record, durable job leases, rate-limit counters,
  worker heartbeats, object-deletion outbox, and audit events.
- Cloudflare R2: originals, thumbnails, and temporary search images.
- `deploy/`: hardened OCI Compose definitions and systemd timer examples.

The public application exposes `/user`; organizer operations live under
`/admin` and `/upload`; workers use `/internal/worker/*`. `/livez` proves the
process is alive and `/readyz` checks whether it is ready for traffic.

## Security and lifecycle defaults

- Production configuration fails closed when required secrets are absent.
- Upload byte, count, dimension, pixel, spreadsheet, queue, and search-result
  budgets are enforced before expensive work is accepted.
- Public search capabilities are hashed, browser-bound, short-lived, and rate
  limited. Temporary biometric data is deleted by scheduled maintenance.
- Worker claims use expiring leases, attempt IDs, bounded retries, heartbeats,
  idempotent completion, and dead-letter states.
- Privileged actions are recorded in append-only application audit rows.
- Containers run as UID/GID `10001`, with read-only roots, dropped
  capabilities, `no-new-privileges`, resource/PID limits, health checks, and
  bounded Docker logs. PostgreSQL runs separately as UID/GID `999`.
- Python packages and base images are pinned. Docker builds install from
  committed hash locks on Linux x86_64/aarch64.

Edge identity is deliberately not stored in this repository. Before exposing
organizer routes, configure Cloudflare Access (or another trusted identity
proxy) and ensure the origin is reachable only through that proxy. Model
commercial-use licensing, cloud credential rotation, off-host backup storage,
DNS/TLS, and alert destinations remain operator-owned release gates.

## Development

Use Python 3.12 for the backend and Python 3.11 for the worker. Do not put live
secrets in the repository.

```bash
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install --require-hashes -r backend/requirements.lock
python -m pip install -r requirements-dev.txt
export APP_ENV=test
export RACEFRAME_SECRET_KEY=test-only-secret-key-with-at-least-32-bytes
export DATABASE_URL=sqlite:///./backend/raceframe.db
pytest backend/tests
```

The worker lock is Linux-specific because its native ML wheels do not support
Windows. Build and test the worker through its Dockerfile.

```bash
docker build -t raceframe-worker:dev raceframe-worker
```

## Database migrations

Production schema changes run separately from application startup:

```bash
cd backend
DATABASE_URL='postgresql+psycopg://raceframe_owner:...@localhost/raceframe' alembic upgrade head
```

An existing database created by `create_all()` must be inspected and stamped at
revision `20260718_0001` before applying revision `20260718_0002`. Never guess
or stamp without the release runbook and a verified backup.

## Operations

- [Production runbook](docs/operations/production-runbook.md)
- [Configuration reference](docs/operations/configuration.md)
- [Release and rollback](docs/operations/release-and-rollback.md)
- [Backup and restore](docs/operations/backup-and-restore.md)
- [Data lifecycle and reconciliation](docs/operations/data-lifecycle.md)
- [Implementation and architecture guide](docs/implementation-and-architecture-guide-2026-07-19.md)
- [Production audit](docs/production-audit-2026-07-18.md)
- [Production remediation record](docs/production-remediation-2026-07-18.md)

The CI gates run tests, a complete Alembic upgrade/downgrade cycle, dependency
audits, secret/static analysis, Docker builds, container CVE scans, and CodeQL.
Release tags publish commit-addressed GHCR images plus a manifest containing
their immutable digests; production Compose accepts digests only.
