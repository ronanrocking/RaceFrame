from __future__ import annotations

import math
import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .config import settings
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
    try:
        from .photographer import get_r2_client
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("R2 helper is unavailable.") from exc

    get_r2_client().put_object(
        Bucket=settings.r2_bucket_name,
        Key=object_key,
        Body=content,
        ContentType=content_type,
    )


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
        normalized_content_type = validate_selfie_upload(
            file_name=normalized_file_name,
            content_type=content_type,
            file_size=len(content),
        )
    except ValueError as exc:
        return FaceSelfieUploadResult(file_name=normalized_file_name, face_image=None, success=False, message=str(exc))

    face_image_id = uuid.uuid4()
    object_key = build_face_selfie_object_key(
        event_id=participant.event_id,
        participant_id=participant.id,
        face_image_id=face_image_id,
        file_name=normalized_file_name,
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
    )
    session.add(face_image)
    session.add(job)
    session.commit()
    session.refresh(face_image)

    try:
        upload_r2_object(content=content, object_key=object_key, content_type=normalized_content_type)
    except Exception as exc:  # noqa: BLE001
        face_image.status = "failed"
        face_image.error_message = str(exc)[:2000]
        job.status = "failed"
        job.error_message = str(exc)[:2000]
        job.finished_at = utc_now()
        session.commit()
        return FaceSelfieUploadResult(file_name=normalized_file_name, face_image=face_image, success=False, message=str(exc))

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

    try:
        normalized_content_type = validate_selfie_upload(
            file_name=normalized_file_name,
            content_type=content_type,
            file_size=len(content),
        )
    except ValueError as exc:
        return FaceSearchUploadResult(
            file_name=normalized_file_name,
            search_session=search_session,
            search_image=None,
            success=False,
            message=str(exc),
        )

    if search_session is None:
        search_session = FaceSearchSession(
            event_id=event.id,
            participant_id=participant.id if participant is not None else None,
            status="queued",
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
    )
    session.add(search_image)
    session.add(job)
    session.commit()
    session.refresh(search_session)
    session.refresh(search_image)

    try:
        upload_r2_object(content=content, object_key=object_key, content_type=normalized_content_type)
    except Exception as exc:  # noqa: BLE001
        search_session.status = "failed"
        search_session.error_message = str(exc)[:2000]
        search_image.status = "failed"
        search_image.error_message = str(exc)[:2000]
        job.status = "failed"
        job.error_message = str(exc)[:2000]
        job.finished_at = utc_now()
        session.commit()
        return FaceSearchUploadResult(
            file_name=normalized_file_name,
            search_session=search_session,
            search_image=search_image,
            success=False,
            message=str(exc),
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
    existing = session.execute(
        select(PhotoParticipantMatch).where(
            PhotoParticipantMatch.photo_id == photo_id,
            PhotoParticipantMatch.participant_id == participant_id,
        )
    ).scalar_one_or_none()
    if existing is None:
        existing = pending_photo_participant_match(
            session,
            photo_id=photo_id,
            participant_id=participant_id,
        )

    if existing is None:
        match = PhotoParticipantMatch(
            event_id=event_id,
            photo_id=photo_id,
            participant_id=participant_id,
            match_source=match_source,
            matched_value=matched_value,
            confidence=confidence,
            status="auto_matched",
        )
        session.add(match)
        return

    sources = {source.strip() for source in existing.match_source.split("+") if source.strip()}
    sources.add(match_source)
    existing.match_source = "+".join(source for source in ("ocr", "face") if source in sources)
    if existing.match_source == match_source:
        existing.matched_value = matched_value
    elif match_source not in existing.matched_value:
        existing.matched_value = clamp_match_value(f"{existing.matched_value} | {match_source}:{matched_value}")

    if confidence is not None:
        existing.confidence = max(existing.confidence or 0.0, confidence)


def pending_photo_participant_match(
    session: Session,
    *,
    photo_id: uuid.UUID,
    participant_id: uuid.UUID,
) -> PhotoParticipantMatch | None:
    for pending in session.new:
        if not isinstance(pending, PhotoParticipantMatch):
            continue
        if pending.photo_id == photo_id and pending.participant_id == participant_id:
            return pending
    return None


def clamp_match_value(value: str) -> str:
    normalized = str(value or "").strip()
    return normalized[:255] if normalized else "matched"


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
    job.raw_response_json = raw_response_json
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
    job.raw_response_json = raw_response_json
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
    job.raw_response_json = raw_response_json
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
    job.raw_response_json = {
        **(raw_response_json or {}),
        "bounding_box_json": bounding_box_json,
        "detection_score": detection_score,
        "quality_score": quality_score,
        "matched_count": matched_count,
    }
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
        apply_adaptive_reinforcement(session, search_session=search_session)

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
    strong_bib_photos = (
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

    seed_embeddings: list[list[float]] = []
    cluster_faces: list[PhotoFaceDetection] = []
    for photo in strong_bib_photos:
        if score_bib_evidence(photo.detections, participant.bib_number).strength != "strong":
            continue

        good_faces = sorted(
            [face for face in photo.face_detections if normalize_embedding_payload(face.embedding_json)],
            key=face_seed_quality,
            reverse=True,
        )
        if not good_faces:
            continue
        if len(good_faces) == 1:
            seed_embeddings.append(good_faces[0].embedding_json)
        elif is_dominant_face(good_faces):
            seed_embeddings.append(good_faces[0].embedding_json)
        else:
            cluster_faces.extend(good_faces[:4])

        if len(seed_embeddings) >= 3:
            return dedupe_seed_embeddings(seed_embeddings)[:3]

    seed_embeddings.extend(select_cluster_seed_embeddings(cluster_faces))
    return dedupe_seed_embeddings(seed_embeddings)[:3]


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
    if statuses and all(status in {"completed", "failed"} for status in statuses):
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
    results_by_detection_id: dict[uuid.UUID, FaceSearchResult] = {
        result.photo_face_detection_id: result
        for result in session.execute(
            select(FaceSearchResult).where(FaceSearchResult.search_session_id == search_session_id)
        ).scalars()
    }
    for pending in session.new:
        if not isinstance(pending, FaceSearchResult):
            continue
        if pending.search_session_id == search_session_id:
            results_by_detection_id[pending.photo_face_detection_id] = pending

    detections = (
        session.execute(
            select(PhotoFaceDetection).where(PhotoFaceDetection.event_id == event_id)
        )
        .scalars()
        .all()
    )
    created_count = 0
    for detection in detections:
        score = cosine_similarity(embedding, detection.embedding_json)
        if score < settings.face_candidate_similarity_threshold:
            continue
        existing = results_by_detection_id.get(detection.id)
        if existing is None:
            result = FaceSearchResult(
                event_id=event_id,
                search_session_id=search_session_id,
                photo_id=detection.photo_id,
                photo_face_detection_id=detection.id,
                similarity_score=score,
            )
            session.add(result)
            results_by_detection_id[detection.id] = result
            created_count += 1
        else:
            existing.similarity_score = max(existing.similarity_score, score)
    return created_count


def apply_adaptive_reinforcement(session: Session, *, search_session: FaceSearchSession) -> int:
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
                .order_by(FaceSearchResult.similarity_score.desc())
            )
            .scalars()
            .all()
        )
        created_this_round = 0
        for result in candidates:
            if created_total >= settings.face_reinforcement_max_embeddings:
                return created_total

            photo = session.get(Photo, result.photo_id)
            detection = session.get(PhotoFaceDetection, result.photo_face_detection_id)
            if photo is None or detection is None:
                continue

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
            if candidate == target or target in candidate:
                return BibEvidence(strength="strong", matched_values=(detection.detected_text,))
            if is_partial_bib_match(candidate, target):
                weak_matches.append(detection.detected_text)

    if weak_matches:
        return BibEvidence(strength="weak", matched_values=tuple(weak_matches[:3]))
    return BibEvidence(strength="none", matched_values=())


def is_partial_bib_match(candidate: str, target: str) -> bool:
    if len(target) < 3 or len(candidate) < 2:
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
    if statuses and all(status in {"completed", "failed"} for status in statuses):
        photo.status = "ready" if any(status == "completed" for status in statuses) else "failed"
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
        existing = session.execute(
            select(FaceParticipantMatch).where(
                FaceParticipantMatch.photo_face_detection_id == detection.id,
                FaceParticipantMatch.participant_id == participant_id,
            )
        ).scalar_one_or_none()
        if existing is None:
            existing = pending_face_participant_match(
                session,
                photo_face_detection_id=detection.id,
                participant_id=participant_id,
            )
        if existing is None:
            session.add(
                FaceParticipantMatch(
                    event_id=detection.event_id,
                    photo_id=detection.photo_id,
                    photo_face_detection_id=detection.id,
                    participant_id=participant_id,
                    similarity_score=score,
                    status="auto_matched",
                )
            )
            created_count += 1
        else:
            existing.similarity_score = max(existing.similarity_score, score)

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


def pending_face_participant_match(
    session: Session,
    *,
    photo_face_detection_id: uuid.UUID,
    participant_id: uuid.UUID,
) -> FaceParticipantMatch | None:
    for pending in session.new:
        if not isinstance(pending, FaceParticipantMatch):
            continue
        if pending.photo_face_detection_id == photo_face_detection_id and pending.participant_id == participant_id:
            return pending
    return None


def normalize_embedding_payload(value: object) -> list[float]:
    if not isinstance(value, list):
        return []
    embedding: list[float] = []
    for item in value:
        try:
            embedding.append(float(item))
        except (TypeError, ValueError):
            return []
    return embedding


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0

    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm <= 0 or right_norm <= 0:
        return 0.0
    return dot / (left_norm * right_norm)


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
