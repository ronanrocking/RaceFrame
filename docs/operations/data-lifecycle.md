# Data lifecycle, backfill, and storage reconciliation

RaceFrame stores durable metadata in PostgreSQL and image objects in R2. A
database transaction cannot atomically delete an R2 object, so deletions use a
durable outbox with leases, bounded retries, and dead letters.

## Automatic lifecycle

Run `python -m app.maintenance run` every five minutes through the supplied
oneshot timer. Each bounded, idempotent run:

- expires face-search sessions after `BIOMETRIC_RETENTION_HOURS` and enqueues
  their uploaded images for deletion;
- leases/deletes queued R2 objects and retries transient failures with backoff;
- dead-letters deletion tasks after the configured attempt limit;
- removes old worker raw diagnostics after
  `RAW_RESPONSE_RETENTION_HOURS`; and
- purges expired persistent rate-limit buckets.

Alert on `raceframe_object_deletion_tasks{status="dead_lettered"} > 0` and on
expired sessions remaining for more than two intervals. Investigate object
permissions, R2 availability, and the redacted `last_error`; replay only after
the cause is fixed. Do not delete DB rows to hide a failed privacy deletion.

## Storage reconciliation

Run a read-only reconciliation weekly and after every restore or storage
incident:

```bash
docker compose --profile maintenance run --rm maintenance \
  python -m app.storage_reconcile
```

`deploy/systemd/raceframe-storage-reconcile.*` schedules this dry-run weekly.
A nonzero timer result is an alert, not permission to run apply mode.

It lists the `events/` R2 namespace, compares it with photo/thumbnail/search/
participant-face references, prints `missing_referenced_objects` and
`sensitive_orphans`, and exits `2` if referenced objects are missing. Missing
objects can produce broken customer results: preserve evidence and recover the
object or correct the reference through a reviewed repair.

Apply mode is intentionally narrow:

```bash
docker compose --profile maintenance run --rm maintenance \
  python -m app.storage_reconcile --apply
```

It queues/deletes only unreferenced keys recognized as temporary face-search or
participant-face objects and attempts at most 1,000 immediately. It does not
delete orphan race photos/thumbnails.
Review the dry-run count, active incidents, database freshness, and backup/R2
versioning before `--apply`. Concurrent uploads can race a bucket inventory, so
schedule apply mode during a quiet window and run a second dry check afterward.

## Thumbnail backfill

After upgrading legacy photos, first count a bounded batch without writes:

```bash
docker compose --profile maintenance run --rm maintenance \
  python -m app.thumbnail_backfill --limit 100
```

Then process small batches while watching R2 errors, DB locks, CPU/memory, and
cost:

```bash
docker compose --profile maintenance run --rm maintenance \
  python -m app.thumbnail_backfill --limit 100 --apply
```

In dry-run output, `processed` means candidate rows, not completed writes. The
limit is capped at 1,000; repeat small applied batches. The command is
idempotent for rows with no thumbnail reference, revalidates
stored image bytes and limits, uploads the derivative, and records its key and
missing checksum. A failed batch exits nonzero. Resume after investigating;
never increase decode limits merely to force malformed legacy data through.

## Retention policy ownership

Before selling the service, document per data class: business purpose, legal
basis/consent, tenant visibility, retention clock, deletion behavior (including
R2 versions and backups), export process, incident handling, and owner. The
24-hour biometric/raw-diagnostic defaults are technical maximums, not legal
advice. Customer contracts and jurisdiction-specific biometric/privacy rules
remain a manual release gate.
