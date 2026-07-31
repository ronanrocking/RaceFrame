from __future__ import annotations

import hmac
import json
import math
import random
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import and_, or_, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, selectinload

from .config import settings
from .db import get_db
from .face import (
    complete_face_search_job,
    complete_participant_face_job,
    complete_photo_face_job,
    complete_photo_ocr_job,
)
from .models import FaceSearchJob, ParticipantFaceJob, PhotoJob, WorkerHeartbeat, utc_now
from .photographer import safe_photo_access_url


router = APIRouter(prefix="/internal/worker", tags=["worker"])

TERMINAL_JOB_STATUSES = frozenset({"completed", "failed", "dead_lettered"})
MAX_OCR_DETECTIONS = 2_000
MAX_FACE_DETECTIONS = 256
EMBEDDING_DIMENSIONS = 512
MAX_RAW_JSON_BYTES = 256 * 1024
MAX_BOUNDING_BOX_BYTES = 16 * 1024


WORKER_LEASE_SECONDS = max(30, min(settings.worker_lease_seconds, 3_600))
WORKER_RETRY_BASE_SECONDS = max(1, min(settings.worker_retry_base_seconds, 3_600))
WORKER_RETRY_MAX_SECONDS = max(
    WORKER_RETRY_BASE_SECONDS,
    min(settings.worker_retry_max_seconds, 86_400),
)


def model_to_dict(model: BaseModel) -> dict[str, Any]:
    if hasattr(model, "model_dump"):
        return model.model_dump()
    return model.dict()


def _validate_bounded_json(value: Any, *, max_bytes: int, label: str) -> Any:
    if value is None:
        return None
    try:
        encoded = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must contain valid finite JSON values.") from exc
    if len(encoded) > max_bytes:
        raise ValueError(f"{label} exceeds the {max_bytes}-byte limit.")

    stack: list[tuple[Any, int]] = [(value, 0)]
    item_count = 0
    while stack:
        current, depth = stack.pop()
        if depth > 10:
            raise ValueError(f"{label} is nested too deeply.")
        if isinstance(current, dict):
            item_count += len(current)
            stack.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            item_count += len(current)
            stack.extend((item, depth + 1) for item in current)
        if item_count > 10_000:
            raise ValueError(f"{label} contains too many values.")
    return value


def _validate_embedding(value: list[float]) -> list[float]:
    if len(value) != EMBEDDING_DIMENSIONS:
        raise ValueError(f"Embedding must have exactly {EMBEDDING_DIMENSIONS} dimensions.")
    if not all(math.isfinite(float(item)) for item in value):
        raise ValueError("Embedding values must all be finite.")
    norm_squared = sum(float(item) * float(item) for item in value)
    if norm_squared <= 0.0:
        raise ValueError("Embedding must have a non-zero norm.")
    return value


def _validate_score(value: float | None) -> float | None:
    if value is None:
        return None
    if not math.isfinite(value) or value < 0.0 or value > 1.0:
        raise ValueError("Score must be a finite number between 0 and 1.")
    return value


class StrictRequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ClaimRequest(StrictRequestModel):
    job_types: list[str] = Field(default_factory=lambda: ["ocr", "face_photo_scan"])

    @field_validator("job_types")
    @classmethod
    def validate_job_types(cls, value: list[str]) -> list[str]:  # noqa: N805
        if not value or len(value) > 2:
            raise ValueError("Request between one and two job types.")
        if len(set(value)) != len(value):
            raise ValueError("Duplicate job types are not allowed.")
        unsupported = set(value) - {"ocr", "face_photo_scan"}
        if unsupported:
            raise ValueError(f"Unsupported photo job type: {sorted(unsupported)[0]}")
        return value


class AttemptRequest(StrictRequestModel):
    attempt_id: UUID


class HeartbeatRequest(AttemptRequest):
    pass


class OCRDetectionPayload(StrictRequestModel):
    detected_text: str = Field(min_length=1, max_length=4_096)
    normalized_text: str = Field(min_length=1, max_length=4_096)
    confidence: float | None = None
    bounding_box_json: dict[str, Any] | None = None

    _finite_confidence = field_validator("confidence")(_validate_score)

    @field_validator("bounding_box_json")
    @classmethod
    def validate_box(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:  # noqa: N805
        return _validate_bounded_json(value, max_bytes=MAX_BOUNDING_BOX_BYTES, label="Bounding box")


class FaceDetectionPayload(StrictRequestModel):
    face_index: int = Field(ge=0, le=MAX_FACE_DETECTIONS - 1)
    embedding: list[float]
    bounding_box_json: dict[str, Any] | None = None
    detection_score: float | None = None
    quality_score: float | None = None

    _bounded_embedding = field_validator("embedding")(_validate_embedding)
    _finite_detection_score = field_validator("detection_score")(_validate_score)
    _finite_quality_score = field_validator("quality_score")(_validate_score)

    @field_validator("bounding_box_json")
    @classmethod
    def validate_box(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:  # noqa: N805
        return _validate_bounded_json(value, max_bytes=MAX_BOUNDING_BOX_BYTES, label="Bounding box")


class CompletePhotoJobRequest(AttemptRequest):
    detections: list[OCRDetectionPayload] = Field(default_factory=list)
    face_detections: list[FaceDetectionPayload] = Field(default_factory=list)
    raw_response_json: dict[str, Any] | None = None

    @field_validator("detections")
    @classmethod
    def validate_detection_count(cls, value: list[OCRDetectionPayload]) -> list[OCRDetectionPayload]:  # noqa: N805
        if len(value) > MAX_OCR_DETECTIONS:
            raise ValueError(f"At most {MAX_OCR_DETECTIONS} OCR detections are accepted.")
        return value

    @field_validator("face_detections")
    @classmethod
    def validate_face_count(cls, value: list[FaceDetectionPayload]) -> list[FaceDetectionPayload]:  # noqa: N805
        if len(value) > MAX_FACE_DETECTIONS:
            raise ValueError(f"At most {MAX_FACE_DETECTIONS} face detections are accepted.")
        indexes = [item.face_index for item in value]
        if len(indexes) != len(set(indexes)):
            raise ValueError("Face indexes must be unique within a result.")
        return value

    @field_validator("raw_response_json")
    @classmethod
    def validate_raw_response(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:  # noqa: N805
        return _validate_bounded_json(value, max_bytes=MAX_RAW_JSON_BYTES, label="Raw response")


class CompleteEmbeddingJobRequest(AttemptRequest):
    embedding: list[float]
    bounding_box_json: dict[str, Any] | None = None
    detection_score: float | None = None
    quality_score: float | None = None
    raw_response_json: dict[str, Any] | None = None

    _bounded_embedding = field_validator("embedding")(_validate_embedding)
    _finite_detection_score = field_validator("detection_score")(_validate_score)
    _finite_quality_score = field_validator("quality_score")(_validate_score)

    @field_validator("bounding_box_json")
    @classmethod
    def validate_box(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:  # noqa: N805
        return _validate_bounded_json(value, max_bytes=MAX_BOUNDING_BOX_BYTES, label="Bounding box")

    @field_validator("raw_response_json")
    @classmethod
    def validate_raw_response(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:  # noqa: N805
        return _validate_bounded_json(value, max_bytes=MAX_RAW_JSON_BYTES, label="Raw response")


class CompleteParticipantFaceJobRequest(CompleteEmbeddingJobRequest):
    pass


class CompleteFaceSearchJobRequest(CompleteEmbeddingJobRequest):
    pass


class FailJobRequest(AttemptRequest):
    error_message: str = Field(min_length=1, max_length=2_000)
    retryable: bool = True
    error_code: str | None = Field(default=None, min_length=1, max_length=64, pattern=r"^[a-z0-9_.-]+$")


class WorkerHeartbeatRequest(StrictRequestModel):
    worker_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")
    worker_version: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.:+-]+$")
    started_at: datetime
    status: str
    current_job_id: UUID | None = None
    current_job_type: str | None = Field(default=None, max_length=32)

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str) -> str:  # noqa: N805
        if value not in {"idle", "active", "draining"}:
            raise ValueError("Worker status must be idle, active, or draining.")
        return value

    @field_validator("started_at")
    @classmethod
    def validate_started_at(cls, value: datetime) -> datetime:  # noqa: N805
        if value.tzinfo is None:
            raise ValueError("Worker start time must include a timezone.")
        return value.astimezone(timezone.utc)

    @field_validator("current_job_type")
    @classmethod
    def validate_current_job_type(cls, value: str | None) -> str | None:  # noqa: N805
        if value is not None and value not in {"ocr", "face_photo_scan", "face_selfie_enroll", "face_search_probe"}:
            raise ValueError("Unsupported current worker job type.")
        return value


def _constant_time_equal(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def require_worker_token(
    authorization: str | None = Header(default=None),
    x_worker_token: str | None = Header(default=None),
) -> None:
    if not settings.worker_api_token:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Worker API token is not configured.")

    bearer_value = ""
    if authorization and authorization.lower().startswith("bearer "):
        bearer_value = authorization.split(" ", 1)[1].strip()

    expected = settings.worker_api_token
    bearer_valid = _constant_time_equal(bearer_value, expected)
    header_valid = _constant_time_equal(x_worker_token or "", expected)
    if not (bearer_valid or header_valid):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid worker token.")


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _attempt_matches(stored_attempt_id: UUID | None, supplied_attempt_id: UUID) -> bool:
    if stored_attempt_id is None:
        return False
    return hmac.compare_digest(stored_attempt_id.bytes, supplied_attempt_id.bytes)


def _verify_active_attempt(job: Any, attempt_id: UUID, *, allow_terminal: bool = False) -> str:
    matches = _attempt_matches(job.attempt_id, attempt_id)
    if allow_terminal and matches and job.status in TERMINAL_JOB_STATUSES:
        return job.status
    if allow_terminal and matches and job.status == "queued":
        return "retry_scheduled"
    if job.status != "processing" or not matches:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The job is not owned by this worker attempt.",
        )
    if job.lease_expires_at is None or _aware(job.lease_expires_at) <= utc_now():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="The worker lease has expired.")
    return "processing"


def _sync_terminal_parent_state(db: Session, job: Any) -> None:
    if isinstance(job, PhotoJob):
        statuses = list(db.execute(select(PhotoJob.status).where(PhotoJob.photo_id == job.photo_id)).scalars())
        if statuses and all(item in TERMINAL_JOB_STATUSES for item in statuses):
            if all(item == "completed" for item in statuses):
                job.photo.status = "ready"
            elif "completed" in statuses:
                job.photo.status = "partially_ready"
            else:
                job.photo.status = "failed"
        return

    if isinstance(job, ParticipantFaceJob):
        job.face_image.status = "failed"
        job.face_image.error_message = (job.error_message or "Face processing failed.")[:2_000]
        return

    if isinstance(job, FaceSearchJob):
        job.search_image.status = "failed"
        job.search_image.error_message = (job.error_message or "Face search failed.")[:2_000]
        statuses = list(
            db.execute(
                select(FaceSearchJob.status).where(FaceSearchJob.search_session_id == job.search_session_id)
            ).scalars()
        )
        if statuses and all(item in TERMINAL_JOB_STATUSES for item in statuses):
            job.search_session.status = "completed" if "completed" in statuses else "failed"
            job.search_session.finished_at = utc_now()


def _dead_letter(job: Any, *, reason: str) -> None:
    now = utc_now()
    job.status = "dead_lettered"
    job.error_message = reason[:2_000]
    job.retry_after = None
    job.lease_expires_at = None
    job.finished_at = now
    job.dead_lettered_at = now


def _mark_parent_processing(job: Any) -> None:
    if isinstance(job, PhotoJob):
        job.photo.status = "processing"
    elif isinstance(job, ParticipantFaceJob):
        job.face_image.status = "processing"
        job.face_image.error_message = None
    elif isinstance(job, FaceSearchJob):
        job.search_image.status = "processing"
        job.search_image.error_message = None
        job.search_session.status = "processing"


def _reap_exhausted_jobs(
    db: Session,
    model: type[Any],
    *,
    job_type_condition: Any,
    relationship_options: tuple[Any, ...],
) -> None:
    now = utc_now()
    availability = or_(
        and_(model.status == "queued", or_(model.retry_after.is_(None), model.retry_after <= now)),
        and_(
            model.status == "processing",
            or_(model.lease_expires_at.is_(None), model.lease_expires_at <= now),
        ),
    )
    statement = (
        select(model)
        .where(job_type_condition, model.attempt_count >= model.max_attempts, availability)
        .options(*relationship_options)
        .order_by(model.created_at.asc())
        .limit(50)
        .with_for_update(skip_locked=True)
    )
    for exhausted in db.execute(statement).scalars():
        _dead_letter(exhausted, reason=exhausted.error_message or "Maximum processing attempts exhausted.")
        _sync_terminal_parent_state(db, exhausted)


def _claim_job(
    db: Session,
    model: type[Any],
    *,
    job_type_condition: Any,
    relationship_options: tuple[Any, ...],
) -> Any | None:
    _reap_exhausted_jobs(
        db,
        model,
        job_type_condition=job_type_condition,
        relationship_options=relationship_options,
    )
    now = utc_now()
    availability = or_(
        and_(model.status == "queued", or_(model.retry_after.is_(None), model.retry_after <= now)),
        and_(
            model.status == "processing",
            or_(model.lease_expires_at.is_(None), model.lease_expires_at <= now),
        ),
    )
    statement = (
        select(model)
        .where(job_type_condition, model.attempt_count < model.max_attempts, availability)
        .options(*relationship_options)
        .order_by(model.created_at.asc())
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    job = db.execute(statement).scalar_one_or_none()
    if job is None:
        db.commit()  # Persist exhausted-job reconciliation, if any.
        return None

    job.status = "processing"
    job.attempt_count += 1
    job.attempt_id = uuid.uuid4()
    job.claimed_at = now
    job.lease_expires_at = now + timedelta(seconds=WORKER_LEASE_SECONDS)
    job.retry_after = None
    job.dead_lettered_at = None
    job.error_message = None
    job.finished_at = None
    _mark_parent_processing(job)
    db.commit()
    db.refresh(job)
    return job


def _job_claim_metadata(job: Any, *, heartbeat_path: str) -> dict[str, Any]:
    return {
        "id": str(job.id),
        "job_type": job.job_type,
        "attempt_count": job.attempt_count,
        "max_attempts": job.max_attempts,
        "attempt_id": str(job.attempt_id),
        "lease_expires_at": job.lease_expires_at.isoformat(),
        "lease_seconds": WORKER_LEASE_SECONDS,
        "heartbeat_path": heartbeat_path,
    }


def _retry_delay(attempt_count: int) -> int:
    exponential = min(
        WORKER_RETRY_MAX_SECONDS,
        WORKER_RETRY_BASE_SECONDS * (2 ** min(20, max(0, attempt_count - 1))),
    )
    return max(1, int(exponential * random.uniform(0.8, 1.2)))  # nosec B311


def _fail_job(db: Session, job: Any, payload: FailJobRequest) -> dict[str, str]:
    current = _verify_active_attempt(job, payload.attempt_id, allow_terminal=True)
    if current != "processing":
        return {"status": current}

    prefix = f"[{payload.error_code}] " if payload.error_code else ""
    job.error_message = f"{prefix}{payload.error_message}"[:2_000]
    job.lease_expires_at = None
    now = utc_now()
    if payload.retryable and job.attempt_count < job.max_attempts:
        job.status = "queued"
        job.retry_after = now + timedelta(seconds=_retry_delay(job.attempt_count))
        job.finished_at = None
        db.commit()
        return {"status": "retry_scheduled", "retry_after": job.retry_after.isoformat()}

    if payload.retryable:
        _dead_letter(job, reason=job.error_message)
    else:
        job.status = "failed"
        job.retry_after = None
        job.finished_at = now
    _sync_terminal_parent_state(db, job)
    db.commit()
    return {"status": job.status}


def _heartbeat_job(db: Session, job: Any, payload: HeartbeatRequest) -> dict[str, str]:
    current = _verify_active_attempt(job, payload.attempt_id, allow_terminal=False)
    if current != "processing":  # Defensive; _verify_active_attempt currently raises instead.
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="The worker no longer owns this job.")
    now = utc_now()
    job.lease_expires_at = now + timedelta(seconds=WORKER_LEASE_SECONDS)
    db.commit()
    return {"status": "processing", "lease_expires_at": job.lease_expires_at.isoformat()}


def _load_photo_job(db: Session, job_id: UUID) -> PhotoJob:
    job = db.execute(
        select(PhotoJob)
        .where(PhotoJob.id == job_id)
        .options(selectinload(PhotoJob.photo))
        .with_for_update()
    ).scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=404, detail="Photo job not found.")
    return job


def _load_participant_face_job(db: Session, job_id: UUID) -> ParticipantFaceJob:
    job = db.execute(
        select(ParticipantFaceJob)
        .where(ParticipantFaceJob.id == job_id)
        .options(selectinload(ParticipantFaceJob.face_image))
        .with_for_update()
    ).scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=404, detail="Face job not found.")
    return job


def _load_face_search_job(db: Session, job_id: UUID) -> FaceSearchJob:
    job = db.execute(
        select(FaceSearchJob)
        .where(FaceSearchJob.id == job_id)
        .options(selectinload(FaceSearchJob.search_image), selectinload(FaceSearchJob.search_session))
        .with_for_update()
    ).scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=404, detail="Face search job not found.")
    return job


@router.post("/heartbeat")
def record_worker_heartbeat(
    payload: WorkerHeartbeatRequest,
    db: Session = Depends(get_db),
    _token: None = Depends(require_worker_token),
) -> dict[str, Any]:
    has_job_id = payload.current_job_id is not None
    has_job_type = payload.current_job_type is not None
    if has_job_id != has_job_type:
        raise HTTPException(status_code=422, detail="Current job ID and type must be provided together.")
    if payload.status == "active" and not has_job_id:
        raise HTTPException(status_code=422, detail="An active worker must report its current job.")
    if payload.status != "active" and (has_job_id or has_job_type):
        raise HTTPException(status_code=422, detail="Only an active worker may report a current job.")

    now = utc_now()
    table = WorkerHeartbeat.__table__
    dialect_name = db.get_bind().dialect.name
    if dialect_name == "sqlite":
        statement = sqlite_insert(table)
    elif dialect_name == "postgresql":
        statement = postgresql_insert(table)
    else:
        raise HTTPException(status_code=503, detail="Worker heartbeat storage is unavailable.")
    statement = statement.values(
        worker_id=payload.worker_id,
        worker_version=payload.worker_version,
        status=payload.status,
        current_job_id=payload.current_job_id,
        current_job_type=payload.current_job_type,
        started_at=payload.started_at,
        last_seen_at=now,
    )
    db.execute(
        statement.on_conflict_do_update(
            index_elements=[table.c.worker_id],
            set_={
                "worker_version": statement.excluded.worker_version,
                "status": statement.excluded.status,
                "current_job_id": statement.excluded.current_job_id,
                "current_job_type": statement.excluded.current_job_type,
                "started_at": statement.excluded.started_at,
                "last_seen_at": statement.excluded.last_seen_at,
            },
        )
    )
    db.commit()
    return {
        "status": "ok",
        "server_time": now.isoformat(),
        "recommended_interval_seconds": 30,
    }


@router.post("/photo-jobs/claim")
def claim_photo_job(
    payload: ClaimRequest,
    db: Session = Depends(get_db),
    _token: None = Depends(require_worker_token),
) -> dict[str, Any]:
    job = _claim_job(
        db,
        PhotoJob,
        job_type_condition=PhotoJob.job_type.in_(payload.job_types),
        relationship_options=(selectinload(PhotoJob.photo),),
    )
    if job is None:
        return {"job": None}

    response = _job_claim_metadata(job, heartbeat_path=f"/internal/worker/photo-jobs/{job.id}/heartbeat")
    response["photo"] = {
        "id": str(job.photo.id),
        "event_id": str(job.photo.event_id),
        "object_key": job.photo.original_object_key,
        "download_url": safe_photo_access_url(job.photo.original_object_key),
        "file_name": job.photo.file_name,
        "content_type": job.photo.content_type,
    }
    return {"job": response}


@router.post("/photo-jobs/{job_id}/heartbeat")
def heartbeat_photo_job(
    job_id: UUID,
    payload: HeartbeatRequest,
    db: Session = Depends(get_db),
    _token: None = Depends(require_worker_token),
) -> dict[str, str]:
    return _heartbeat_job(db, _load_photo_job(db, job_id), payload)


@router.post("/photo-jobs/{job_id}/complete")
def complete_photo_job(
    job_id: UUID,
    payload: CompletePhotoJobRequest,
    db: Session = Depends(get_db),
    _token: None = Depends(require_worker_token),
) -> dict[str, str]:
    job = _load_photo_job(db, job_id)
    current = _verify_active_attempt(job, payload.attempt_id, allow_terminal=True)
    if current == "completed":
        return {"status": "completed"}
    if current != "processing":
        raise HTTPException(status_code=409, detail=f"Job cannot complete from state {current}.")

    job.lease_expires_at = None
    job.retry_after = None
    if job.job_type == "ocr":
        if payload.face_detections:
            raise HTTPException(status_code=422, detail="OCR jobs cannot return face detections.")
        complete_photo_ocr_job(
            db,
            job=job,
            detections=[model_to_dict(detection) for detection in payload.detections],
            raw_response_json=payload.raw_response_json,
        )
    elif job.job_type == "face_photo_scan":
        if payload.detections:
            raise HTTPException(status_code=422, detail="Face scan jobs cannot return OCR detections.")
        complete_photo_face_job(
            db,
            job=job,
            face_detections=[model_to_dict(detection) for detection in payload.face_detections],
            raw_response_json=payload.raw_response_json,
        )
    else:
        raise HTTPException(status_code=400, detail="Unsupported photo job type.")
    return {"status": "completed"}


@router.post("/photo-jobs/{job_id}/fail")
def fail_worker_photo_job(
    job_id: UUID,
    payload: FailJobRequest,
    db: Session = Depends(get_db),
    _token: None = Depends(require_worker_token),
) -> dict[str, str]:
    return _fail_job(db, _load_photo_job(db, job_id), payload)


@router.post("/face-jobs/claim")
def claim_participant_face_job(
    db: Session = Depends(get_db),
    _token: None = Depends(require_worker_token),
) -> dict[str, Any]:
    job = _claim_job(
        db,
        ParticipantFaceJob,
        job_type_condition=ParticipantFaceJob.job_type == "face_selfie_enroll",
        relationship_options=(selectinload(ParticipantFaceJob.face_image),),
    )
    if job is None:
        return {"job": None}

    response = _job_claim_metadata(job, heartbeat_path=f"/internal/worker/face-jobs/{job.id}/heartbeat")
    response.update(
        {
            "event_id": str(job.event_id),
            "participant_id": str(job.participant_id),
            "face_image": {
                "id": str(job.face_image.id),
                "object_key": job.face_image.object_key,
                "download_url": safe_photo_access_url(job.face_image.object_key),
                "file_name": job.face_image.file_name,
                "content_type": job.face_image.content_type,
            },
        }
    )
    return {"job": response}


@router.post("/face-jobs/{job_id}/heartbeat")
def heartbeat_participant_face_job(
    job_id: UUID,
    payload: HeartbeatRequest,
    db: Session = Depends(get_db),
    _token: None = Depends(require_worker_token),
) -> dict[str, str]:
    return _heartbeat_job(db, _load_participant_face_job(db, job_id), payload)


@router.post("/face-jobs/{job_id}/complete")
def complete_worker_participant_face_job(
    job_id: UUID,
    payload: CompleteParticipantFaceJobRequest,
    db: Session = Depends(get_db),
    _token: None = Depends(require_worker_token),
) -> dict[str, str]:
    job = _load_participant_face_job(db, job_id)
    current = _verify_active_attempt(job, payload.attempt_id, allow_terminal=True)
    if current == "completed":
        return {"status": "completed"}
    if current != "processing":
        raise HTTPException(status_code=409, detail=f"Job cannot complete from state {current}.")
    job.lease_expires_at = None
    job.retry_after = None
    complete_participant_face_job(
        db,
        job=job,
        embedding=payload.embedding,
        bounding_box_json=payload.bounding_box_json,
        detection_score=payload.detection_score,
        quality_score=payload.quality_score,
        raw_response_json=payload.raw_response_json,
    )
    return {"status": "completed"}


@router.post("/face-jobs/{job_id}/fail")
def fail_worker_participant_face_job(
    job_id: UUID,
    payload: FailJobRequest,
    db: Session = Depends(get_db),
    _token: None = Depends(require_worker_token),
) -> dict[str, str]:
    return _fail_job(db, _load_participant_face_job(db, job_id), payload)


@router.post("/face-search-jobs/claim")
def claim_face_search_job(
    db: Session = Depends(get_db),
    _token: None = Depends(require_worker_token),
) -> dict[str, Any]:
    job = _claim_job(
        db,
        FaceSearchJob,
        job_type_condition=FaceSearchJob.job_type == "face_search_probe",
        relationship_options=(selectinload(FaceSearchJob.search_image), selectinload(FaceSearchJob.search_session)),
    )
    if job is None:
        return {"job": None}

    response = _job_claim_metadata(job, heartbeat_path=f"/internal/worker/face-search-jobs/{job.id}/heartbeat")
    response.update(
        {
            "event_id": str(job.event_id),
            "search_session_id": str(job.search_session_id),
            "face_image": {
                "id": str(job.search_image.id),
                "object_key": job.search_image.object_key,
                "download_url": safe_photo_access_url(job.search_image.object_key),
                "file_name": job.search_image.file_name,
                "content_type": job.search_image.content_type,
            },
        }
    )
    return {"job": response}


@router.post("/face-search-jobs/{job_id}/heartbeat")
def heartbeat_face_search_job(
    job_id: UUID,
    payload: HeartbeatRequest,
    db: Session = Depends(get_db),
    _token: None = Depends(require_worker_token),
) -> dict[str, str]:
    return _heartbeat_job(db, _load_face_search_job(db, job_id), payload)


@router.post("/face-search-jobs/{job_id}/complete")
def complete_worker_face_search_job(
    job_id: UUID,
    payload: CompleteFaceSearchJobRequest,
    db: Session = Depends(get_db),
    _token: None = Depends(require_worker_token),
) -> dict[str, str]:
    job = _load_face_search_job(db, job_id)
    current = _verify_active_attempt(job, payload.attempt_id, allow_terminal=True)
    if current == "completed":
        return {"status": "completed"}
    if current != "processing":
        raise HTTPException(status_code=409, detail=f"Job cannot complete from state {current}.")
    job.lease_expires_at = None
    job.retry_after = None
    complete_face_search_job(
        db,
        job=job,
        embedding=payload.embedding,
        bounding_box_json=payload.bounding_box_json,
        detection_score=payload.detection_score,
        quality_score=payload.quality_score,
        raw_response_json=payload.raw_response_json,
    )
    return {"status": "completed"}


@router.post("/face-search-jobs/{job_id}/fail")
def fail_worker_face_search_job(
    job_id: UUID,
    payload: FailJobRequest,
    db: Session = Depends(get_db),
    _token: None = Depends(require_worker_token),
) -> dict[str, str]:
    return _fail_job(db, _load_face_search_job(db, job_id), payload)
