# RaceFrame Admin Implementation

## Purpose

This file describes the current admin-panel implementation for RaceFrame as it exists now in the codebase and on the OCI deployment.

## Current state

The admin panel is currently implemented as a plain server-rendered FastAPI flow.

It is intentionally simple:

- no login/auth yet
- server-rendered HTML pages
- focused on working event and participant management
- now also tied into the photo-matching workflow for roster updates

Public admin entry point:

- `https://raceframe.ronanrocking.com/admin`

## Implemented admin routes

- `GET /admin`
  - admin dashboard
  - lists existing events
  - shows basic event details and participant counts

- `GET /admin/events/new`
  - create-event page

- `POST /admin/events/new`
  - creates a new event

- `GET /admin/events/{event_id}/edit`
  - edit-event page
  - shows event fields
  - shows participant import controls
  - shows participant add/edit/delete controls
  - shows participant table

- `POST /admin/events/{event_id}/edit`
  - updates event fields

- `POST /admin/events/{event_id}/delete`
  - deletes the entire event

- `POST /admin/events/{event_id}/participants/upload`
  - uploads CSV/XLSX participant data

- `POST /admin/events/{event_id}/participants/add`
  - adds a participant from the browser

- `POST /admin/events/{event_id}/participants/{participant_id}/update`
  - updates a participant from the browser

- `POST /admin/events/{event_id}/participants/{participant_id}/delete`
  - deletes one participant

- `POST /admin/events/{event_id}/participants/delete-all`
  - deletes all participants for the event

- `POST /admin/session/takeover`
  - force-takes the admin session if a stale/foreign lock is blocking access

## Event behavior

Current event fields:

- `name`
- `event_date`
- `location`
- `status`

Current status values:

- `draft`
- `published`

Slug behavior:

- slug is not user-entered
- slug is auto-generated from the event name
- if a slug already exists, integer suffixes are added
- examples:
  - `freedom-run`
  - `freedom-run-2`

Event delete behavior:

- deleting an event deletes participant rows through the DB relationship cascade
- deleting an event also deletes event photo records and attempts to remove the backing R2 objects first

## Participant behavior

Current participant fields in active use:

- `bib_number`
- `full_name`

DB table also has:

- `id`
- `event_id`
- `created_at`

Participant uniqueness rule:

- `bib_number` must be unique within an event

## Upload behavior

Upload formats supported:

- `.csv`
- `.xlsx`
- `.xlsm`

Preferred columns:

- `bib_number`
- `full_name`

Accepted alternate headers:

- bib variants:
  - `bib`
  - `bib_number`
  - `bib_no`
  - `bib_num`
  - `bib_no.`
  - `bib#`
  - `race_number`
  - `runner_number`
  - `number`

- full-name variants:
  - `full_name`
  - `name`
  - `participant_name`
  - `runner_name`
  - `athlete_name`

- split-name variants:
  - `first_name`
  - `firstname`
  - `given_name`
  - `last_name`
  - `lastname`
  - `surname`
  - `family_name`

Import semantics:

- upload is an upsert, not a replace-all
- if bib does not exist for that event, a participant is added
- if bib already exists for that event, that participant row is updated
- rows missing bib or name are skipped
- duplicate bibs inside one upload are deduplicated
- missing rows are not deleted automatically
- after import, photo-to-participant matching is rebuilt for that event

If a full replace is needed:

1. use `Delete All Participants`
2. upload the new file

## Browser-side participant management

On the event edit page, admins can:

- add a participant directly
- edit bib/name directly inside the participant table
- delete an individual participant
- delete all participants for the event
- manage uploaded photo records for the event

Participant change side effect:

- add/edit/delete/import/delete-all participant actions now trigger a full photo-match rebuild for that event
- this keeps `photo_participant_matches` aligned with the latest roster

The participant table currently shows:

- `Bib Number`
- `Full Name`
- `Created`

## Concurrency / locking

Current admin concurrency control is a temporary global admin-session lock.

Behavior:

- one active browser session at a time
- same browser can use multiple tabs because it shares the same cookie
- a different browser/session is blocked
- blocked page includes a takeover button
- stale locks expire after inactivity

Important note:

- this is an MVP safety mechanism
- it is not the ideal final concurrency design
- long-term, optimistic concurrency or per-record conflict handling would be better

## Implementation files

Main backend files involved:

- [main.py](/D:/PWD/Porjects/raceframe/backend/app/main.py)
- [admin.py](/D:/PWD/Porjects/raceframe/backend/app/admin.py)
- [models.py](/D:/PWD/Porjects/raceframe/backend/app/models.py)

Templates:

- [base.html](/D:/PWD/Porjects/raceframe/backend/app/templates/base.html)
- [admin_dashboard.html](/D:/PWD/Porjects/raceframe/backend/app/templates/admin_dashboard.html)
- [admin_event_form.html](/D:/PWD/Porjects/raceframe/backend/app/templates/admin_event_form.html)

Minimal styling:

- [admin.css](/D:/PWD/Porjects/raceframe/backend/app/static/admin.css)

## Known limitations

- no auth/login yet
- global admin lock is crude
- no migrations yet; schema is still created on startup
- participant model is intentionally minimal for now
- matching is still bib/OCR driven and intentionally simple
