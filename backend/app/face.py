from __future__ import annotations

import math
import heapq
import logging
import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import case, func, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, selectinload

from .config import settings
from .storage import delete_object, put_object
from .uploads import validate_image_bytes
from .models import (
    FaceParticipantMatch,
    FaceSearchImage,
    FaceSearchJob,
    FaceSearchResult,
    FaceSearchSession,
    Participant,
    ParticipantFaceEmbedding,
    ParticipantFaceImage,
    ParticipantFaceJob,
    Photo,
    PhotoFaceDetection,
    PhotoJob,
    PhotoParticipantMatch,
    PhotoTextDetection,
)


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
IMAGE_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp"}
ASCII_TOKEN_PATTERN = re.compile(r"[A-Z0-9]+")
ASCII_DIGIT_PATTERN = re.compile(r"[0-9]+")
logger = logging.getLogger(__name__)


@dataclass
class FaceSelfieUploadResult:
    file_name: str
    face_image: ParticipantFaceImage | None
    success: bool
    message: str


@dataclass
class FaceSearchUploadResult:
    file_name: str
    search_session: FaceSearchSession | None
    search_image: FaceSearchImage | None
    success: bool
    message: str


@dataclass(frozen=True)
class BibEvidence:
    strength: str
    matched_values: tuple[str, ...]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def validate_selfie_upload(*, file_name: str, content_type: str, file_size: int) -> str:
    extension = Path(file_name).suffix.lower()
    if extension not in IMAGE_EXTENSIONS:
        raise ValueError("Upload JPG, PNG, or WEBP selfies only.")

    if file_size <= 0:
        raise ValueError("Uploaded selfie is empty.")

    if file_size > settings.max_selfie_upload_bytes:
        max_mb = settings.max_selfie_upload_bytes // (1024 * 1024)
        raise ValueError(f"Selfie exceeds the {max_mb} MB upload limit.")

    normalized_content_type = content_type.strip().lower() if content_type else ""
    if normalized_content_type and normalized_content_type not in IMAGE_CONTENT_TYPES:
        raise ValueError("Unsupported selfie type. Use JPG, PNG, or WEBP.")

    if normalized_content_type:
        return normalized_content_type
    if extension in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if extension == ".png":
        return "image/png"
    return "image/webp"


def build_face_selfie_object_key(
    *,
    event_id: uuid.UUID,
    participant_id: uuid.UUID,
    face_image_id: uuid.UUID,
    file_name: str,
) -> str:
    extension = Path(file_name).suffix.lower() or ".jpg"
    safe_base_name = re.sub(r"[^a-zA-Z0-9._-]+", "-", Path(file_name).stem).strip("-") or "selfie"
    return f"events/{event_id}/participants/{participant_id}/faces/{face_image_id}-{safe_base_name}{extension}"


def build_face_search_object_key(
    *,
    event_id: uuid.UUID,
    search_session_id: uuid.UUID,
    search_image_id: uuid.UUID,
    file_name: str,
) -> str:
    extension = Path(file_name).suffix.lower() or ".jpg"
    safe_base_name = re.sub(r"[^a-zA-Z0-9._-]+", "-", Path(file_name).stem).strip("-") or "face-photo"
    return f"events/{event_id}/face-search/{search_session_id}/{search_image_id}-{safe_base_name}{extension}"


def upload_r2_object(*, content: bytes, object_key: str, content_type: str) -> None:
    put_object(object_key=object_key, content=content, content_type=content_type)


def ingest_participant_selfie_upload(
    session: Session,
    *,
    participant: Participant,
    file_name: str,
    content_type: str,
    content: bytes,
) -> FaceSelfieUploadResult:
    normalized_file_name = file_name.strip()
    if not normalized_file_name:
        return FaceSelfieUploadResult(file_name="Unnamed selfie", face_image=None, success=False, message="Missing file name.")

    try:
        validate_selfie_upload(
            file_name=normalized_file_name,
            content_type=content_type,
            file_size=len(content),
        )
        image = validate_image_bytes(
            content,
            file_name=normalized_file_name,
            declared_content_type=content_type,
            max_bytes=settings.max_selfie_upload_bytes,
        )
        normalized_content_type = image.content_type
    except ValueError as exc:
        return FaceSelfieUploadResult(file_name=normalized_file_name, face_image=None, success=False, message=str(exc))

    face_image_id = uuid.uuid4()
    object_key = build_face_selfie_object_key(
        event_id=participant.event_id,
        participant_id=participant.id,
        face_image_id=face_image_id,
        file_name=normalized_file_name,
    )
    try:
        upload_r2_object(content=content, object_key=object_key, content_type=normalized_content_type)
    except Exception:  # noqa: BLE001
        logger.exception("Participant face image storage failed", extra={"object_key": object_key})
        return FaceSelfieUploadResult(
            file_name=normalized_file_name,
            face_image=None,
            success=False,
            message="The selfie could not be stored. Please try again later.",
        )

    face_image = ParticipantFaceImage(
        id=face_image_id,
        event_id=participant.event_id,
        participant_id=participant.id,
        object_key=object_key,
        file_name=normalized_file_name,
        content_type=normalized_content_type,
        file_size=len(content),
        status="queued",
    )
    job = ParticipantFaceJob(
        event_id=participant.event_id,
        participant_id=participant.id,
        face_image_id=face_image_id,
        status="queued",
        attempt_count=0,
        max_attempts=settings.worker_max_attempts,
    )
    try:
        session.add(face_image)
        session.add(job)
        session.commit()
        session.refresh(face_image)
    except Exception:  # noqa: BLE001
        session.rollback()
        logger.exception("Participant face image database publish failed")
        try:
            delete_object(object_key=object_key)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to compensate participant face object", extra={"object_key": object_key})
        return FaceSelfieUploadResult(
            file_name=normalized_file_name,
            face_image=None,
            success=False,
            message="The selfie could not be queued. Please try again later.",
        )

    return FaceSelfieUploadResult(
        file_name=normalized_file_name,
        face_image=face_image,
        success=True,
        message="Selfie uploaded and queued for face enrollment.",
    )


def ingest_face_search_upload(
    session: Session,
    *,
    event,
    search_session: FaceSearchSession | None,
    participant: Participant | None = None,
    file_name: str,
    content_type: str,
    content: bytes,
) -> FaceSearchUploadResult:
    normalized_file_name = file_name.strip()
    if not normalized_file_name:
        return FaceSearchUploadResult(
            file_name="Unnamed face photo",
            search_session=search_session,
            search_image=None,
            success=False,
            message="Missing file name.",
        )

    active_jobs = session.execute(
        select(func.count(FaceSearchJob.id)).where(FaceSearchJob.status.in_(("queued", "processing")))
    ).scalar_one()
    if active_jobs >= settings.max_face_search_backlog:
        return FaceSearchUploadResult(
            file_name=normalized_file_name,
            search_session=search_session,
            search_image=None,
            success=False,
            message="The face-search queue is full. Please retry later.",
        )

    try:
        validate_selfie_upload(
            file_name=normalized_file_name,
            content_type=content_type,
            file_size=len(content),
        )
        image = validate_image_bytes(
            content,
            file_name=normalized_file_name,
            declared_content_type=content_type,
            max_bytes=settings.max_selfie_upload_bytes,
        )
        normalized_content_type = image.content_type
    except ValueError as exc:
        return FaceSearchUploadResult(
            file_name=normalized_file_name,
            search_session=search_session,
            search_image=None,
            success=False,
            message=str(exc),
        )

    created_session = search_session is None
    if created_session:
        search_session = FaceSearchSession(
            event_id=event.id,
            participant_id=participant.id if participant is not None else None,
            status="queued",
            expires_at=utc_now() + timedelta(hours=max(1, settings.biometric_retention_hours)),
        )
        session.add(search_session)
        session.flush()

    search_image_id = uuid.uuid4()
    object_key = build_face_search_object_key(
        event_id=event.id,
        search_session_id=search_session.id,
        search_image_id=search_image_id,
        file_name=normalized_file_name,
    )
    try:
        upload_r2_object(content=content, object_key=object_key, content_type=normalized_content_type)
    except Exception:  # noqa: BLE001
        session.rollback()
        logger.exception("Temporary face search image storage failed", extra={"object_key": object_key})
        return FaceSearchUploadResult(
            file_name=normalized_file_name,
            search_session=None if created_session else search_session,
            search_image=None,
            success=False,
            message="The selfie could not be stored. Please try again later.",
        )

    search_image = FaceSearchImage(
        id=search_image_id,
        event_id=event.id,
        search_session_id=search_session.id,
        object_key=object_key,
        file_name=normalized_file_name,
        content_type=normalized_content_type,
        file_size=len(content),
        status="queued",
    )
    job = FaceSearchJob(
        event_id=event.id,
        search_session_id=search_session.id,
        search_image_id=search_image_id,
        status="queued",
        attempt_count=0,
        max_attempts=settings.worker_max_attempts,
    )
    try:
        session.add(search_image)
        session.add(job)
        session.commit()
        session.refresh(search_session)
        session.refresh(search_image)
    except Exception:  # noqa: BLE001
        session.rollback()
        logger.exception("Temporary face search database publish failed")
        try:
            delete_object(object_key=object_key)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to compensate face search object", extra={"object_key": object_key})
        return FaceSearchUploadResult(
            file_name=normalized_file_name,
            search_session=None,
            search_image=None,
            success=False,
            message="The selfie could not be queued. Please try again later.",
        )

    return FaceSearchUploadResult(
        file_name=normalized_file_name,
        search_session=search_session,
        search_image=search_image,
        success=True,
        message="Face photo uploaded and queued for search.",
    )


def list_participant_face_images(session: Session, *, participant_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, list[ParticipantFaceImage]]:
    ids = list(participant_ids)
    if not ids:
        return {}

    rows = (
        session.execute(
            select(ParticipantFaceImage)
            .where(ParticipantFaceImage.participant_id.in_(ids))
            .order_by(ParticipantFaceImage.created_at.desc())
        )
        .scalars()
        .all()
    )
    grouped: dict[uuid.UUID, list[ParticipantFaceImage]] = {}
    for row in rows:
        grouped.setdefault(row.participant_id, []).append(row)
    return grouped


def upsert_final_photo_participant_match(
    session: Session,
    *,
    event_id: uuid.UUID,
    photo_id: uuid.UUID,
    participant_id: uuid.UUID,
    match_source: str,
    matched_value: str,
    confidence: float | None,
) -> None:
    matched_value = clamp_match_value(matched_value)
    table = PhotoParticipantMatch.__table__
    insert_factory = sqlite_insert if session.get_bind().dialect.name == "sqlite" else postgresql_insert
    statement = insert_factory(table).values(
        event_id=event_id,
        photo_id=photo_id,
        participant_id=participant_id,
        match_source=match_source,
        matched_value=matched_value,
        confidence=confidence,
        status="auto_matched",
    )
    excluded = statement.excluded
    merged_confidence = case(
        (table.c.confidence.is_(None), excluded.confidence),
        (excluded.confidence.is_(None), table.c.confidence),
        (table.c.confidence >= excluded.confidence, table.c.confidence),
        else_=excluded.confidence,
    )
    merged_source = case(
        (table.c.match_source == excluded.match_source, table.c.match_source),
        (table.c.match_source == "ocr+face", table.c.match_source),
        else_="ocr+face",
    )
    merged_value = case(
        (table.c.match_source == excluded.match_source, excluded.matched_value),
        else_=func.substr(
            table.c.matched_value + " | " + excluded.match_source + ":" + excluded.matched_value,
            1,
            255,
        ),
    )
    session.execute(
        statement.on_conflict_do_update(
            index_elements=[table.c.photo_id, table.c.participant_id],
            set_={
                "match_source": merged_source,
                "matched_value": merged_value,
                "confidence": merged_confidence,
                "status": "auto_matched",
            },
        )
    )


def clamp_match_value(value: str) -> str:
    normalized = str(value or "").strip()
    return normalized[:255] if normalized else "matched"


def compact_worker_diagnostics(raw_response_json: dict | None, **summary: object) -> dict:
    """Retain operational metadata, not duplicate provider payloads or biometric data."""
    allowed_keys = {
        "provider",
        "request_id",
        "model_name",
        "model_version",
        "image_width",
        "image_height",
        "resized_width",
        "resized_height",
        "processing_ms",
    }
    compact: dict[str, object] = {}
    if isinstance(raw_response_json, dict):
        for key in allowed_keys:
            value = raw_response_json.get(key)
            if isinstance(value, (str, int, float, bool)) and len(str(value)) <= 255:
                compact[key] = value
    compact.update(summary)
    return compact


def complete_photo_ocr_job(
    session: Session,
    *,
    job: PhotoJob,
    detections: list[dict],
    raw_response_json: dict | None,
) -> None:
    photo = job.photo
    session.query(PhotoTextDetection).filter(PhotoTextDetection.photo_job_id == job.id).delete(synchronize_session=False)

    stored_detections: list[PhotoTextDetection] = []
    for detection in detections:
        detected_text = str(detection.get("detected_text") or "").strip()
        normalized_text = str(detection.get("normalized_text") or "").strip()
        if not detected_text or not normalized_text:
            continue
        stored_detections.append(
            PhotoTextDetection(
                photo_id=photo.id,
                photo_job_id=job.id,
                detected_text=detected_text,
                normalized_text=normalized_text,
                confidence=detection.get("confidence"),
                bounding_box_json=detection.get("bounding_box_json"),
            )
        )

    session.add_all(stored_detections)
    session.flush()
    create_ocr_participant_matches(session, photo=photo, detections=stored_detections)

    job.status = "completed"
    job.raw_response_json = compact_worker_diagnostics(raw_response_json, detection_count=len(stored_detections))
    job.finished_at = utc_now()
    session.flush()
    mark_photo_ready_if_jobs_done(session, photo=photo)
    session.commit()


def complete_photo_face_job(
    session: Session,
    *,
    job: PhotoJob,
    face_detections: list[dict],
    raw_response_json: dict | None,
) -> None:
    photo = job.photo
    session.query(PhotoFaceDetection).filter(PhotoFaceDetection.photo_job_id == job.id).delete(synchronize_session=False)

    stored_faces: list[PhotoFaceDetection] = []
    for index, detection in enumerate(face_detections):
        embedding = normalize_embedding_payload(detection.get("embedding"))
        if not embedding:
            continue
        stored_faces.append(
            PhotoFaceDetection(
                event_id=photo.event_id,
                photo_id=photo.id,
                photo_job_id=job.id,
                face_index=int(detection.get("face_index", index)),
                embedding_json=embedding,
                bounding_box_json=detection.get("bounding_box_json"),
                detection_score=detection.get("detection_score"),
                quality_score=detection.get("quality_score"),
            )
        )

    session.add_all(stored_faces)
    session.flush()

    job.status = "completed"
    job.raw_response_json = compact_worker_diagnostics(raw_response_json, face_count=len(stored_faces))
    job.finished_at = utc_now()
    session.flush()
    mark_photo_ready_if_jobs_done(session, photo=photo)
    session.commit()


def complete_participant_face_job(
    session: Session,
    *,
    job: ParticipantFaceJob,
    embedding: list[float],
    bounding_box_json: dict | None,
    detection_score: float | None,
    quality_score: float | None,
    raw_response_json: dict | None,
) -> None:
    normalized_embedding = normalize_embedding_payload(embedding)
    if not normalized_embedding:
        raise ValueError("Face enrollment result did not include an embedding.")

    face_image = job.face_image
    session.add(
        ParticipantFaceEmbedding(
            event_id=job.event_id,
            participant_id=job.participant_id,
            face_image_id=job.face_image_id,
            embedding_json=normalized_embedding,
            bounding_box_json=bounding_box_json,
            detection_score=detection_score,
            quality_score=quality_score,
        )
    )
    face_image.status = "ready"
    face_image.error_message = None
    job.status = "completed"
    job.raw_response_json = compact_worker_diagnostics(raw_response_json, face_count=1)
    job.finished_at = utc_now()
    session.flush()
    create_face_participant_matches_for_participant(session, participant_id=job.participant_id, event_id=job.event_id)
    session.commit()


def fail_photo_job(session: Session, *, job: PhotoJob, error_message: str) -> None:
    job.status = "failed"
    job.error_message = error_message[:2000]
    job.finished_at = utc_now()
    session.flush()
    mark_photo_ready_if_jobs_done(session, photo=job.photo)
    session.commit()


def fail_participant_face_job(session: Session, *, job: ParticipantFaceJob, error_message: str) -> None:
    job.status = "failed"
    job.error_message = error_message[:2000]
    job.finished_at = utc_now()
    job.face_image.status = "failed"
    job.face_image.error_message = error_message[:2000]
    session.commit()


def complete_face_search_job(
    session: Session,
    *,
    job: FaceSearchJob,
    embedding: list[float],
    bounding_box_json: dict | None,
    detection_score: float | None,
    quality_score: float | None,
    raw_response_json: dict | None,
) -> None:
    normalized_embedding = normalize_embedding_payload(embedding)
    if not normalized_embedding:
        raise ValueError("Face search result did not include an embedding.")

    matched_count = create_face_search_results(
        session,
        event_id=job.event_id,
        search_session_id=job.search_session_id,
        embedding=normalized_embedding,
    )

    job.search_image.status = "ready"
    job.search_image.error_message = None
    job.status = "completed"
    job.raw_response_json = compact_worker_diagnostics(
        raw_response_json,
        detection_score=detection_score,
        quality_score=quality_score,
        matched_count=matched_count,
    )
    job.finished_at = utc_now()
    if job.search_session.participant_id:
        apply_adaptive_reinforcement(session, search_session=job.search_session)
    mark_face_search_session_done_if_jobs_done(session, search_session=job.search_session)
    session.commit()


def fail_face_search_job(session: Session, *, job: FaceSearchJob, error_message: str) -> None:
    job.status = "failed"
    job.error_message = error_message[:2000]
    job.finished_at = utc_now()
    job.search_image.status = "failed"
    job.search_image.error_message = error_message[:2000]
    mark_face_search_session_done_if_jobs_done(session, search_session=job.search_session)
    session.commit()


def create_bib_only_face_search_session(
    session: Session,
    *,
    event_id: uuid.UUID,
    participant: Participant,
) -> tuple[FaceSearchSession, int]:
    search_session = FaceSearchSession(
        event_id=event_id,
        participant_id=participant.id,
        status="processing",
        expires_at=utc_now() + timedelta(hours=max(1, settings.biometric_retention_hours)),
    )
    session.add(search_session)
    session.flush()

    seed_embeddings = select_bib_only_seed_embeddings(
        session,
        event_id=event_id,
        participant=participant,
    )
    for embedding in seed_embeddings[:3]:
        create_face_search_results(
            session,
            event_id=event_id,
            search_session_id=search_session.id,
            embedding=embedding,
        )

    if seed_embeddings:
        apply_adaptive_reinforcement(session, search_session=search_session, require_exact_bib=True)

    search_session.status = "completed"
    search_session.finished_at = utc_now()
    session.commit()
    session.refresh(search_session)
    return search_session, len(seed_embeddings)


def select_bib_only_seed_embeddings(
    session: Session,
    *,
    event_id: uuid.UUID,
    participant: Participant,
) -> list[list[float]]:
    candidate_photos = (
        session.execute(
            select(Photo)
            .where(Photo.event_id == event_id)
            .options(selectinload(Photo.detections), selectinload(Photo.face_detections))
            .order_by(Photo.created_at.desc())
        )
        .scalars()
        .unique()
        .all()
    )

    candidate_faces: list[PhotoFaceDetection] = []
    exact_bib_photo_count = 0
    usable_face_photo_count = 0
    single_photo_single_face_embedding: list[float] | None = None
    for photo in candidate_photos:
        if not has_exact_bib_match(photo.detections, participant.bib_number):
            continue

        exact_bib_photo_count += 1
        if exact_bib_photo_count > settings.bib_only_seed_photo_limit:
            break

        good_faces = sorted(
            [face for face in photo.face_detections if normalize_embedding_payload(face.embedding_json)],
            key=face_seed_quality,
            reverse=True,
        )
        if not good_faces:
            continue
        usable_face_photo_count += 1
        if len(good_faces) == 1:
            candidate_faces.append(good_faces[0])
            if exact_bib_photo_count == 1:
                single_photo_single_face_embedding = normalize_embedding_payload(good_faces[0].embedding_json)
        elif is_dominant_face(good_faces):
            candidate_faces.append(good_faces[0])
        else:
            candidate_faces.extend(good_faces[:4])

    consensus_embeddings = select_consensus_seed_embeddings(
        candidate_faces,
        support_photo_count=usable_face_photo_count,
    )
    if consensus_embeddings:
        return consensus_embeddings

    if exact_bib_photo_count == 1 and usable_face_photo_count == 1 and single_photo_single_face_embedding:
        return [single_photo_single_face_embedding]

    return []


def face_seed_quality(face: PhotoFaceDetection) -> float:
    box = face.bounding_box_json or {}
    area = float(box.get("width", 0) or 0) * float(box.get("height", 0) or 0)
    quality = float(face.quality_score or face.detection_score or 0.0)
    return area * max(quality, 0.25)


def is_dominant_face(faces: list[PhotoFaceDetection]) -> bool:
    if len(faces) < 2:
        return True
    best = face_seed_quality(faces[0])
    second = face_seed_quality(faces[1])
    return best > 0 and best >= second * 1.8


def select_cluster_seed_embeddings(faces: list[PhotoFaceDetection]) -> list[list[float]]:
    clusters: list[list[PhotoFaceDetection]] = []
    for face in faces:
        embedding = normalize_embedding_payload(face.embedding_json)
        if not embedding:
            continue
        best_cluster = None
        best_score = 0.0
        for cluster in clusters:
            score = max(cosine_similarity(embedding, other.embedding_json) for other in cluster)
            if score > best_score:
                best_score = score
                best_cluster = cluster
        if best_cluster is not None and best_score >= settings.face_reinforcement_similarity_threshold:
            best_cluster.append(face)
        else:
            clusters.append([face])

    eligible_clusters = []
    for cluster in clusters:
        photo_ids = {face.photo_id for face in cluster}
        if len(photo_ids) < 2:
            continue
        avg_quality = sum(face_seed_quality(face) for face in cluster) / len(cluster)
        eligible_clusters.append((len(photo_ids), avg_quality, cluster))

    if not eligible_clusters:
        return []
    eligible_clusters.sort(key=lambda item: (item[0], item[1]), reverse=True)
    if len(eligible_clusters) > 1 and eligible_clusters[0][0] == eligible_clusters[1][0]:
        if eligible_clusters[0][1] < eligible_clusters[1][1] * 1.35:
            return []

    best_cluster = sorted(eligible_clusters[0][2], key=face_seed_quality, reverse=True)
    return [best_cluster[0].embedding_json]


def select_consensus_seed_embeddings(
    faces: list[PhotoFaceDetection],
    *,
    support_photo_count: int,
) -> list[list[float]]:
    if not faces or support_photo_count <= 0:
        return []

    clusters = cluster_seed_faces(faces)
    eligible_clusters = []
    for cluster in clusters:
        photo_ids = {face.photo_id for face in cluster}
        if len(photo_ids) < settings.bib_only_seed_cluster_min_photos:
            continue
        avg_quality = sum(face_seed_quality(face) for face in cluster) / len(cluster)
        eligible_clusters.append((len(photo_ids), avg_quality, cluster))

    if not eligible_clusters:
        return []

    eligible_clusters.sort(key=lambda item: (item[0], item[1]), reverse=True)
    best_photo_count, _best_quality, best_cluster = eligible_clusters[0]
    required_support = max(
        settings.bib_only_seed_cluster_min_photos,
        math.ceil(support_photo_count * settings.bib_only_seed_cluster_majority_ratio),
    )
    if best_photo_count < required_support:
        return []

    if len(eligible_clusters) > 1:
        second_photo_count = eligible_clusters[1][0]
        has_clear_count_lead = best_photo_count >= second_photo_count + 2
        has_clear_ratio_lead = best_photo_count >= math.ceil(second_photo_count * settings.bib_only_seed_cluster_lead_ratio)
        if not has_clear_count_lead and not has_clear_ratio_lead:
            return []

    best_faces = sorted(best_cluster, key=face_seed_quality, reverse=True)
    embeddings = [average_face_embeddings(best_faces)]
    embeddings.extend(face.embedding_json for face in best_faces[:2])
    return dedupe_seed_embeddings(embeddings)[:3]


def cluster_seed_faces(faces: list[PhotoFaceDetection]) -> list[list[PhotoFaceDetection]]:
    clusters: list[list[PhotoFaceDetection]] = []
    for face in faces:
        embedding = normalize_embedding_payload(face.embedding_json)
        if not embedding:
            continue
        best_cluster = None
        best_score = 0.0
        for cluster in clusters:
            score = max(cosine_similarity(embedding, other.embedding_json) for other in cluster)
            if score > best_score:
                best_score = score
                best_cluster = cluster
        if best_cluster is not None and best_score >= settings.face_reinforcement_similarity_threshold:
            best_cluster.append(face)
        else:
            clusters.append([face])
    return clusters


def average_face_embeddings(faces: list[PhotoFaceDetection]) -> list[float]:
    embeddings = [normalize_embedding_payload(face.embedding_json) for face in faces]
    embeddings = [embedding for embedding in embeddings if embedding]
    if not embeddings:
        return []
    dimension = len(embeddings[0])
    if any(len(embedding) != dimension for embedding in embeddings):
        return []

    averaged = [sum(embedding[index] for embedding in embeddings) / len(embeddings) for index in range(dimension)]
    norm = math.sqrt(sum(value * value for value in averaged))
    if norm <= 0:
        return []
    return [value / norm for value in averaged]


def dedupe_seed_embeddings(embeddings: list[list[float]]) -> list[list[float]]:
    deduped: list[list[float]] = []
    for embedding in embeddings:
        normalized = normalize_embedding_payload(embedding)
        if not normalized:
            continue
        if any(cosine_similarity(normalized, existing) >= 0.995 for existing in deduped):
            continue
        deduped.append(normalized)
    return deduped


def mark_face_search_session_done_if_jobs_done(session: Session, *, search_session: FaceSearchSession) -> None:
    statuses = list(
        session.execute(
            select(FaceSearchJob.status).where(FaceSearchJob.search_session_id == search_session.id)
        ).scalars()
    )
    if statuses and all(status in {"completed", "failed", "dead_lettered"} for status in statuses):
        search_session.status = "completed" if any(status == "completed" for status in statuses) else "failed"
        search_session.finished_at = utc_now()
    elif statuses:
        search_session.status = "processing"


def create_face_search_results(
    session: Session,
    *,
    event_id: uuid.UUID,
    search_session_id: uuid.UUID,
    embedding: list[float],
) -> int:
    detection_count = session.execute(
        select(func.count(PhotoFaceDetection.id)).where(PhotoFaceDetection.event_id == event_id)
    ).scalar_one()
    if detection_count > settings.max_search_faces_per_event:
        raise RuntimeError("This event is still being indexed for face search. Please try again later.")

    # Keep only the strongest bounded candidates while streaming JSON embeddings from
    # PostgreSQL. This avoids hydrating a whole event and prevents a probe from writing
    # an unbounded result set. A vector index can replace this implementation later
    # without changing the caller contract.
    candidates: list[tuple[float, str, uuid.UUID, uuid.UUID]] = []
    rows = session.execute(
        select(
            PhotoFaceDetection.id,
            PhotoFaceDetection.photo_id,
            PhotoFaceDetection.embedding_json,
        )
        .where(PhotoFaceDetection.event_id == event_id)
        .execution_options(yield_per=500)
    )
    for detection_id, photo_id, candidate_embedding in rows:
        score = cosine_similarity(embedding, candidate_embedding)
        if score < settings.face_candidate_similarity_threshold:
            continue
        item = (score, str(detection_id), detection_id, photo_id)
        if len(candidates) < settings.max_search_results:
            heapq.heappush(candidates, item)
        elif score > candidates[0][0]:
            heapq.heapreplace(candidates, item)

    upserted_count = 0
    result_table = FaceSearchResult.__table__
    insert_factory = sqlite_insert if session.get_bind().dialect.name == "sqlite" else postgresql_insert
    for score, _stable_id, detection_id, photo_id in sorted(candidates, reverse=True):
        statement = insert_factory(result_table).values(
            event_id=event_id,
            search_session_id=search_session_id,
            photo_id=photo_id,
            photo_face_detection_id=detection_id,
            similarity_score=score,
        )
        excluded = statement.excluded
        best_score = case(
            (result_table.c.similarity_score >= excluded.similarity_score, result_table.c.similarity_score),
            else_=excluded.similarity_score,
        )
        session.execute(
            statement.on_conflict_do_update(
                index_elements=[result_table.c.search_session_id, result_table.c.photo_face_detection_id],
                set_={"similarity_score": best_score, "photo_id": excluded.photo_id},
            )
        )
        upserted_count += 1
    return upserted_count


def apply_adaptive_reinforcement(
    session: Session,
    *,
    search_session: FaceSearchSession,
    require_exact_bib: bool = False,
) -> int:
    participant_id = search_session.participant_id
    if participant_id is None:
        return 0

    participant = session.get(Participant, participant_id)
    if participant is None:
        return 0

    created_total = 0
    reinforced_detection_ids: set[uuid.UUID] = set()
    for _round in range(2):
        candidates = (
            session.execute(
                select(FaceSearchResult)
                .where(FaceSearchResult.search_session_id == search_session.id)
                .options(
                    selectinload(FaceSearchResult.photo).selectinload(Photo.detections),
                    selectinload(FaceSearchResult.photo_face_detection),
                )
                .order_by(FaceSearchResult.similarity_score.desc())
                .limit(settings.max_search_results)
            )
            .scalars()
            .all()
        )
        created_this_round = 0
        for result in candidates:
            if created_total >= settings.face_reinforcement_max_embeddings:
                return created_total

            photo = result.photo
            detection = result.photo_face_detection
            if photo is None or detection is None:
                continue

            if require_exact_bib:
                if not has_exact_bib_match(photo.detections, participant.bib_number):
                    continue
                if result.similarity_score < settings.face_reinforcement_similarity_threshold:
                    continue
            else:
                bib_evidence = score_bib_evidence(photo.detections, participant.bib_number)
                if not should_reinforce_face(face_score=result.similarity_score, bib_strength=bib_evidence.strength):
                    continue

            embedding = normalize_embedding_payload(detection.embedding_json)
            if not embedding or detection.id in reinforced_detection_ids:
                continue

            create_face_search_results(
                session,
                event_id=search_session.event_id,
                search_session_id=search_session.id,
                embedding=embedding,
            )
            reinforced_detection_ids.add(detection.id)
            created_total += 1
            created_this_round += 1

        if created_this_round == 0:
            break

    return created_total


def should_reinforce_face(*, face_score: float, bib_strength: str) -> bool:
    if face_score >= settings.face_strong_similarity_threshold:
        return True
    if bib_strength == "strong" and face_score >= settings.face_reinforcement_similarity_threshold:
        return True
    return False


def score_bib_evidence(detections: Iterable[PhotoTextDetection], bib_number: str) -> BibEvidence:
    target = normalize_match_token(bib_number)
    if not target:
        return BibEvidence(strength="none", matched_values=())

    weak_matches: list[str] = []
    for detection in detections:
        for candidate in extract_match_candidates(detection.detected_text):
            if not candidate:
                continue
            if candidate == target:
                return BibEvidence(strength="strong", matched_values=(detection.detected_text,))
            if is_partial_bib_match(candidate, target):
                weak_matches.append(detection.detected_text)

    if weak_matches:
        return BibEvidence(strength="weak", matched_values=tuple(weak_matches[:3]))
    return BibEvidence(strength="none", matched_values=())


def has_exact_bib_match(detections: Iterable[PhotoTextDetection], bib_number: str) -> bool:
    return bool(exact_bib_match_values(detections, bib_number))


def exact_bib_match_values(detections: Iterable[PhotoTextDetection], bib_number: str) -> tuple[str, ...]:
    target = normalize_match_token(bib_number)
    if not target:
        return ()

    matched_values: list[str] = []
    seen: set[str] = set()
    for detection in detections:
        for candidate in extract_match_candidates(detection.detected_text):
            if candidate != target or detection.detected_text in seen:
                continue
            seen.add(detection.detected_text)
            matched_values.append(detection.detected_text)
    return tuple(matched_values[:3])


def is_partial_bib_match(candidate: str, target: str) -> bool:
    if len(target) < 3 or len(candidate) < 2:
        return False
    if len(candidate) > len(target):
        return False
    if candidate in target and len(candidate) >= max(2, len(target) - 1):
        return True
    longest = longest_common_substring_length(candidate, target)
    return longest >= max(3, len(target) - 1)


def longest_common_substring_length(left: str, right: str) -> int:
    best = 0
    previous = [0] * (len(right) + 1)
    for left_char in left:
        current = [0] * (len(right) + 1)
        for index, right_char in enumerate(right, start=1):
            if left_char == right_char:
                current[index] = previous[index - 1] + 1
                best = max(best, current[index])
        previous = current
    return best


def mark_photo_ready_if_jobs_done(session: Session, *, photo: Photo) -> None:
    statuses = list(
        session.execute(
            select(PhotoJob.status).where(PhotoJob.photo_id == photo.id)
        ).scalars()
    )
    if statuses and all(status in {"completed", "failed", "dead_lettered"} for status in statuses):
        completed_count = sum(status == "completed" for status in statuses)
        if completed_count == len(statuses):
            photo.status = "ready"
        elif completed_count:
            photo.status = "partially_ready"
        else:
            photo.status = "failed"
    elif statuses:
        photo.status = "processing"


def create_ocr_participant_matches(
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


def create_face_participant_matches_for_detection(session: Session, *, detection: PhotoFaceDetection) -> int:
    embeddings = (
        session.execute(
            select(ParticipantFaceEmbedding).where(ParticipantFaceEmbedding.event_id == detection.event_id)
        )
        .scalars()
        .all()
    )
    return create_face_matches(session, detection=detection, participant_embeddings=embeddings)


def create_face_participant_matches_for_participant(
    session: Session,
    *,
    participant_id: uuid.UUID,
    event_id: uuid.UUID,
) -> int:
    detections = (
        session.execute(
            select(PhotoFaceDetection).where(PhotoFaceDetection.event_id == event_id)
        )
        .scalars()
        .all()
    )
    embeddings = (
        session.execute(
            select(ParticipantFaceEmbedding).where(
                ParticipantFaceEmbedding.event_id == event_id,
                ParticipantFaceEmbedding.participant_id == participant_id,
            )
        )
        .scalars()
        .all()
    )

    created_count = 0
    for detection in detections:
        created_count += create_face_matches(session, detection=detection, participant_embeddings=embeddings)
    return created_count


def create_face_matches(
    session: Session,
    *,
    detection: PhotoFaceDetection,
    participant_embeddings: Iterable[ParticipantFaceEmbedding],
) -> int:
    best_by_participant: dict[uuid.UUID, float] = {}
    for participant_embedding in participant_embeddings:
        score = cosine_similarity(detection.embedding_json, participant_embedding.embedding_json)
        if score < settings.face_match_similarity_threshold:
            continue
        current = best_by_participant.get(participant_embedding.participant_id)
        if current is None or score > current:
            best_by_participant[participant_embedding.participant_id] = score

    created_count = 0
    for participant_id, score in best_by_participant.items():
        table = FaceParticipantMatch.__table__
        insert_factory = sqlite_insert if session.get_bind().dialect.name == "sqlite" else postgresql_insert
        statement = insert_factory(table).values(
            event_id=detection.event_id,
            photo_id=detection.photo_id,
            photo_face_detection_id=detection.id,
            participant_id=participant_id,
            similarity_score=score,
            status="auto_matched",
        )
        excluded = statement.excluded
        best_score = case(
            (table.c.similarity_score >= excluded.similarity_score, table.c.similarity_score),
            else_=excluded.similarity_score,
        )
        session.execute(
            statement.on_conflict_do_update(
                index_elements=[table.c.photo_face_detection_id, table.c.participant_id],
                set_={"similarity_score": best_score, "status": "auto_matched"},
            )
        )
        created_count += 1

        upsert_final_photo_participant_match(
            session,
            event_id=detection.event_id,
            photo_id=detection.photo_id,
            participant_id=participant_id,
            match_source="face",
            matched_value=f"{score:.4f}",
            confidence=score,
        )

    return created_count


def normalize_embedding_payload(value: object) -> list[float]:
    if not isinstance(value, list) or len(value) != 512:
        return []
    embedding: list[float] = []
    for item in value:
        try:
            number = float(item)
        except (TypeError, ValueError):
            return []
        if not math.isfinite(number) or abs(number) > 100:
            return []
        embedding.append(number)
    norm = math.sqrt(sum(number * number for number in embedding))
    if not math.isfinite(norm) or norm <= 1e-12:
        return []
    return [number / norm for number in embedding]


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0

    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm <= 0 or right_norm <= 0:
        return 0.0
    # Floating-point rounding can produce 1.0000000000000002 for identical
    # normalized vectors; keep persisted scores inside their database domain.
    return max(-1.0, min(1.0, dot / (left_norm * right_norm)))


def extract_match_candidates(value: str) -> list[str]:
    normalized_value = value.upper()
    tokens = ASCII_TOKEN_PATTERN.findall(normalized_value)
    seen: set[str] = set()
    candidates: list[str] = []

    raw_candidate = normalize_match_token(value)
    if raw_candidate:
        tokens.insert(0, raw_candidate)
    tokens.extend(ASCII_DIGIT_PATTERN.findall(normalized_value))

    for token in tokens:
        normalized = normalize_match_token(token)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        candidates.append(normalized)
    return candidates


def normalize_match_token(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "", value).upper()
