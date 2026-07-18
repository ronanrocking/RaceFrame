from __future__ import annotations

import io
import math
import uuid

import pytest
from PIL import Image
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.face import normalize_embedding_payload, score_bib_evidence, upsert_final_photo_participant_match
from app.models import Event, Participant, Photo, PhotoJob, PhotoParticipantMatch, PhotoTextDetection
from app.photographer import ingest_photo_upload
from app.uploads import validate_image_bytes


@pytest.fixture()
def db() -> Session:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    Base.metadata.drop_all(engine)


def jpeg_bytes(size: tuple[int, int] = (120, 80)) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", size, color=(20, 40, 60)).save(output, format="JPEG")
    return output.getvalue()


def detection(text: str) -> PhotoTextDetection:
    return PhotoTextDetection(
        photo_id=uuid.uuid4(),
        photo_job_id=uuid.uuid4(),
        detected_text=text,
        normalized_text=text,
    )


def test_bib_matching_is_digit_boundary_safe() -> None:
    assert score_bib_evidence([detection("11042")], "1042").strength != "strong"
    exact = score_bib_evidence([detection("BIB 1042")], "1042")
    assert exact.strength == "strong"
    assert score_bib_evidence([detection("104")], "1042").strength == "weak"


def test_embedding_validation_requires_finite_512d_and_normalizes() -> None:
    assert normalize_embedding_payload([1.0] * 511) == []
    assert normalize_embedding_payload([float("nan")] * 512) == []
    normalized = normalize_embedding_payload([2.0] * 512)
    assert len(normalized) == 512
    assert math.isclose(sum(value * value for value in normalized), 1.0, rel_tol=1e-6)


def test_participant_lookup_uses_indexed_canonical_keys(db: Session) -> None:
    from app.main import find_event_participant

    event = Event(name="Race", slug="indexed-race", status="published")
    participant = Participant(event=event, bib_number="A-42", full_name="  Alice   Smith  ")
    db.add(participant)
    db.commit()

    assert participant.bib_lookup == "A42"
    assert participant.name_lookup == "alice smith"
    assert find_event_participant(db, event_id=event.id, query="A 42") == participant
    assert find_event_participant(db, event_id=event.id, query=" alice smith ") == participant


def test_image_validation_uses_contents_not_claimed_mime() -> None:
    content = jpeg_bytes()
    result = validate_image_bytes(
        content,
        file_name="race.jpg",
        declared_content_type="image/jpeg",
        max_bytes=1_000_000,
    )
    assert result.width == 120
    assert result.height == 80
    assert len(result.sha256) == 64

    with pytest.raises(ValueError, match="declared image type"):
        validate_image_bytes(
            content,
            file_name="race.jpg",
            declared_content_type="image/png",
            max_bytes=1_000_000,
        )


def test_atomic_final_match_upsert_merges_ocr_and_face(db: Session) -> None:
    event = Event(name="Race", slug="race", status="published")
    db.add(event)
    db.flush()
    participant = Participant(event_id=event.id, bib_number="42", full_name="Runner")
    photo = Photo(
        event_id=event.id,
        original_object_key="events/photo.jpg",
        file_name="photo.jpg",
        content_type="image/jpeg",
        file_size=10,
        status="processing",
    )
    db.add_all((participant, photo))
    db.flush()

    upsert_final_photo_participant_match(
        db,
        event_id=event.id,
        photo_id=photo.id,
        participant_id=participant.id,
        match_source="ocr",
        matched_value="42",
        confidence=0.8,
    )
    upsert_final_photo_participant_match(
        db,
        event_id=event.id,
        photo_id=photo.id,
        participant_id=participant.id,
        match_source="face",
        matched_value="0.91",
        confidence=0.91,
    )
    db.commit()

    match = db.scalar(select(PhotoParticipantMatch))
    assert match is not None
    assert match.match_source == "ocr+face"
    assert match.confidence == pytest.approx(0.91)
    assert db.query(PhotoParticipantMatch).count() == 1


def test_photo_jobs_are_published_only_after_storage_succeeds(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    event = Event(name="Race", slug="race", status="published")
    db.add(event)
    db.commit()
    stored: list[str] = []

    monkeypatch.setattr("app.photographer.upload_original_photo", lambda **kwargs: stored.append(kwargs["object_key"]))
    monkeypatch.setattr("app.photographer.upload_thumbnail_photo", lambda **kwargs: stored.append(kwargs["object_key"]))

    result = ingest_photo_upload(
        db,
        event=event,
        file_name="photo.jpg",
        content_type="image/jpeg",
        content=jpeg_bytes(),
        idempotency_key="test-request-1",
    )
    assert result.success
    assert len(stored) == 2
    assert db.query(Photo).count() == 1
    assert db.query(PhotoJob).count() == 2


def test_storage_failure_does_not_publish_jobs(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    event = Event(name="Race", slug="race", status="published")
    db.add(event)
    db.commit()

    def fail_upload(**_kwargs):
        raise TimeoutError("storage unavailable")

    monkeypatch.setattr("app.photographer.upload_original_photo", fail_upload)
    monkeypatch.setattr("app.photographer.delete_object", lambda **_kwargs: None)
    result = ingest_photo_upload(
        db,
        event=event,
        file_name="photo.jpg",
        content_type="image/jpeg",
        content=jpeg_bytes(),
    )
    assert not result.success
    assert db.query(Photo).count() == 0
    assert db.query(PhotoJob).count() == 0
