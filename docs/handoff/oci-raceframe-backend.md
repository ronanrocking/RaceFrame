# RaceFrame OCI Backend Status

## Current deployment

- VPS: `ssh oci`
- backend service path: `~/raceframe/raceframe-backend`
- postgres service path: `~/raceframe/raceframe-postgres`
- runtime: `docker compose`
- shared Docker network: `raceframe-internal`
- backend container: `raceframe-backend`
- postgres container: `raceframe-postgres`
- backend origin bind on VPS: `127.0.0.1:8008`
- postgres bind on VPS: `127.0.0.1:5432`

## Current behavior

The FastAPI backend currently provides:

- `GET /` -> `{"status":"ok"}`
- `GET /health` -> `{"status":"ok"}`
- `GET /admin` -> admin dashboard
- `GET /admin/events/new` -> create event page
- `GET /admin/events/{event_id}/edit` -> edit event page
- `POST /admin/events/{event_id}/participants/upload` -> CSV/XLSX participant import
- `GET /upload` -> photographer event picker
- `GET /upload/events/{event_id}` -> batch photo upload page
- `GET /user` -> public event picker for photo search
- `GET /user/events/{event_id}` -> public photo search page
- `GET /user/events/{event_id}/download-all` -> ZIP download of matching photos

Implemented database tables right now:

- `events`
- `participants`
- `photos`
- `photo_jobs`
- `photo_text_detection`
- `photo_participant_matches`
- `admin_session_locks`

Schema bootstrapping right now:

- tables are created by the FastAPI app on startup via SQLAlchemy `create_all`
- Alembic/migrations are not set up yet

Current admin behavior:

- list existing events with basic details and participant counts
- create events with `name`, auto-generated `slug`, `event_date`, `location`, `status`
- event slugs are generated from the name and gain integer suffixes on collisions
- edit event name/date/location/status from the browser
- delete events from the edit page
- upload participant files with headers such as `bib_number` + `full_name`
- add participants from the browser
- edit participant bib/name rows directly in the browser
- delete individual participants from the browser
- delete all participants for an event from the browser
- also accepts common alternates like `bib`, `name`, `first_name`, `last_name`
- participant changes trigger a photo-match rebuild for that event

Current photo/user behavior:

- photographers can upload batches of race images from `/upload`
- uploads go to R2 and OCR runs during ingestion
- OCR detections are stored and used to build participant-photo matches
- public users can pick an event, search by bib/name, and download matched photos
- public bib search also has a direct OCR fallback for testing even when a participant match row is missing

Import behavior:

- CSV and XLSX supported
- rows missing bib or name are skipped
- repeated bibs inside the same upload are deduplicated
- existing participants are updated by `(event_id, bib_number)`

Admin concurrency guard:

- only one active admin browser session is allowed at a time
- same-browser multiple tabs keep working because they share the same cookie
- a different browser/session is blocked with an "Admin panel in use" response
- stale admin locks expire after roughly 30 minutes of inactivity

## Secrets and config

Keep live secrets on the server only:

- `~/raceframe/raceframe-postgres/.env`
- `~/raceframe/raceframe-backend/.env`

Do not commit those files.

## Useful commands

```bash
ssh oci "cd ~/raceframe/raceframe-backend && docker compose ps"
ssh oci "cd ~/raceframe/raceframe-backend && docker compose logs --tail 40"
ssh oci "curl -sS http://127.0.0.1:8008/health"
ssh oci "curl -sS http://127.0.0.1:8008/admin | head"
ssh oci "cd ~/raceframe/raceframe-postgres && docker compose ps"
ssh oci "docker exec raceframe-postgres pg_isready -U raceframe -d raceframe"
ssh oci "docker exec raceframe-postgres psql -U raceframe -d raceframe -c '\dt'"
ssh oci "docker network inspect raceframe-internal >/dev/null && echo ok"
```

## Cloudflare routing intent

Active public exposure:

- active public hostname: `raceframe.ronanrocking.com`
- point that hostname at `http://localhost:8008` on the OCI VPS

Tunnel/service status:

- `cloudflared` installed on OCI via Debian package
- systemd service name: `cloudflared`
- public admin route verified through Cloudflare

This tunnel path avoids competing with coturn, which already owns port `443` on the VPS.
