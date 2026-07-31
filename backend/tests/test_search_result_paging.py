from __future__ import annotations

import uuid

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.face import enqueue_bib_only_face_search_session
from app.models import BibSearchJob, Event, Participant, Photo, SearchSessionPhotoResult
from app.photographer import list_face_search_photo_items, search_session_allows_photo


def make_session_factory():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def test_bib_search_is_enqueued_without_running_the_matcher() -> None:
    factory = make_session_factory()
    with factory() as session:
        event = Event(name="Race", slug="race", status="published")
        participant = Participant(event=event, bib_number="42", full_name="Runner")
        session.add_all((event, participant))
        session.commit()

        search_session = enqueue_bib_only_face_search_session(session, event_id=event.id, participant=participant)

        assert search_session.status == "queued"
        job = session.query(BibSearchJob).filter_by(search_session_id=search_session.id).one()
        assert job.status == "queued"
        assert job.participant_id == participant.id


def test_materialized_result_page_is_cursor_scoped_and_authorizes_one_photo() -> None:
    factory = make_session_factory()
    with factory() as session:
        event = Event(name="Race", slug="race", status="published")
        participant = Participant(event=event, bib_number="42", full_name="Runner")
        session.add_all((event, participant))
        session.commit()
        search_session = enqueue_bib_only_face_search_session(session, event_id=event.id, participant=participant)
        photos = [
            Photo(
                event=event,
                original_object_key=f"events/{event.id}/original-{index}.jpg",
                file_name=f"{index}.jpg",
                content_type="image/jpeg",
                file_size=1,
                status="ready",
            )
            for index in range(3)
        ]
        session.add_all(photos)
        session.flush()
        session.add_all(
            SearchSessionPhotoResult(
                event_id=event.id,
                search_session_id=search_session.id,
                photo_id=photo.id,
                rank=index + 1,
                evidence_labels=["Strong bib match"],
            )
            for index, photo in enumerate(photos)
        )
        session.commit()

        first_session, first_page = list_face_search_photo_items(
            session, event=event, face_session_id=str(search_session.id)
        )
        assert first_session is not None
        assert [item.photo.id for item in first_page] == [photo.id for photo in photos]
        _, later_page = list_face_search_photo_items(
            session,
            event=event,
            face_session_id=str(search_session.id),
            cursor=first_page[0].result_cursor,
        )
        assert [item.photo.id for item in later_page] == [photos[1].id, photos[2].id]
        assert search_session_allows_photo(
            session, event_id=event.id, search_session_id=search_session.id, photo_id=str(photos[0].id)
        )
        assert not search_session_allows_photo(
            session, event_id=event.id, search_session_id=search_session.id, photo_id=str(uuid.uuid4())
        )
