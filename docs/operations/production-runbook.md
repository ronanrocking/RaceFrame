# Production runbook

This runbook assumes separate backend/PostgreSQL and ML-worker hosts, immutable
release images, and an external Docker network named `raceframe-internal` on
the backend host. Commands are examples; inspect paths, digests, and current
state before executing them.

## Release gates

Do not expose production until all of these operator-owned controls are true:

- Cloudflare Access (or equivalent identity-aware proxy) protects `/admin` and
  `/upload`, the origin firewall accepts only the tunnel/proxy, TLS is valid,
  and the forwarding-header trust boundary has been tested.
- The selected face model and all production dependencies/assets have approved
  commercial licenses and a recorded privacy/biometric policy.
- Production secrets are unique, stored outside Git, and have an owner and
  rotation procedure. The R2 key is restricted to the production bucket.
- An encrypted off-host database backup and a successful isolated restore test
  exist. R2 retention/versioning and recovery policy are documented separately.
- Prometheus scraping, alert routing, log collection, and an on-call contact are
  live. CI is green for the exact release commit.

## Host preparation

Use explicit project directories for every Compose command. Create the shared
network once on the backend/database host:

```bash
docker network inspect raceframe-internal >/dev/null 2>&1 || \
  docker network create raceframe-internal
```

For a new database host, create the bind mount with the container's explicit
UID/GID before first start:

```bash
sudo install -d -m 0700 -o 999 -g 999 ~/raceframe/raceframe-postgres/data
cp deploy/oci/raceframe-postgres/.env.example \
  ~/raceframe/raceframe-postgres/.env
chmod 0600 ~/raceframe/raceframe-postgres/.env
cd ~/raceframe/raceframe-postgres
```

Populate the file with distinct generated passwords. Review the resolved
configuration before startup:

```bash
docker compose config --quiet
docker compose pull
docker compose up -d
docker compose ps
docker compose logs --tail 100 postgres
```

Prepare the non-root worker's bind mounts before its first start. The Google
credential must be readable by container GID `10001`, but not world-readable:

```bash
cd ~/raceframe/raceframe-worker
sudo install -d -m 0750 -o 10001 -g 10001 model-cache
sudo install -d -m 0750 -o root -g 10001 secrets
sudo install -m 0640 -o root -g 10001 /secure/source/google-vision.json \
  secrets/google-vision.json
```

The R2 credential requires bucket head/list and object get/put/delete for only
the production bucket. List access is required by reconciliation; do not grant
account-wide administration.

The init role script runs only against an empty cluster. For an existing
cluster, do not delete or reinitialize `data`; use the controlled role/schema
conversion in the release runbook.

## Normal health checks

`/livez` answers only process liveness. `/readyz` verifies database access,
current Alembic head, and R2 readiness in production. A load balancer should
route traffic only when `/readyz` succeeds, while Docker restarts only on
liveness failure.

```bash
cd ~/raceframe/raceframe-backend
curl --fail --silent http://127.0.0.1:8008/livez
curl --fail --silent http://127.0.0.1:8008/readyz
docker compose ps
docker stats --no-stream
```

From the backend host, verify database readiness with `pg_isready`; do not put
passwords in the shell history. Verify the public path through Cloudflare as a
separate smoke test, including that unauthenticated organizer access is denied.

## Metrics and alerts

`GET /internal/metrics` requires
`Authorization: Bearer <METRICS_API_TOKEN>`. Scrape it through a private path;
never publish the token or exempt this endpoint at the edge. The endpoint emits
bounded database-derived gauges:

- `raceframe_jobs{queue,status}` and
  `raceframe_oldest_queued_job_seconds{queue}`;
- `raceframe_workers{status,freshness}` and
  `raceframe_freshest_worker_heartbeat_age_seconds`;
- `raceframe_object_deletion_tasks{status}` and
  `raceframe_expired_face_search_sessions`;
- `raceframe_db_pool_checked_out` and `raceframe_db_pool_size`.

At minimum, page on no fresh worker during event processing, rapidly growing
queue age, any dead-lettered job/deletion task, repeated 5xx responses,
readiness failure, backup failure, disk exhaustion, or R2/API spend anomalies.
Warn before DB pool saturation and when expired sessions remain after two
maintenance intervals. Tune thresholds from measured event traffic, not
guesses. Include the release commit, request ID, queue, and non-sensitive job
ID in incident context.

Prometheus client state is process-local; with the current two Uvicorn workers,
database gauges are authoritative while per-process HTTP counters can represent
only the responding process. Configure Prometheus multiprocess collection or
move request metrics to the reverse proxy before relying on aggregate HTTP
counter alerts.

## Scheduled maintenance

Install the example user timer after adjusting its repository path:

```bash
install -d ~/.config/systemd/user
cp deploy/systemd/raceframe-maintenance.* ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now raceframe-maintenance.timer
systemctl --user list-timers raceframe-maintenance.timer
```

The service account must be allowed to use Docker. Enable lingering with
`sudo loginctl enable-linger "$USER"` if timers must run without a login.
The oneshot unit prevents overlapping instances. Inspect every failure:

```bash
journalctl --user -u raceframe-maintenance.service --since today
docker compose --profile maintenance run --rm maintenance
```

Maintenance expires at most 500 biometric searches per run, durably deletes up
to the configured batch (hard cap 1,000), purges
old raw diagnostics/rate-limit buckets, retries transient deletion failures,
and dead-letters exhausted deletions. It is idempotent and should run every
five minutes. A run can exit successfully after scheduling a transient deletion
retry, so logs/metrics—not only the systemd exit code—must be monitored.

## Worker incidents

If a worker disappears, do not manually rewrite processing rows. Stop/restart
the worker and allow expired leases to become reclaimable. A completion from an
old attempt is rejected by its `attempt_id`. Investigate dead letters before
explicit replay so bad input cannot create an infinite expensive loop.

For planned deploys, stop/drain the old worker first, migrate and deploy the
backend, and only then start the protocol-matched new worker. The hardened API
requires `attempt_id`; an old worker left running against it receives `422`.
Legacy processing rows with null leases are reclaimable by the new worker.

## Incident priorities

1. Preserve evidence: release digest, UTC time window, request/job IDs, logs,
   metrics, and database state. Never log/download selfie bytes unnecessarily.
2. Contain at the narrowest layer: edge route/rate rule, worker stop, or upload
   pause. Do not destroy data or rotate all credentials without identifying the
   affected boundary.
3. Restore service from a known digest. Database changes follow the backup and
   rollback procedure; do not improvise destructive SQL.
4. Reconcile database/R2 state and verify retention after recovery.
5. Record cause, affected data/tenants, timeline, corrective action, and whether
   customer/privacy notification is required.
