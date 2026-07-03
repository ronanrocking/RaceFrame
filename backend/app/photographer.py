from __future__ import annotations

import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from .config import settings
from .models import Event, Participant, Photo, PhotoJob, PhotoParticipantMatch, PhotoTextDetection


PUBLISHED_STATUS = "published"
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
IMAGE_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp"}


@dataclass
class OCRTextDetectionResult:
    detected_text: str
    normalized_text: str
    confidence: float | None
    bounding_box_json: dict | None


@dataclass
class OCRResult:
    detections: list[OCRTextDetectionResult]
    raw_response_json: dict


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
class UserPhotoListItem:
    photo: Photo
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


def list_event_photo_items(session: Session, *, event_id: uuid.UUID) -> list[PhotographerPhotoListItem]:
    photos = (
        session.execute(
            select(Photo)
            .where(Photo.event_id == event_id)
            .options(
                selectinload(Photo.jobs),
                selectinload(Photo.detections),
                selectinload(Photo.matches).selectinload(PhotoParticipantMatch.participant),
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
                photo_url=safe_photo_access_url(photo.original_object_key),
            )
        )
    return items


def list_user_photo_items(session: Session) -> list[UserPhotoListItem]:
    photos = (
        session.execute(
            select(Photo)
            .options(
                selectinload(Photo.event),
                selectinload(Photo.detections),
            )
            .order_by(Photo.created_at.desc())
        )
        .scalars()
        .all()
    )

    return [UserPhotoListItem(photo=photo, photo_url=safe_photo_access_url(photo.original_object_key)) for photo in photos]


def search_event_photo_items(
    session: Session,
    *,
    event: Event,
    search_term: str,
) -> tuple[list[UserSearchPhotoItem], list[Participant]]:
    normalized_search_term = " ".join(search_term.strip().lower().split())
    normalized_bib_token = normalize_match_token(search_term)
    if not normalized_search_term and not normalized_bib_token:
        return [], []

    participants = (
        session.execute(
            select(Participant).where(Participant.event_id == event.id).order_by(Participant.full_name.asc())
        )
        .scalars()
        .all()
    )

    matched_participants = [
        participant
        for participant in participants
        if (
            normalized_bib_token
            and normalize_match_token(participant.bib_number) == normalized_bib_token
        )
        or (
            normalized_search_term
            and normalized_search_term in " ".join(participant.full_name.lower().split())
        )
    ]
    matched_participant_ids = [participant.id for participant in matched_participants]
    photo_items_by_id: dict[uuid.UUID, UserSearchPhotoItem] = {}

    if matched_participant_ids:
        matched_photos = (
            session.execute(
                select(Photo)
                .join(PhotoParticipantMatch, PhotoParticipantMatch.photo_id == Photo.id)
                .where(
                    Photo.event_id == event.id,
                    PhotoParticipantMatch.participant_id.in_(matched_participant_ids),
                )
                .options(
                    selectinload(Photo.matches).selectinload(PhotoParticipantMatch.participant),
                    selectinload(Photo.detections),
                )
                .distinct()
                .order_by(Photo.created_at.desc())
            )
            .scalars()
            .all()
        )

        for photo in matched_photos:
            photo_items_by_id[photo.id] = UserSearchPhotoItem(
                photo=photo,
                photo_url=safe_photo_access_url(photo.original_object_key),
                matched_participants=[
                    match.participant
                    for match in photo.matches
                    if match.participant_id in matched_participant_ids and match.participant is not None
                ],
                direct_match_values=[],
            )

    if normalized_bib_token:
        direct_match_photos = (
            session.execute(
                select(Photo)
                .join(PhotoTextDetection, PhotoTextDetection.photo_id == Photo.id)
                .where(
                    Photo.event_id == event.id,
                    PhotoTextDetection.normalized_text.contains(normalized_bib_token),
                )
                .options(
                    selectinload(Photo.matches).selectinload(PhotoParticipantMatch.participant),
                    selectinload(Photo.detections),
                )
                .distinct()
                .order_by(Photo.created_at.desc())
            )
            .scalars()
            .all()
        )

        for photo in direct_match_photos:
            direct_match_values = [
                detection.detected_text
                for detection in photo.detections
                if normalized_bib_token in normalize_match_token(detection.detected_text)
            ]
            existing_item = photo_items_by_id.get(photo.id)
            if existing_item is not None:
                existing_item.direct_match_values = direct_match_values
                continue
            photo_items_by_id[photo.id] = UserSearchPhotoItem(
                photo=photo,
                photo_url=safe_photo_access_url(photo.original_object_key),
                matched_participants=[],
                direct_match_values=direct_match_values,
            )

    items = sorted(
        photo_items_by_id.values(),
        key=lambda item: item.photo.created_at,
        reverse=True,
    )
    return items, matched_participants


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
            selectinload(Photo.matches).selectinload(PhotoParticipantMatch.participant),
        )
    ).scalar_one_or_none()


def safe_photo_access_url(object_key: str) -> str | None:
    try:
        return generate_photo_access_url(object_key)
    except Exception:  # noqa: BLE001
        return None


def ingest_photo_upload(
    session: Session,
    *,
    event: Event,
    file_name: str,
    content_type: str,
    content: bytes,
) -> UploadedPhotoResult:
    normalized_file_name = file_name.strip()
    if not normalized_file_name:
        return UploadedPhotoResult(file_name="Unnamed file", photo=None, success=False, message="Missing file name.")

    try:
        normalized_content_type = validate_image_upload(
            file_name=normalized_file_name,
            content_type=content_type,
            file_size=len(content),
        )
    except ValueError as exc:
        return UploadedPhotoResult(file_name=normalized_file_name, photo=None, success=False, message=str(exc))

    photo_id = uuid.uuid4()
    object_key = build_original_object_key(event_id=event.id, photo_id=photo_id, file_name=normalized_file_name)
    photo = Photo(
        id=photo_id,
        event_id=event.id,
        original_object_key=object_key,
        thumbnail_object_key=None,
        file_name=normalized_file_name,
        content_type=normalized_content_type,
        file_size=len(content),
        status="uploaded",
    )
    job = PhotoJob(
        photo_id=photo_id,
        job_type="ocr",
        status="queued",
        attempt_count=0,
    )
    session.add(photo)
    session.add(job)
    session.commit()
    session.refresh(photo)
    session.refresh(job)

    try:
        upload_original_photo(content=content, object_key=object_key, content_type=normalized_content_type)
        process_photo_pipeline(session, photo=photo, job=job, image_bytes=content)
        session.refresh(photo)
        return UploadedPhotoResult(
            file_name=normalized_file_name,
            photo=photo,
            success=photo.status == "ready",
            message="Uploaded and processed." if photo.status == "ready" else "Upload completed, but processing failed.",
        )
    except Exception as exc:  # noqa: BLE001
        mark_photo_job_failed(session, photo=photo, job=job, error_message=str(exc))
        session.refresh(photo)
        return UploadedPhotoResult(
            file_name=normalized_file_name,
            photo=photo,
            success=False,
            message=str(exc),
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


def upload_original_photo(*, content: bytes, object_key: str, content_type: str) -> None:
    r2_client = get_r2_client()
    r2_client.put_object(
        Bucket=settings.r2_bucket_name,
        Key=object_key,
        Body=content,
        ContentType=content_type,
    )


def generate_photo_access_url(object_key: str) -> str | None:
    if not is_r2_configured():
        return None

    r2_client = get_r2_client()
    return r2_client.generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.r2_bucket_name, "Key": object_key},
        ExpiresIn=settings.r2_presigned_url_ttl_seconds,
    )


def download_photo_bytes(object_key: str) -> bytes:
    if not is_r2_configured():
        raise RuntimeError("R2 storage is not configured. Downloads are unavailable.")

    response = get_r2_client().get_object(
        Bucket=settings.r2_bucket_name,
        Key=object_key,
    )
    return response["Body"].read()


def delete_photo(session: Session, *, photo: Photo) -> None:
    delete_object_if_possible(photo.original_object_key)
    if photo.thumbnail_object_key:
        delete_object_if_possible(photo.thumbnail_object_key)
    session.delete(photo)
    session.commit()


def delete_all_event_photos(session: Session, *, event: Event) -> int:
    photos = (
        session.execute(
            select(Photo).where(Photo.event_id == event.id)
        )
        .scalars()
        .all()
    )

    deleted_count = 0
    for photo in photos:
        delete_object_if_possible(photo.original_object_key)
        if photo.thumbnail_object_key:
            delete_object_if_possible(photo.thumbnail_object_key)
        session.delete(photo)
        deleted_count += 1

    session.commit()
    return deleted_count


def process_photo_pipeline(session: Session, *, photo: Photo, job: PhotoJob, image_bytes: bytes) -> None:
    job.status = "processing"
    job.attempt_count += 1
    job.error_message = None
    photo.status = "processing"
    session.commit()

    ocr_result = run_google_ocr(image_bytes=image_bytes)
    stored_detections = [
        PhotoTextDetection(
            photo_id=photo.id,
            photo_job_id=job.id,
            detected_text=detection.detected_text,
            normalized_text=detection.normalized_text,
            confidence=detection.confidence,
            bounding_box_json=detection.bounding_box_json,
        )
        for detection in ocr_result.detections
    ]
    session.add_all(stored_detections)
    session.commit()

    job.raw_response_json = ocr_result.raw_response_json
    create_participant_matches(session, photo=photo, detections=stored_detections)

    job.status = "completed"
    job.finished_at = utc_now()
    photo.status = "ready"
    session.commit()


def rebuild_event_photo_matches(session: Session, *, event: Event) -> int:
    session.query(PhotoParticipantMatch).filter(PhotoParticipantMatch.event_id == event.id).delete(synchronize_session=False)
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

    existing_participant_ids = {
        participant_id
        for participant_id in session.execute(
            select(PhotoParticipantMatch.participant_id).where(PhotoParticipantMatch.photo_id == photo.id)
        ).scalars()
    }

    matched_any = False
    for detection in detections:
        for candidate_value in extract_match_candidates(detection.detected_text):
            participant = participants_by_bib.get(candidate_value)
            if participant is None or participant.id in existing_participant_ids:
                continue

            session.add(
                PhotoParticipantMatch(
                    event_id=photo.event_id,
                    photo_id=photo.id,
                    participant_id=participant.id,
                    match_source="ocr",
                    matched_value=candidate_value,
                    confidence=detection.confidence,
                    status="auto_matched",
                )
            )
            existing_participant_ids.add(participant.id)
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
    return re.sub(r"[^A-Z0-9]+", "", value.upper())


def normalize_detected_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().upper()


def run_google_ocr(*, image_bytes: bytes) -> OCRResult:
    if not settings.google_application_credentials:
        raise RuntimeError("Google OCR is not configured. Set GOOGLE_APPLICATION_CREDENTIALS for the backend.")

    try:
        from google.cloud import vision
        from google.protobuf.json_format import MessageToDict
    except ImportError as exc:  # pragma: no cover - handled at runtime
        raise RuntimeError("Google OCR dependency is missing. Install google-cloud-vision.") from exc

    client = vision.ImageAnnotatorClient()
    image = vision.Image(content=image_bytes)
    response = client.document_text_detection(image=image)
    if response.error.message:
        raise RuntimeError(f"Google OCR failed: {response.error.message}")

    return OCRResult(
        detections=extract_google_ocr_detections(response),
        raw_response_json=MessageToDict(response._pb, preserving_proto_field_name=True),
    )


def extract_google_ocr_detections(response) -> list[OCRTextDetectionResult]:
    detections: list[OCRTextDetectionResult] = []
    seen_keys: set[tuple[str, tuple[tuple[int, int], ...]]] = set()

    full_text_annotation = getattr(response, "full_text_annotation", None)
    if full_text_annotation and full_text_annotation.pages:
        for page_index, page in enumerate(full_text_annotation.pages):
            for block_index, block in enumerate(page.blocks):
                for paragraph_index, paragraph in enumerate(block.paragraphs):
                    for word_index, word in enumerate(paragraph.words):
                        detected_text = "".join(symbol.text for symbol in word.symbols).strip()
                        normalized_text = normalize_detected_text(detected_text)
                        if not normalized_text:
                            continue

                        box_json = bounding_poly_to_json(
                            word.bounding_box,
                            source="word",
                            page_index=page_index,
                            block_index=block_index,
                            paragraph_index=paragraph_index,
                            word_index=word_index,
                        )
                        dedupe_key = (normalized_text, vertices_key_from_box(box_json))
                        if dedupe_key in seen_keys:
                            continue
                        seen_keys.add(dedupe_key)

                        detections.append(
                            OCRTextDetectionResult(
                                detected_text=detected_text,
                                normalized_text=normalized_text,
                                confidence=float(word.confidence) if getattr(word, "confidence", None) is not None else None,
                                bounding_box_json=box_json,
                            )
                        )

    if detections:
        return sort_ocr_detections(detections)

    annotations = list(getattr(response, "text_annotations", []) or [])
    for annotation_index, annotation in enumerate(annotations[1:], start=1):
        detected_text = annotation.description.strip()
        normalized_text = normalize_detected_text(detected_text)
        if not normalized_text:
            continue

        box_json = bounding_poly_to_json(
            annotation.bounding_poly,
            source="text_annotation",
            annotation_index=annotation_index,
        )
        dedupe_key = (normalized_text, vertices_key_from_box(box_json))
        if dedupe_key in seen_keys:
            continue
        seen_keys.add(dedupe_key)

        detections.append(
            OCRTextDetectionResult(
                detected_text=detected_text,
                normalized_text=normalized_text,
                confidence=None,
                bounding_box_json=box_json,
            )
        )

    return sort_ocr_detections(detections)


def bounding_poly_to_json(bounding_poly, **metadata) -> dict | None:
    if not bounding_poly or not getattr(bounding_poly, "vertices", None):
        return metadata or None

    vertices = [
        {"x": int(getattr(vertex, "x", 0) or 0), "y": int(getattr(vertex, "y", 0) or 0)}
        for vertex in bounding_poly.vertices
    ]
    xs = [vertex["x"] for vertex in vertices]
    ys = [vertex["y"] for vertex in vertices]
    payload = {
        "vertices": vertices,
        "left": min(xs) if xs else 0,
        "top": min(ys) if ys else 0,
        "right": max(xs) if xs else 0,
        "bottom": max(ys) if ys else 0,
        "width": (max(xs) - min(xs)) if xs else 0,
        "height": (max(ys) - min(ys)) if ys else 0,
    }
    payload.update(metadata)
    return payload


def vertices_key_from_box(box_json: dict | None) -> tuple[tuple[int, int], ...]:
    if not box_json:
        return ()
    return tuple(
        (int(vertex.get("x", 0)), int(vertex.get("y", 0)))
        for vertex in box_json.get("vertices", [])
    )


def sort_ocr_detections(detections: list[OCRTextDetectionResult]) -> list[OCRTextDetectionResult]:
    return sorted(
        detections,
        key=lambda detection: (
            int((detection.bounding_box_json or {}).get("top", 0)),
            int((detection.bounding_box_json or {}).get("left", 0)),
            detection.detected_text,
        ),
    )


def mark_photo_job_failed(session: Session, *, photo: Photo, job: PhotoJob, error_message: str) -> None:
    job.status = "failed"
    job.finished_at = utc_now()
    job.error_message = error_message[:2000]
    photo.status = "failed"
    session.commit()


def is_r2_configured() -> bool:
    return all(
        [
            settings.r2_bucket_name,
            settings.r2_endpoint,
            settings.r2_access_key_id,
            settings.r2_secret_access_key,
        ]
    )


def get_r2_client():
    if not is_r2_configured():
        raise RuntimeError("R2 storage is not configured. Set the CLOUDFLARE_R2_* environment variables.")

    try:
        import boto3
    except ImportError as exc:  # pragma: no cover - handled at runtime
        raise RuntimeError("R2 dependency is missing. Install boto3.") from exc

    return boto3.client(
        "s3",
        endpoint_url=settings.r2_endpoint,
        aws_access_key_id=settings.r2_access_key_id,
        aws_secret_access_key=settings.r2_secret_access_key,
        region_name="auto",
    )


def delete_object_if_possible(object_key: str) -> None:
    if not object_key or not is_r2_configured():
        return

    try:
        r2_client = get_r2_client()
        r2_client.delete_object(Bucket=settings.r2_bucket_name, Key=object_key)
    except Exception:  # noqa: BLE001
        # Best effort cleanup. The DB delete should still succeed even if object removal is unavailable.
        return
