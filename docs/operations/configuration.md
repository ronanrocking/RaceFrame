# Production configuration reference

RaceFrame reads configuration only at process startup. Keep the real backend,
database, worker, backup, and Google credentials outside Git with mode `0600`.
The checked-in `.env.example` files are inventories, not deployable values.

## Required production values

| Variable | Purpose and production rule |
| --- | --- |
| `APP_ENV` | Set exactly to `production` (or `staging`). A deployed service using the development default refuses to boot. |
| `RACEFRAME_BACKEND_IMAGE`, `RACEFRAME_WORKER_IMAGE` | Complete GHCR references ending in `@sha256:<digest>`, copied from the release manifest. |
| `RACEFRAME_SECRET_KEY` | At least 32 random bytes; signs browser capabilities and security tokens. Do not reuse any other secret. |
| `DATABASE_URL` | Application-role DSN (`raceframe_app`), not the schema owner or bootstrap admin. Percent-encode reserved password characters. |
| `.migration.env` `DATABASE_URL` | Schema-owner DSN (`raceframe_owner`), loaded only by the one-shot migration profile. Keep it out of the normal `.env`. |
| `ALLOWED_HOSTS` | Comma-separated explicit public hostnames. Wildcards, URLs, and paths are rejected. |
| `PUBLIC_ORIGINS` | Comma-separated exact HTTPS origins. Every hostname must also occur in `ALLOWED_HOSTS`. |
| `TRUST_PROXY_HEADERS` | `true` only when the origin accepts traffic exclusively from the trusted reverse proxy or tunnel. |
| `CLOUDFLARE_R2_*` | Account, bucket, HTTPS endpoint, access-key ID, and secret. Use a dedicated key restricted to the single production bucket. |
| `WORKER_API_TOKEN` | At least 32 random bytes shared only by backend and workers. It must differ from every browser, metrics, and database secret. |
| `METRICS_API_TOKEN` | A different token of at least 32 random bytes. Give it only to the metrics scraper. |
| `BACKEND_BASE_URL` | Worker's public HTTPS backend origin; never a URL containing credentials. |
| `GOOGLE_APPLICATION_CREDENTIALS` | Worker path to a read-only mounted service-account JSON file. Scope the service account to the required Vision API only. |
| `FACE_MODEL_NAME` | Exact reviewed, commercially licensed model installed in the persistent model cache. This is an operator-owned release gate. |

Generate independent secrets with a cryptographic generator, for example
`openssl rand -base64 48`. Rotate a leaked secret immediately. Rotating
`RACEFRAME_SECRET_KEY` invalidates outstanding browser capabilities; rotating
`WORKER_API_TOKEN` requires a coordinated backend/worker restart.

## Backend limits and retention

The production example records the currently reviewed defaults. Keep all byte
limits in bytes and all retention/lease values in seconds or hours as named.

| Variables | Meaning |
| --- | --- |
| `MAX_PHOTO_UPLOAD_BYTES`, `MAX_SELFIE_UPLOAD_BYTES`, `MAX_PARTICIPANT_UPLOAD_BYTES` | Per-file body limits (25 MiB, 10 MiB, and 5 MiB defaults). |
| `MAX_PHOTO_BATCH_FILES`, `MAX_SELFIE_BATCH_FILES` | Files accepted by one operation (50 and 5). |
| `MAX_PHOTO_REQUEST_BYTES`, `MAX_FORM_REQUEST_BYTES` | Streaming request-envelope caps (256 MiB and 2 MiB). Keep the proxy limits consistent and no larger than necessary. |
| `MAX_IMAGE_PIXELS`, `MAX_IMAGE_DIMENSION` | Decoded-image bomb limits (40 million pixels and 12,000 px). |
| `MAX_PARTICIPANT_ROWS`, `MAX_PARTICIPANT_COLUMNS`, `MAX_PARTICIPANT_CELL_CHARS`, `MAX_SPREADSHEET_UNCOMPRESSED_BYTES` | Spreadsheet expansion and content limits. |
| `MAX_FACE_SEARCH_BACKLOG`, `MAX_PHOTO_JOB_BACKLOG` | Admission backpressure thresholds. |
| `MAX_SEARCH_RESULTS`, `MAX_SEARCH_FACES_PER_EVENT` | Bounded query and compute results. |
| `SEARCH_CAPABILITY_TTL_SECONDS` | Browser search authorization lifetime; default one hour. |
| `BIOMETRIC_RETENTION_HOURS` | Maximum temporary face-search session lifetime before scheduled purge; default 24 hours. |
| `RAW_RESPONSE_RETENTION_HOURS` | Worker diagnostic payload lifetime; default 24 hours. |
| `DELETION_RETRY_BATCH_SIZE` | Object deletion outbox batch; default 100. |
| `WORKER_LEASE_SECONDS`, `WORKER_MAX_ATTEMPTS`, `WORKER_RETRY_BASE_SECONDS`, `WORKER_RETRY_MAX_SECONDS` | Durable claim lease, per-job dead-letter threshold, and bounded retry-delay policy. Defaults are 300 seconds, 5 attempts, 10 seconds, and 900 seconds. |
| `WORKER_HEARTBEAT_STALE_SECONDS` | Worker freshness threshold used by metrics; default 120 seconds and greater than the worker heartbeat interval. |
| `CLOUDFLARE_R2_PRESIGNED_URL_TTL_SECONDS` | Signed object URL lifetime; production example uses 900 seconds. |
| `PHOTO_THUMBNAIL_MAX_EDGE`, `PHOTO_THUMBNAIL_QUALITY` | Thumbnail output budget; defaults 640 px and quality 72. |

The `FACE_*` and `BIB_ONLY_*` values in the example are matching policy, not
security knobs. Change them only with a versioned evaluation dataset, recorded
false-positive/false-negative results, and an explicit rollback value.

## Worker controls

`WORKER_ID` must be a stable opaque identifier unique per running worker;
`WORKER_VERSION` should be the deployed Git commit. The worker rejects invalid
configuration before polling. `WORKER_JOB_TYPES` controls only photo OCR/scan
claims (`ocr,face_photo_scan`); participant-face and search queues are polled
independently. `WORKER_HEARTBEAT_SECONDS` defaults to 30 and must remain below
the backend stale threshold.

The download and decode limits (`WORKER_MAX_DOWNLOAD_BYTES`,
`WORKER_MAX_IMAGE_PIXELS`, `WORKER_MAX_IMAGE_DIMENSION`) should be equal to or
stricter than backend upload limits. HTTP retry/backoff values are bounded in
the worker. `FACE_DET_SIZE`, `FACE_MAX_IMAGE_EDGE`,
`SEARCH_FACE_MIN_EDGE`, and `SEARCH_FACE_MIN_DETECTION_SCORE` are reviewed ML
resource/quality policy.

## PostgreSQL bootstrap values

`POSTGRES_USER` is the bootstrap administrator, `RACEFRAME_OWNER_PASSWORD` is
the migration credential, and `RACEFRAME_APP_PASSWORD` is the application
credential. All three passwords must be distinct. Initialization uses SCRAM,
data checksums, and creates least-privilege roles. Init scripts run only for an
empty data directory; follow the release runbook for an existing cluster.

Never put `.migration.env`, the bootstrap password, R2 credentials, the
Google JSON key, backup age identity, or Cloudflare API credentials in the app
or worker image. Record secret ownership, creation date, last rotation, and the
next rotation deadline in the operator's secret manager.
