from __future__ import annotations

import csv
import io
import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from openpyxl import load_workbook
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import AdminSessionLock, Event, Participant


EVENT_STATUSES = ("draft", "published")
ADMIN_LOCK_ID = 1
ADMIN_SESSION_TTL = timedelta(minutes=30)

HEADER_ALIASES = {
    "bib_number": {
        "bib",
        "bib_number",
        "bib_no",
        "bib_num",
        "bib_no.",
        "bib#",
        "bib_number.",
        "race_number",
        "runner_number",
        "number",
    },
    "full_name": {
        "full_name",
        "name",
        "participant_name",
        "runner_name",
        "athlete_name",
    },
    "first_name": {
        "first_name",
        "firstname",
        "given_name",
    },
    "last_name": {
        "last_name",
        "lastname",
        "surname",
        "family_name",
    },
}


@dataclass
class EventListItem:
    event: Event
    participant_count: int


@dataclass
class ParticipantUploadResult:
    processed_rows: int
    inserted_rows: int
    updated_rows: int
    skipped_rows: int


def slugify(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return normalized or "event"


def normalize_header(value: object) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[\s\-]+", "_", text)
    return text


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def list_events(session: Session) -> list[EventListItem]:
    stmt = (
        select(Event, func.count(Participant.id))
        .outerjoin(Participant, Participant.event_id == Event.id)
        .group_by(Event.id)
        .order_by(Event.created_at.desc())
    )
    return [EventListItem(event=row[0], participant_count=row[1]) for row in session.execute(stmt).all()]


def list_participants(session: Session, *, event_id: uuid.UUID) -> list[Participant]:
    return (
        session.query(Participant)
        .filter(Participant.event_id == event_id)
        .order_by(Participant.bib_number.asc(), Participant.created_at.asc())
        .all()
    )


def get_event(session: Session, event_id: str) -> Event | None:
    try:
        parsed_id = uuid.UUID(str(event_id))
    except ValueError:
        return None
    return session.get(Event, parsed_id)


def get_participant(session: Session, participant_id: str) -> Participant | None:
    try:
        parsed_id = uuid.UUID(str(participant_id))
    except ValueError:
        return None
    return session.get(Participant, parsed_id)


def slug_exists(session: Session, slug: str, *, exclude_event_id: str | None = None) -> bool:
    stmt = select(Event.id).where(Event.slug == slug)
    if exclude_event_id:
        try:
            stmt = stmt.where(Event.id != uuid.UUID(str(exclude_event_id)))
        except ValueError:
            pass
    return session.execute(stmt).scalar_one_or_none() is not None


def generate_unique_slug(session: Session, *, name: str, exclude_event_id: str | None = None) -> str:
    base_slug = slugify(name)
    candidate = base_slug
    suffix = 2
    while slug_exists(session, candidate, exclude_event_id=exclude_event_id):
        candidate = f"{base_slug}-{suffix}"
        suffix += 1
    return candidate


def create_event(
    session: Session,
    *,
    name: str,
    slug: str,
    event_date: date | None,
    location: str | None,
    status: str,
) -> Event:
    event = Event(
        name=name,
        slug=slug,
        event_date=event_date,
        location=location,
        status=status,
    )
    session.add(event)
    session.commit()
    session.refresh(event)
    return event


def update_event(
    session: Session,
    *,
    event: Event,
    name: str,
    slug: str,
    event_date: date | None,
    location: str | None,
    status: str,
) -> Event:
    event.name = name
    event.slug = slug
    event.event_date = event_date
    event.location = location
    event.status = status
    session.commit()
    session.refresh(event)
    return event


def delete_event(session: Session, *, event: Event) -> None:
    session.delete(event)
    session.commit()


def parse_participant_file(file_name: str, content: bytes) -> list[dict[str, str]]:
    suffix = file_name.lower().rsplit(".", 1)[-1] if "." in file_name else ""
    if suffix == "csv":
        return _parse_csv(content)
    if suffix in {"xlsx", "xlsm"}:
        return _parse_excel(content)
    raise ValueError("Upload a CSV or XLSX file.")


def _parse_csv(content: bytes) -> list[dict[str, str]]:
    text = content.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError("CSV file is missing a header row.")
    return [_normalize_row(row) for row in reader]


def _parse_excel(content: bytes) -> list[dict[str, str]]:
    workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    sheet = workbook.active
    rows = list(sheet.iter_rows(values_only=True))
    if not rows:
        raise ValueError("Spreadsheet is empty.")
    headers = [normalize_header(cell) for cell in rows[0]]
    if not any(headers):
        raise ValueError("Spreadsheet is missing a header row.")
    normalized_rows: list[dict[str, str]] = []
    for values in rows[1:]:
        row = {headers[index]: "" if value is None else str(value).strip() for index, value in enumerate(values)}
        normalized_rows.append(_normalize_row(row))
    return normalized_rows


def _normalize_row(raw_row: dict[object, object]) -> dict[str, str]:
    normalized = {normalize_header(key): str(value or "").strip() for key, value in raw_row.items()}

    first_name = _read_value(normalized, "first_name")
    last_name = _read_value(normalized, "last_name")
    full_name = _read_value(normalized, "full_name")
    if not full_name and (first_name or last_name):
        full_name = f"{first_name} {last_name}".strip()

    return {
        "bib_number": _read_value(normalized, "bib_number"),
        "full_name": full_name,
    }


def _read_value(row: dict[str, str], canonical_name: str) -> str:
    for header in HEADER_ALIASES[canonical_name]:
        if header in row and row[header]:
            return row[header]
    return ""


def upsert_participants(
    session: Session,
    *,
    event: Event,
    rows: Iterable[dict[str, str]],
) -> ParticipantUploadResult:
    cleaned_rows: list[dict[str, str]] = []
    skipped_rows = 0
    for row in rows:
        bib_number = row.get("bib_number", "").strip()
        full_name = row.get("full_name", "").strip()
        if not bib_number or not full_name:
            skipped_rows += 1
            continue
        cleaned_rows.append({"bib_number": bib_number, "full_name": full_name})

    if not cleaned_rows:
        return ParticipantUploadResult(processed_rows=0, inserted_rows=0, updated_rows=0, skipped_rows=skipped_rows)

    existing = {
        participant.bib_number: participant
        for participant in session.execute(
            select(Participant).where(
                Participant.event_id == event.id,
                Participant.bib_number.in_([row["bib_number"] for row in cleaned_rows]),
            )
        ).scalars()
    }

    inserted_rows = 0
    updated_rows = 0
    deduped_by_bib = {row["bib_number"]: row for row in cleaned_rows}

    for bib_number, row in deduped_by_bib.items():
        participant = existing.get(bib_number)
        if participant is None:
            session.add(
                Participant(
                    event_id=event.id,
                    bib_number=bib_number,
                    full_name=row["full_name"],
                )
            )
            inserted_rows += 1
            continue
        if participant.full_name != row["full_name"]:
            participant.full_name = row["full_name"]
            updated_rows += 1

    session.commit()
    duplicate_rows = len(cleaned_rows) - len(deduped_by_bib)
    return ParticipantUploadResult(
        processed_rows=len(deduped_by_bib),
        inserted_rows=inserted_rows,
        updated_rows=updated_rows,
        skipped_rows=skipped_rows + duplicate_rows,
    )


def add_participant(session: Session, *, event: Event, bib_number: str, full_name: str) -> Participant:
    normalized_bib = bib_number.strip()
    normalized_name = full_name.strip()
    if not normalized_bib or not normalized_name:
        raise ValueError("Bib number and full name are required.")

    existing = (
        session.query(Participant)
        .filter(Participant.event_id == event.id, Participant.bib_number == normalized_bib)
        .one_or_none()
    )
    if existing is not None:
        raise ValueError("That bib number already exists for this event.")

    participant = Participant(event_id=event.id, bib_number=normalized_bib, full_name=normalized_name)
    session.add(participant)
    session.commit()
    session.refresh(participant)
    return participant


def update_participant(
    session: Session,
    *,
    participant: Participant,
    bib_number: str,
    full_name: str,
) -> Participant:
    normalized_bib = bib_number.strip()
    normalized_name = full_name.strip()
    if not normalized_bib or not normalized_name:
        raise ValueError("Bib number and full name are required.")

    existing = (
        session.query(Participant)
        .filter(
            Participant.event_id == participant.event_id,
            Participant.bib_number == normalized_bib,
            Participant.id != participant.id,
        )
        .one_or_none()
    )
    if existing is not None:
        raise ValueError("That bib number already exists for this event.")

    participant.bib_number = normalized_bib
    participant.full_name = normalized_name
    session.commit()
    session.refresh(participant)
    return participant


def delete_participant(session: Session, *, participant: Participant) -> None:
    session.delete(participant)
    session.commit()


def delete_all_participants(session: Session, *, event: Event) -> int:
    count = (
        session.query(Participant)
        .filter(Participant.event_id == event.id)
        .delete(synchronize_session=False)
    )
    session.commit()
    return count


def acquire_admin_lock(session: Session, *, session_id: str) -> bool:
    lock = session.get(AdminSessionLock, ADMIN_LOCK_ID)
    now = utc_now()

    if lock is None:
        lock = AdminSessionLock(id=ADMIN_LOCK_ID, session_id=session_id, last_seen_at=now)
        session.add(lock)
        session.commit()
        return True

    if lock.session_id == session_id:
        lock.last_seen_at = now
        session.commit()
        return True

    if lock.last_seen_at < now - ADMIN_SESSION_TTL:
        lock.session_id = session_id
        lock.last_seen_at = now
        session.commit()
        return True

    return False


def force_admin_lock(session: Session, *, session_id: str) -> None:
    lock = session.get(AdminSessionLock, ADMIN_LOCK_ID)
    now = utc_now()

    if lock is None:
        lock = AdminSessionLock(id=ADMIN_LOCK_ID, session_id=session_id, last_seen_at=now)
        session.add(lock)
    else:
        lock.session_id = session_id
        lock.last_seen_at = now
    session.commit()
