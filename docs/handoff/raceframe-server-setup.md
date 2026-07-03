# RaceFrame Server Setup

## Purpose

This file records the actual server-side setup used for RaceFrame on the OCI VPS so the stack can be rebuilt or debugged without reconstructing it from chat history.

## Host

- SSH entry: `ssh oci`
- OS checked during setup: `Ubuntu 22.04.5 LTS`
- architecture: `arm64` / `aarch64`

## Public access

- public hostname: `raceframe.ronanrocking.com`
- Cloudflare Tunnel forwards traffic to: `http://localhost:8008`
- `cloudflared` runs on the OCI VPS as a systemd service
- systemd service name: `cloudflared`

Useful checks:

```bash
ssh oci "sudo systemctl status cloudflared --no-pager"
ssh oci "sudo journalctl -u cloudflared -n 40 --no-pager"
ssh oci "curl -I https://raceframe.ronanrocking.com"
```

## Service layout

Current service folders:

```text
~/raceframe/
  raceframe-backend/
  raceframe-postgres/

~/server-services/
  coturn/
  reverse-proxy/
```

RaceFrame-specific runtime pieces:

- backend service path: `~/raceframe/raceframe-backend`
- postgres service path: `~/raceframe/raceframe-postgres`
- shared Docker network: `raceframe-internal`

## Backend service

- container name: `raceframe-backend`
- local origin bind: `127.0.0.1:8008`
- app server: `uvicorn`
- framework: `FastAPI`

Backend startup currently creates tables directly through SQLAlchemy `create_all`.

Useful checks:

```bash
ssh oci "cd ~/raceframe/raceframe-backend && docker compose ps"
ssh oci "cd ~/raceframe/raceframe-backend && docker compose logs --tail 40"
ssh oci "curl -sS http://127.0.0.1:8008/health"
ssh oci "curl -sS http://127.0.0.1:8008/admin | head"
```

## Postgres service

- container name: `raceframe-postgres`
- local bind: `127.0.0.1:5432`
- image: `postgres:16`
- database name: `raceframe`

Useful checks:

```bash
ssh oci "cd ~/raceframe/raceframe-postgres && docker compose ps"
ssh oci "docker exec raceframe-postgres pg_isready -U raceframe -d raceframe"
ssh oci "docker exec raceframe-postgres psql -U raceframe -d raceframe -c '\dt'"
```

## Secrets

Live env files stay on the server only:

- `~/raceframe/raceframe-backend/.env`
- `~/raceframe/raceframe-postgres/.env`

Do not copy those secrets into repo docs.

## Current app behavior

Current working backend/admin slice:

- `GET /` health-style JSON response
- `GET /health` health-style JSON response
- `GET /admin` event dashboard
- `GET /admin/events/new` event creation page
- `GET /admin/events/{event_id}/edit` event edit page
- browser CRUD for participants on the event edit page
- CSV/XLSX participant import on the event edit page

Implemented tables in active use:

- `events`
- `participants`
- `admin_session_locks`

Planned later tables not yet wired into the app:

- `photos`
- `photo_jobs`
- `photo_text_detection`
- `photo_participant_matches`

## Notes on admin locking

There is currently a temporary global admin-session lock.

Behavior:

- one active browser session at a time
- same browser can use multiple tabs because it shares the same cookie
- another browser/session is blocked
- blocked screen provides a takeover action
- lock expires after inactivity

This is a temporary MVP safety mechanism, not the ideal final concurrency design.

## Deployment pattern

Current update pattern used successfully:

1. edit files locally in the repo
2. `scp` updated backend files into `~/raceframe/raceframe-backend/app/`
3. `scp` template/static changes into the matching backend folders
4. run:

```bash
ssh oci "cd ~/raceframe/raceframe-backend && docker compose up -d --build"
```

For Postgres/service-structure changes, also update the matching compose/env files under `~/raceframe/raceframe-postgres`.
