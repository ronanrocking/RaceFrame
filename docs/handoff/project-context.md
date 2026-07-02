# RaceFrame Project Context

## Project Summary

RaceFrame is an OCR-based race photo finder MVP.

The product currently has 3 sides:

- `user`: enters a bib number or participant name and gets matching race photos
- `admin`: creates events and uploads event data such as participants and bib numbers
- `photographer`: uploads photos that are automatically processed and stored in the cloud, while extracted labels/details are stored on the server

For the current MVP, these sides are exposed through separate links and are open to all. There is no authentication or role database yet.

## Current MVP Scope

The MVP is intentionally narrow:

- public search by bib number or participant name
- event creation and basic event management
- participant data upload/import by admin
- photographer photo upload
- automatic OCR/text detection from uploaded photos
- participant-photo matching based on detected bib text

Not in scope yet:

- login/auth system
- admin database tables
- photographer database tables
- user accounts
- payments
- face recognition
- advanced moderation/review flows

## Chosen Core Database Tables

Only these tables are currently in scope:

- `events`
- `participants`
- `photos`
- `photo_jobs`
- `photo_text_detection`
- `photo_participant_matches`

No extra database tables should be assumed unless explicitly added later.

## Database Intent

### `events`

Stores each race/event.

Key ideas:

- each event has a unique `slug`
- event pages are public-facing by slug
- event status can be `draft` or `published`

### `participants`

Stores participant data for a specific event.

Key ideas:

- bib numbers are unique within an event
- participant search is by `bib_number` or `full_name`

### `photos`

Stores uploaded photo metadata and cloud storage object keys.

Key ideas:

- actual image files live in cloud object storage
- database stores metadata and object paths only
- each photo belongs to one event

### `photo_jobs`

Tracks background processing work per photo.

Key ideas:

- OCR is the main initial job type
- jobs need statuses such as `queued`, `processing`, `completed`, `failed`
- job errors and raw OCR response should be stored for debugging

### `photo_text_detection`

Stores OCR text extracted from a photo.

Key ideas:

- keep raw OCR text
- keep normalized text for matching
- optional confidence and bounding box data

### `photo_participant_matches`

Stores the final decision linking a photo to a participant.

Key ideas:

- this is the main search result table
- should include `event_id`, `photo_id`, and `participant_id`
- `event_id` is intentionally duplicated here for simpler filtering and stronger integrity checks

## Important Constraints and Indexes

Important constraints:

- `events.slug` unique
- `participants(event_id, bib_number)` unique
- `photo_participant_matches(photo_id, participant_id)` unique

Important indexes:

- `events(slug)`
- `participants(event_id, bib_number)`
- `participants(event_id, full_name)`
- `photos(event_id, status)`
- `photo_jobs(photo_id, status)`
- `photo_text_detection(photo_id)`
- `photo_participant_matches(event_id, participant_id)`
- `photo_participant_matches(photo_id)`

## Slug Definition

A `slug` is a URL-friendly unique text identifier used in links.

Example:

- event name: `Bangalore City Marathon 2026`
- slug: `bangalore-city-marathon-2026`

Example route:

`/event/bangalore-city-marathon-2026`

## Postgres Decision

Postgres is the planned primary relational database for the project.

Postgres will store:

- event metadata
- participant rows
- photo metadata
- photo processing jobs
- OCR detections
- participant-photo matches

Postgres will not store:

- original image binaries
- thumbnails as binary data

Those image assets should live in cloud object storage, with keys/paths stored in `photos`.

## Storage / Infra Direction

Agreed direction so far:

- backend: `FastAPI`
- templating/UI: server-rendered pages are acceptable for MVP
- database: `Postgres`
- object storage: `Cloudflare R2`
- OCR provider: `Google Vision OCR`
- hosting: `OCI VPS`
- production serving: `Nginx` + app server

Expected production layout for MVP:

- OCI VPS runs the app
- OCI VPS can also run Postgres for the MVP
- Cloudflare R2 stores originals and thumbnails
- OCR provider processes images externally

## Implementation Order Agreed So Far

Recommended setup order:

1. freeze MVP scope
2. finalize database schema
3. set up FastAPI project skeleton
4. set up Postgres and migrations
5. build event management
6. build participant import
7. set up cloud photo storage
8. build photographer upload flow
9. build OCR/job pipeline
10. build public search flow
11. add retry/status/error handling
12. test with real sample data
13. deploy after local end-to-end flow works

Compressed build order:

1. schema
2. FastAPI skeleton
3. Postgres + migrations
4. event CRUD
5. participant import
6. photo upload + storage
7. OCR/job pipeline
8. participant-photo matching
9. public search
10. polish + deploy

## Git / Repo Context

Current local workspace:

- `D:\PWD\Porjects\raceframe`

Git state established so far:

- local Git repository initialized in current folder
- remote `origin` configured as `https://github.com/ronanrocking/RaceFrame.git`

No assumption should be made that remote contents have already been fetched or pulled into the workspace.

## Existing Docs

Database/ER reference exists in:

- [db-design.md](D:\PWD\Porjects\raceframe\docs\handoff\db-design.md)

## Guidance For Future Agents

- Do not introduce auth tables yet unless explicitly requested.
- Keep the MVP centered on the 6 agreed tables.
- Treat cloud storage and relational storage as separate concerns.
- Preserve `event_id` in `photo_participant_matches`.
- Prefer simple, shippable architecture over premature abstraction.
