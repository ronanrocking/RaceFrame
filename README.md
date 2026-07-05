# RaceFrame

RaceFrame is an OCR-based race photo finder MVP.

It currently has three simple flows:

- `admin`: create and manage events, import participants, and maintain event data
- `upload`: upload race photos for an event and run OCR/matching
- `user`: pick an event, search by bib number or participant name, and download matched photos

## Current Stack

- Backend: FastAPI
- Database: Postgres
- Storage: Cloudflare R2
- OCR: Google Vision OCR
- Deployment: Docker on OCI VPS
- UI: server-rendered HTML + shared CSS

## Current Implemented Features

- event create/edit/delete
- participant CRUD and CSV/XLSX import
- batch race photo upload
- OCR text detection on uploaded photos
- participant-photo matching from bib text
- public event picker and photo search
- per-photo download and download-all ZIP

## Important Routes

- `/admin`
- `/upload`
- `/user`
- `/health`

## Repo Notes

- `docs/handoff/` is for local handoff/reference notes and is not intended to stay tracked in Git
- the app currently uses SQLAlchemy `create_all` on startup rather than migrations
- there is no auth/login layer yet

## Local Workspace

Main backend code lives under:

- `backend/app/main.py`
- `backend/app/admin.py`
- `backend/app/photographer.py`
- `backend/app/models.py`

Templates and styles live under:

- `backend/app/templates/`
- `backend/app/static/admin.css`

## Deployment Notes

Production is hosted on an OCI VPS and exposed through Cloudflare Tunnel.

Useful handoff docs exist locally in:

- `docs/handoff/project-context.md`
- `docs/handoff/oci-raceframe-backend.md`
- `docs/handoff/raceframe-server-setup.md`
