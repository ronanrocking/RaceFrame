from __future__ import annotations

import io
import logging
import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from .config import settings
from .face import exact_bib_match_values, score_bib_evidence, upsert_final_photo_participant_match
from .models import (
    Event,
    FaceParticipantMatch,
    FaceSearchResult,
    FaceSearchSession,
    Participant,
    Photo,
    PhotoFaceDetection,
    PhotoJob,
    PhotoParticipantMatch,
    PhotoTextDetection,
)
from .maintenance import enqueue_object_deletion, process_object_deletions
from .participant_lookup import normalize_bib_lookup
from .storage import (
    delete_object,
    generate_download_url,
    get_object_body,
    get_object_storage_client,
    is_object_storage_configured,
    put_object,
)
from .uploads import validate_image_bytes


PUBLISHED_STATUS = "published"
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
IMAGE_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp"}
logger = logging.getLogger(__name__)


@dataclass
class UploadedPhotoResult:
    file_name: str
    photo: Photo | None
    success: bool
    message: str


@dataclass
class PhotographerPhotoListItem:
    photo: Photo
    latest_job: PhotoJob | None
    photo_url: str | None


@dataclass
class UserEventListItem:
    event: Event
    participant_count: int
    photo_count: int


@dataclass
class UserSearchPhotoItem:
    photo: Photo
    photo_url: str | None
    matched_participants: list[Participant]
    direct_match_values: list[str]
    evidence_labels: list[str]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def list_published_events(session: Session) -> list[Event]:
    return (
        session.execute(
            select(Event)
            .where(Event.status == PUBLISHED_STATUS)
            .order_by(Event.event_date.asc(), Event.created_at.desc())
        )
        .scalars()
        .all()
    )


def list_user_events(session: Session) -> list[UserEventListItem]:
    rows = session.execute(
        select(
            Event,
            func.count(func.distinct(Participant.id)),
            func.count(func.distinct(Photo.id)),
        )
        .outerjoin(Participant, Participant.event_id == Event.id)
        .outerjoin(Photo, Photo.event_id == Event.id)
        .where(Event.status == PUBLISHED_STATUS)
        .group_by(Event.id)
        .order_by(Event.event_date.asc(), Event.created_at.desc())
    ).all()

    return [
        UserEventListItem(
            event=row[0],
            participant_count=int(row[1] or 0),
            photo_count=int(row[2] or 0),
        )
        for row in rows
    ]


def get_published_event(session: Session, event_id: str) -> Event | None:
    try:
        parsed_id = uuid.UUID(str(event_id))
    except ValueError:
        return None

    return session.execute(
        select(Event).where(Event.id == parsed_id, Event.status == PUBLISHED_STATUS)
    ).scalar_one_or_none()


def count_event_participants(session: Session, *, event_id: uuid.UUID) -> int:
    return int(
        session.execute(
            select(func.count(Participant.id)).where(Participant.event_id == event_id)
        ).scalar_one()
    )


def build_event_photo_stats(session: Session, *, event_id: uuid.UUID) -> dict[str, int | float]:
    total_count, total_file_size, thumbnail_count = session.execute(
        select(
            func.count(Photo.id),
            func.coalesce(func.sum(Photo.file_size), 0),
            func.count(Photo.thumbnail_object_key),
        ).where(Photo.event_id == event_id)
    ).one()

    photo_status_counts = {
        status: int(count)
        for status, count in session.execute(
            select(Photo.status, func.count(Photo.id))
            .where(Photo.event_id == event_id)
            .group_by(Photo.status)
        ).all()
    }
    job_status_counts = {
        status: int(count)
        for status, count in session.execute(
            select(PhotoJob.status, func.count(PhotoJob.id))
            .join(Photo, Photo.id == PhotoJob.photo_id)
            .where(Photo.event_id == event_id)
            .group_by(PhotoJob.status)
        ).all()
    }
    job_type_counts = {
        job_type: int(count)
        for job_type, count in session.execute(
            select(PhotoJob.job_type, func.count(PhotoJob.id))
            .join(Photo, Photo.id == PhotoJob.photo_id)
            .where(Photo.event_id == event_id)
            .group_by(PhotoJob.job_type)
        ).all()
    }

    text_detection_count = int(
        session.execute(
            select(func.count(PhotoTextDetection.id))
            .join(Photo, Photo.id == PhotoTextDetection.photo_id)
            .where(Photo.event_id == event_id)
        ).scalar_one()
        or 0
    )
    face_detection_count = int(
        session.execute(
            select(func.count(PhotoFaceDetection.id))
            .join(Photo, Photo.id == PhotoFaceDetection.photo_id)
            .where(Photo.event_id == event_id)
        ).scalar_one()
        or 0
    )
    participant_match_count = int(
        session.execute(
            select(func.count(PhotoParticipantMatch.id))
            .where(PhotoParticipantMatch.event_id == event_id)
        ).scalar_one()
        or 0
    )
    face_match_count = int(
        session.execute(
            select(func.count(FaceParticipantMatch.id))
            .where(FaceParticipantMatch.event_id == event_id)
        ).scalar_one()
        or 0
    )

    total = int(total_count or 0)
    size_bytes = int(total_file_size or 0)
    thumbnails = int(thumbnail_count or 0)
    return {
        "total": total,
        "uploaded": photo_status_counts.get("uploaded", 0),
        "processing": photo_status_counts.get("processing", 0),
        "ready": photo_status_counts.get("ready", 0),
        "failed": photo_status_counts.get("failed", 0),
        "queued_jobs": job_status_counts.get("queued", 0),
        "processing_jobs": job_status_counts.get("processing", 0),
        "completed_jobs": job_status_counts.get("completed", 0),
        "failed_jobs": job_status_counts.get("failed", 0),
        "ocr_jobs": job_type_counts.get("ocr", 0),
        "face_scan_jobs": job_type_counts.get("face_photo_scan", 0),
        "text_detections": text_detection_count,
        "face_detections": face_detection_count,
        "participant_matches": participant_match_count,
        "face_matches": face_match_count,
        "thumbnails": thumbnails,
        "missing_thumbnails": max(total - thumbnails, 0),
        "total_file_size_bytes": size_bytes,
        "total_file_size_mb": round(size_bytes / (1024 * 1024), 1),
        "average_file_size_mb": round((size_bytes / max(total, 1)) / (1024 * 1024), 2),
    }


def list_event_photo_items(session: Session, *, event_id: uuid.UUID) -> list[PhotographerPhotoListItem]:
    photos = (
        session.execute(
            select(Photo)
            .where(Photo.event_id == event_id)
            .options(
                selectinload(Photo.jobs),
                selectinload(Photo.detections),
                selectinload(Photo.face_detections),
                selectinload(Photo.matches).selectinload(PhotoParticipantMatch.participant),
                selectinload(Photo.face_matches).selectinload(FaceParticipantMatch.participant),
            )
            .order_by(Photo.created_at.desc())
        )
        .scalars()
        .all()
    )

    items: list[PhotographerPhotoListItem] = []
    for photo in photos:
        items.append(
            PhotographerPhotoListItem(
                photo=photo,
                latest_job=photo.jobs[0] if photo.jobs else None,
                photo_url=safe_photo_preview_url(photo),
            )
        )
    return items


def list_face_search_photo_items(
    session: Session,
    *,
    event: Event,
    face_session_id: str,
) -> tuple[FaceSearchSession | None, list[UserSearchPhotoItem]]:
    try:
        parsed_id = uuid.UUID(str(face_session_id))
    except ValueError:
        return None, []

    search_session = session.execute(
        select(FaceSearchSession)
        .where(FaceSearchSession.id == parsed_id, FaceSearchSession.event_id == event.id)
        .options(
            selectinload(FaceSearchSession.images),
            selectinload(FaceSearchSession.jobs),
            selectinload(FaceSearchSession.participant),
        )
    ).scalar_one_or_none()
    if search_session is None:
        return None, []

    participant = search_session.participant
    if participant is None:
        return search_session, list_legacy_face_search_photo_items(session, event=event, search_session=search_session)

    is_bib_only_session = len(search_session.images) == 0
    score_rows = session.execute(
        select(FaceSearchResult.photo_id, func.max(FaceSearchResult.similarity_score).label("best_score"))
        .where(FaceSearchResult.search_session_id == search_session.id, FaceSearchResult.event_id == event.id)
        .group_by(FaceSearchResult.photo_id)
        .order_by(func.max(FaceSearchResult.similarity_score).desc())
        .limit(settings.max_search_results)
    ).all()
    best_face_score_by_photo = {photo_id: float(score) for photo_id, score in score_rows}

    ocr_photo_ids = list(
        session.execute(
            select(PhotoParticipantMatch.photo_id)
            .where(
                PhotoParticipantMatch.event_id == event.id,
                PhotoParticipantMatch.participant_id == participant.id,
                PhotoParticipantMatch.match_source.in_(("ocr", "ocr+face")),
            )
            .order_by(PhotoParticipantMatch.created_at.desc())
            .limit(settings.max_search_results)
        ).scalars()
    )
    candidate_photo_ids = list(dict.fromkeys([*best_face_score_by_photo, *ocr_photo_ids]))[: settings.max_search_results]
    if not candidate_photo_ids:
        return search_session, []

    photos = (
        session.execute(
            select(Photo)
            .where(Photo.event_id == event.id, Photo.id.in_(candidate_photo_ids))
            .options(
                selectinload(Photo.matches).selectinload(PhotoParticipantMatch.participant),
                selectinload(Photo.detections),
                selectinload(Photo.face_detections),
                selectinload(Photo.face_matches).selectinload(FaceParticipantMatch.participant),
            )
            .order_by(Photo.created_at.desc())
        )
        .scalars()
        .unique()
        .all()
    )

    items: list[UserSearchPhotoItem] = []
    for photo in photos:
        face_score = best_face_score_by_photo.get(photo.id)
        face_strength = classify_face_score(face_score)
        if is_bib_only_session:
            exact_bib_values = exact_bib_match_values(photo.detections, participant.bib_number)
            bib_strength = "strong" if exact_bib_values else "none"
            if face_strength != "strong" and bib_strength != "strong":
                continue
            bib_values = exact_bib_values
        else:
            bib_evidence = score_bib_evidence(photo.detections, participant.bib_number)
            if not should_accept_hybrid_result(face_strength=face_strength, bib_strength=bib_evidence.strength):
                continue
            bib_strength = bib_evidence.strength
            bib_values = bib_evidence.matched_values

        labels = build_hybrid_evidence_labels(
            face_score=face_score,
            face_strength=face_strength,
            bib_strength=bib_strength,
            bib_values=bib_values,
        )
        labels.extend(build_photo_evidence_labels(photo, matched_participant_ids=[participant.id]))
        items.append(
            UserSearchPhotoItem(
                photo=photo,
                photo_url=safe_photo_preview_url(photo),
                matched_participants=[participant],
                direct_match_values=list(bib_values),
                evidence_labels=labels,
            )
        )

    items.sort(
        key=lambda item: (
            best_face_score_by_photo.get(item.photo.id, 0.0),
            1
            if (
                exact_bib_match_values(item.photo.detections, participant.bib_number)
                if is_bib_only_session
                else score_bib_evidence(item.photo.detections, participant.bib_number).strength == "strong"
            )
            else 0,
            item.photo.created_at,
        ),
        reverse=True,
    )
    return search_session, items


def list_legacy_face_search_photo_items(
    session: Session,
    *,
    event: Event,
    search_session: FaceSearchSession,
) -> list[UserSearchPhotoItem]:
    result_rows = (
        session.execute(
            select(FaceSearchResult)
            .where(FaceSearchResult.search_session_id == search_session.id, FaceSearchResult.event_id == event.id)
            .options(
                selectinload(FaceSearchResult.photo).selectinload(Photo.matches).selectinload(PhotoParticipantMatch.participant),
                selectinload(FaceSearchResult.photo).selectinload(Photo.detections),
                selectinload(FaceSearchResult.photo).selectinload(Photo.face_detections),
                selectinload(FaceSearchResult.photo).selectinload(Photo.face_matches).selectinload(FaceParticipantMatch.participant),
            )
            .order_by(FaceSearchResult.similarity_score.desc(), FaceSearchResult.created_at.desc())
            .limit(settings.max_search_results)
        )
        .scalars()
        .unique()
        .all()
    )
    best_by_photo: dict[uuid.UUID, tuple[Photo, float]] = {}
    for result in result_rows:
        if result.photo is None or result.similarity_score < settings.face_strong_similarity_threshold:
            continue
        existing = best_by_photo.get(result.photo_id)
        if existing is None or result.similarity_score > existing[1]:
            best_by_photo[result.photo_id] = (result.photo, result.similarity_score)

    items: list[UserSearchPhotoItem] = []
    for photo, best_score in sorted(best_by_photo.values(), key=lambda item: (item[1], item[0].created_at), reverse=True):
        labels = [f"Strong face score {float(best_score):.2f}"]
        labels.extend(build_photo_evidence_labels(photo, matched_participant_ids=[]))
        items.append(
            UserSearchPhotoItem(
                photo=photo,
                photo_url=safe_photo_preview_url(photo),
                matched_participants=[],
                direct_match_values=[],
                evidence_labels=labels,
            )
        )
    return items


def classify_face_score(score: float | None) -> str:
    if score is None:
        return "none"
    if score >= settings.face_strong_similarity_threshold:
        return "strong"
    if score >= settings.face_medium_similarity_threshold:
        return "medium"
    if score >= settings.face_candidate_similarity_threshold:
        return "weak"
    return "none"


def should_accept_hybrid_result(*, face_strength: str, bib_strength: str) -> bool:
    if face_strength == "strong" or bib_strength == "strong":
        return True
    if face_strength == "medium" and bib_strength == "weak":
        return True
    return False


def build_hybrid_evidence_labels(
    *,
    face_score: float | None,
    face_strength: str,
    bib_strength: str,
    bib_values: tuple[str, ...],
) -> list[str]:
    labels = []
    if bib_strength == "strong":
        labels.append("Strong bib match")
    elif bib_strength == "weak":
        labels.append("Partial bib match")

    if face_score is not None and face_strength != "none":
        labels.append(f"{face_strength.title()} face score {face_score:.2f}")
    elif bib_strength == "strong":
        labels.append("Accepted by bib; no usable face match")

    if bib_values:
        labels.append(f"Matched text: {', '.join(bib_values)}")
    return labels


def get_event_photo(session: Session, *, event_id: uuid.UUID, photo_id: str) -> Photo | None:
    try:
        parsed_id = uuid.UUID(str(photo_id))
    except ValueError:
        return None

    return session.execute(
        select(Photo)
        .where(Photo.id == parsed_id, Photo.event_id == event_id)
        .options(
            selectinload(Photo.jobs),
            selectinload(Photo.detections),
            selectinload(Photo.face_detections),
            selectinload(Photo.matches).selectinload(PhotoParticipantMatch.participant),
            selectinload(Photo.face_matches).selectinload(FaceParticipantMatch.participant),
        )
    ).scalar_one_or_none()


def build_photo_evidence_labels(photo: Photo, *, matched_participant_ids: list[uuid.UUID]) -> list[str]:
    labels: list[str] = []
    for match in photo.matches:
        if matched_participant_ids and match.participant_id not in matched_participant_ids:
            continue
        if match.match_source == "ocr":
            labels.append(f"OCR bib {match.matched_value}")
        elif match.match_source == "face":
            labels.append(f"Face score {match.confidence:.2f}" if match.confidence is not None else "Face match")
        elif match.match_source == "ocr+face":
            labels.append(f"OCR + face {match.confidence:.2f}" if match.confidence is not None else "OCR + face")
        else:
            labels.append(f"{match.match_source.upper()} match")

    if photo.face_detections:
        labels.append(f"{len(photo.face_detections)} face{'s' if len(photo.face_detections) != 1 else ''} detected")
    return labels


def safe_photo_access_url(object_key: str) -> str | None:
    try:
        return generate_photo_access_url(object_key)
    except Exception:  # noqa: BLE001
        return None


def safe_photo_preview_url(photo: Photo) -> str | None:
    object_key = photo.thumbnail_object_key or photo.original_object_key
    return safe_photo_access_url(object_key)


def ingest_photo_upload(
    session: Session,
    *,
    event: Event,
    file_name: str,
    content_type: str,
    content: bytes,
    idempotency_key: str | None = None,
) -> UploadedPhotoResult:
    normalized_file_name = file_name.strip()
    if not normalized_file_name:
        return UploadedPhotoResult(file_name="Unnamed file", photo=None, success=False, message="Missing file name.")

    try:
        validate_image_upload(
            file_name=normalized_file_name,
            content_type=content_type,
            file_size=len(content),
        )
        image = validate_image_bytes(
            content,
            file_name=normalized_file_name,
            declared_content_type=content_type,
            max_bytes=settings.max_photo_upload_bytes,
        )
        normalized_content_type = image.content_type
    except ValueError as exc:
        return UploadedPhotoResult(file_name=normalized_file_name, photo=None, success=False, message=str(exc))

    normalized_idempotency_key = (idempotency_key or "").strip()[:128] or None
    existing_filters = [Photo.event_id == event.id]
    if normalized_idempotency_key:
        existing_filters.append(Photo.idempotency_key == normalized_idempotency_key)
    else:
        existing_filters.append(Photo.checksum_sha256 == image.sha256)
    existing = session.execute(select(Photo).where(*existing_filters)).scalar_one_or_none()
    if existing is not None:
        return UploadedPhotoResult(
            file_name=normalized_file_name,
            photo=existing,
            success=True,
            message="This photo was already accepted; the existing upload was reused.",
        )

    backlog = session.execute(
        select(func.count(PhotoJob.id)).where(PhotoJob.status.in_(("queued", "processing")))
    ).scalar_one()
    if backlog >= settings.max_photo_job_backlog:
        return UploadedPhotoResult(
            file_name=normalized_file_name,
            photo=None,
            success=False,
            message="The processing queue is currently full. Please retry later.",
        )

    photo_id = uuid.uuid4()
    object_key = build_original_object_key(event_id=event.id, photo_id=photo_id, file_name=normalized_file_name)
    thumbnail_object_key = build_thumbnail_object_key(event_id=event.id, photo_id=photo_id, file_name=normalized_file_name)
    try:
        thumbnail_bytes = create_photo_thumbnail(content=content)
        upload_original_photo(content=content, object_key=object_key, content_type=normalized_content_type)
        upload_thumbnail_photo(content=thumbnail_bytes, object_key=thumbnail_object_key)
    except Exception:  # noqa: BLE001
        logger.exception("Photo object upload failed", extra={"object_key": object_key})
        for uploaded_key in (thumbnail_object_key, object_key):
            try:
                delete_object(object_key=uploaded_key)
            except Exception:  # noqa: BLE001
                logger.exception("Photo upload compensation failed", extra={"object_key": uploaded_key})
        return UploadedPhotoResult(
            file_name=normalized_file_name,
            photo=None,
            success=False,
            message="The photo could not be stored. Please retry later.",
        )

    photo = Photo(
        id=photo_id,
        event_id=event.id,
        original_object_key=object_key,
        thumbnail_object_key=thumbnail_object_key,
        file_name=normalized_file_name,
        content_type=normalized_content_type,
        file_size=len(content),
        status="processing",
        checksum_sha256=image.sha256,
        idempotency_key=normalized_idempotency_key,
    )
    ocr_job = PhotoJob(
        photo_id=photo_id,
        job_type="ocr",
        status="queued",
        attempt_count=0,
        max_attempts=settings.worker_max_attempts,
    )
    face_job = PhotoJob(
        photo_id=photo_id,
        job_type="face_photo_scan",
        status="queued",
        attempt_count=0,
        max_attempts=settings.worker_max_attempts,
    )
    try:
        session.add(photo)
        session.add(ocr_job)
        session.add(face_job)
        session.commit()
        session.refresh(photo)
        return UploadedPhotoResult(
            file_name=normalized_file_name,
            photo=photo,
            success=True,
            message="Uploaded and queued for OCR and face recognition.",
        )
    except Exception:  # noqa: BLE001
        session.rollback()
        logger.exception("Photo database publish failed")
        for uploaded_key in (thumbnail_object_key, object_key):
            try:
                delete_object(object_key=uploaded_key)
            except Exception:  # noqa: BLE001
                logger.exception("Photo database compensation failed", extra={"object_key": uploaded_key})
        return UploadedPhotoResult(
            file_name=normalized_file_name,
            photo=None,
            success=False,
            message="The photo could not be queued. Please retry later.",
        )


def validate_image_upload(*, file_name: str, content_type: str, file_size: int) -> str:
    extension = Path(file_name).suffix.lower()
    if extension not in IMAGE_EXTENSIONS:
        raise ValueError("Upload JPG, PNG, or WEBP images only.")

    if file_size <= 0:
        raise ValueError("Uploaded file is empty.")

    if file_size > settings.max_photo_upload_bytes:
        max_mb = settings.max_photo_upload_bytes // (1024 * 1024)
        raise ValueError(f"Image exceeds the {max_mb} MB upload limit.")

    normalized_content_type = content_type.strip().lower() if content_type else ""
    if normalized_content_type and normalized_content_type not in IMAGE_CONTENT_TYPES:
        raise ValueError("Unsupported image type. Use JPG, PNG, or WEBP.")

    if not normalized_content_type:
        return guess_content_type(extension)
    return normalized_content_type


def guess_content_type(extension: str) -> str:
    if extension in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if extension == ".png":
        return "image/png"
    return "image/webp"


def build_original_object_key(*, event_id: uuid.UUID, photo_id: uuid.UUID, file_name: str) -> str:
    extension = Path(file_name).suffix.lower() or ".jpg"
    safe_base_name = re.sub(r"[^a-zA-Z0-9._-]+", "-", Path(file_name).stem).strip("-") or "photo"
    return f"events/{event_id}/originals/{photo_id}-{safe_base_name}{extension}"


def build_thumbnail_object_key(*, event_id: uuid.UUID, photo_id: uuid.UUID, file_name: str) -> str:
    safe_base_name = re.sub(r"[^a-zA-Z0-9._-]+", "-", Path(file_name).stem).strip("-") or "photo"
    return f"events/{event_id}/thumbnails/{photo_id}-{safe_base_name}.jpg"


def create_photo_thumbnail(*, content: bytes) -> bytes:
    from PIL import Image, ImageOps

    max_edge = max(128, int(settings.photo_thumbnail_max_edge))
    quality = min(95, max(40, int(settings.photo_thumbnail_quality)))
    with Image.open(io.BytesIO(content)) as image:
        image = ImageOps.exif_transpose(image)
        image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)

        if image.mode in {"RGBA", "LA"} or (image.mode == "P" and "transparency" in image.info):
            alpha_source = image.convert("RGBA")
            background = Image.new("RGB", alpha_source.size, (255, 255, 255))
            background.paste(alpha_source, mask=alpha_source.getchannel("A"))
            image = background
        else:
            image = image.convert("RGB")

        output = io.BytesIO()
        image.save(output, format="JPEG", quality=quality, optimize=True, progressive=True)
        return output.getvalue()


def upload_original_photo(*, content: bytes, object_key: str, content_type: str) -> None:
    put_object(object_key=object_key, content=content, content_type=content_type)


def upload_thumbnail_photo(*, content: bytes, object_key: str) -> None:
    put_object(
        object_key=object_key,
        content=content,
        content_type="image/jpeg",
        cache_control="private, max-age=31536000, immutable",
    )


def generate_photo_access_url(object_key: str, *, download_name: str | None = None) -> str | None:
    if not is_r2_configured():
        return None
    return generate_download_url(object_key=object_key, download_name=download_name)


def download_photo_bytes(object_key: str) -> bytes:
    if not is_r2_configured():
        raise RuntimeError("R2 storage is not configured. Downloads are unavailable.")

    body, _length, _content_type = get_object_body(object_key=object_key)
    return body.read()


def delete_photo(session: Session, *, photo: Photo) -> None:
    cleanup_photo_identity_data(session, photo_ids=[photo.id])
    enqueue_object_deletion(session, photo.original_object_key)
    enqueue_object_deletion(session, photo.thumbnail_object_key)
    session.delete(photo)
    session.commit()
    process_object_deletions(session, limit=2)


def delete_all_event_photos(session: Session, *, event: Event) -> int:
    photos = (
        session.execute(
            select(Photo).where(Photo.event_id == event.id)
        )
        .scalars()
        .all()
    )

    cleanup_photo_identity_data(session, photo_ids=[photo.id for photo in photos])
    deleted_count = 0
    for photo in photos:
        enqueue_object_deletion(session, photo.original_object_key)
        enqueue_object_deletion(session, photo.thumbnail_object_key)
        session.delete(photo)
        deleted_count += 1

    session.commit()
    process_object_deletions(session, limit=min(deleted_count * 2, settings.deletion_retry_batch_size))
    return deleted_count


def cleanup_photo_identity_data(session: Session, *, photo_ids: Iterable[uuid.UUID]) -> None:
    ids = list(photo_ids)
    if not ids:
        return

    face_detection_ids = list(
        session.execute(
            select(PhotoFaceDetection.id).where(PhotoFaceDetection.photo_id.in_(ids))
        ).scalars()
    )
    if face_detection_ids:
        session.query(FaceSearchResult).filter(FaceSearchResult.photo_face_detection_id.in_(face_detection_ids)).delete(synchronize_session=False)
        session.query(FaceParticipantMatch).filter(FaceParticipantMatch.photo_face_detection_id.in_(face_detection_ids)).delete(synchronize_session=False)

    session.query(FaceSearchResult).filter(FaceSearchResult.photo_id.in_(ids)).delete(synchronize_session=False)
    session.query(FaceParticipantMatch).filter(FaceParticipantMatch.photo_id.in_(ids)).delete(synchronize_session=False)
    session.query(PhotoParticipantMatch).filter(PhotoParticipantMatch.photo_id.in_(ids)).delete(synchronize_session=False)
    session.query(PhotoTextDetection).filter(PhotoTextDetection.photo_id.in_(ids)).delete(synchronize_session=False)
    session.query(PhotoFaceDetection).filter(PhotoFaceDetection.photo_id.in_(ids)).delete(synchronize_session=False)
    session.query(PhotoJob).filter(PhotoJob.photo_id.in_(ids)).delete(synchronize_session=False)


def rebuild_event_photo_matches(session: Session, *, event: Event) -> int:
    existing_matches = (
        session.execute(
            select(PhotoParticipantMatch).where(PhotoParticipantMatch.event_id == event.id)
        )
        .scalars()
        .all()
    )
    for match in existing_matches:
        if match.match_source == "ocr":
            session.delete(match)
        elif match.match_source == "ocr+face":
            match.match_source = "face"
            match.matched_value = "face-preserved-after-ocr-rebuild"
    session.commit()

    photos = (
        session.execute(
            select(Photo)
            .where(Photo.event_id == event.id)
            .options(selectinload(Photo.detections))
            .order_by(Photo.created_at.desc())
        )
        .scalars()
        .all()
    )

    rebuilt_count = 0
    for photo in photos:
        if not photo.detections:
            continue
        create_participant_matches(session, photo=photo, detections=photo.detections)
        rebuilt_count += 1

    return rebuilt_count


def create_participant_matches(
    session: Session,
    *,
    photo: Photo,
    detections: Iterable[PhotoTextDetection],
) -> None:
    participants = (
        session.execute(
            select(Participant).where(Participant.event_id == photo.event_id)
        )
        .scalars()
        .all()
    )
    participants_by_bib = {
        normalize_match_token(participant.bib_number): participant
        for participant in participants
        if normalize_match_token(participant.bib_number)
    }

    matched_any = False
    for detection in detections:
        for candidate_value in extract_match_candidates(detection.detected_text):
            participant = participants_by_bib.get(candidate_value)
            if participant is None:
                continue

            upsert_final_photo_participant_match(
                session,
                event_id=photo.event_id,
                photo_id=photo.id,
                participant_id=participant.id,
                match_source="ocr",
                matched_value=candidate_value,
                confidence=detection.confidence,
            )
            matched_any = True

    if matched_any:
        session.commit()


def extract_match_candidates(value: str) -> list[str]:
    tokens = re.findall(r"[A-Z0-9]+", value.upper())
    seen: set[str] = set()
    candidates: list[str] = []

    raw_candidate = normalize_match_token(value)
    if raw_candidate:
        tokens.insert(0, raw_candidate)

    for token in tokens:
        normalized = normalize_match_token(token)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        candidates.append(normalized)
    return candidates


def normalize_match_token(value: str) -> str:
    return normalize_bib_lookup(value)


def is_r2_configured() -> bool:
    return is_object_storage_configured()


def get_r2_client():
    return get_object_storage_client()
