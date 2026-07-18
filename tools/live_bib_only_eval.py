from __future__ import annotations

import json
import uuid

from sqlalchemy import select

from app.db import SessionLocal
from app.face import create_bib_only_face_search_session
from app.models import Event, Participant
from app.photographer import list_face_search_photo_items


EVENT_ID = uuid.UUID("97f47890-d71b-4b8c-be3d-9b0fd07ea364")


def bib_sort_key(participant: Participant) -> tuple[int, str]:
    try:
        return int(participant.bib_number), participant.bib_number
    except ValueError:
        return 10**9, participant.bib_number


def main() -> None:
    with SessionLocal() as session:
        event = session.get(Event, EVENT_ID)
        if event is None:
            raise SystemExit(f"Event not found: {EVENT_ID}")

        participants = (
            session.execute(
                select(Participant)
                .where(Participant.event_id == EVENT_ID)
                .order_by(Participant.bib_number.asc())
            )
            .scalars()
            .all()
        )
        participants = sorted(participants, key=bib_sort_key)

        results = []
        for participant in participants:
            try:
                search_session, seed_count = create_bib_only_face_search_session(
                    session,
                    event_id=EVENT_ID,
                    participant=participant,
                )
                _, items = list_face_search_photo_items(
                    session,
                    event=event,
                    face_session_id=str(search_session.id),
                )
                results.append(
                    {
                        "bib_number": participant.bib_number,
                        "participant_id": str(participant.id),
                        "full_name": participant.full_name,
                        "face_session_id": str(search_session.id),
                        "seed_count": seed_count,
                        "images": sorted({item.photo.file_name for item in items}),
                    }
                )
            except Exception as exc:  # noqa: BLE001
                results.append(
                    {
                        "bib_number": participant.bib_number,
                        "participant_id": str(participant.id),
                        "full_name": participant.full_name,
                        "error": str(exc),
                        "images": [],
                    }
                )

        print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
