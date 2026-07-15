from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .config import settings
from .db import get_db
from .face import (
    complete_face_search_job,
    complete_participant_face_job,
    complete_photo_face_job,
    complete_photo_ocr_job,
    fail_face_search_job,
    fail_participant_face_job,
    fail_photo_job,
)
from .models import FaceSearchJob, ParticipantFaceJob, PhotoJob
from .photographer import safe_photo_access_url


router = APIRouter(prefix="/internal/worker", tags=["worker"])


def model_to_dict(model: BaseModel) -> dict[str, Any]:
    if hasattr(model, "model_dump"):
        return model.model_dump()
    return model.dict()


class ClaimRequest(BaseModel):
    job_types: list[str] = Field(default_factory=lambda: ["ocr", "face_photo_scan"])


class OCRDetectionPayload(BaseModel):
    detected_text: str
    normalized_text: str
    confidence: float | None = None
    bounding_box_json: dict[str, Any] | None = None


class FaceDetectionPayload(BaseModel):
    face_index: int
    embedding: list[float]
    bounding_box_json: dict[str, Any] | None = None
    detection_score: float | None = None
    quality_score: float | None = None


class CompletePhotoJobRequest(BaseModel):
    detections: list[OCRDetectionPayload] = Field(default_factory=list)
    face_detections: list[FaceDetectionPayload] = Field(default_factory=list)
    raw_response_json: dict[str, Any] | None = None


class CompleteParticipantFaceJobRequest(BaseModel):
    embedding: list[float]
    bounding_box_json: dict[str, Any] | None = None
    detection_score: float | None = None
    quality_score: float | None = None
    raw_response_json: dict[str, Any] | None = None


class CompleteFaceSearchJobRequest(BaseModel):
    embedding: list[float]
    bounding_box_json: dict[str, Any] | None = None
    detection_score: float | None = None
    quality_score: float | None = None
    raw_response_json: dict[str, Any] | None = None


class FailJobRequest(BaseModel):
    error_message: str


def require_worker_token(
    authorization: str | None = Header(default=None),
    x_worker_token: str | None = Header(default=None),
) -> None:
    if not settings.worker_api_token:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Worker API token is not configured.")

    expected = settings.worker_api_token
    bearer_value = ""
    if authorization and authorization.lower().startswith("bearer "):
        bearer_value = authorization.split(" ", 1)[1].strip()

    if x_worker_token != expected and bearer_value != expected:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid worker token.")


@router.post("/photo-jobs/claim")
def claim_photo_job(
    payload: ClaimRequest,
    db: Session = Depends(get_db),
    _token: None = Depends(require_worker_token),
) -> dict[str, Any]:
    allowed_types = [job_type for job_type in payload.job_types if job_type in {"ocr", "face_photo_scan"}]
    if not allowed_types:
        raise HTTPException(status_code=400, detail="No supported photo job types requested.")

    statement = (
        select(PhotoJob)
        .where(PhotoJob.status == "queued", PhotoJob.job_type.in_(allowed_types))
        .options(selectinload(PhotoJob.photo))
        .order_by(PhotoJob.created_at.asc())
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    job = db.execute(statement).scalar_one_or_none()
    if job is None:
        return {"job": None}

    job.status = "processing"
    job.attempt_count += 1
    job.error_message = None
    db.commit()
    db.refresh(job)

    return {
        "job": {
            "id": str(job.id),
            "job_type": job.job_type,
            "attempt_count": job.attempt_count,
            "photo": {
                "id": str(job.photo.id),
                "event_id": str(job.photo.event_id),
                "object_key": job.photo.original_object_key,
                "download_url": safe_photo_access_url(job.photo.original_object_key),
                "file_name": job.photo.file_name,
                "content_type": job.photo.content_type,
            },
        }
    }


@router.post("/photo-jobs/{job_id}/complete")
def complete_photo_job(
    job_id: UUID,
    payload: CompletePhotoJobRequest,
    db: Session = Depends(get_db),
    _token: None = Depends(require_worker_token),
) -> dict[str, str]:
    job = db.execute(
        select(PhotoJob)
        .where(PhotoJob.id == job_id)
        .options(selectinload(PhotoJob.photo))
    ).scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=404, detail="Photo job not found.")

    if job.job_type == "ocr":
        complete_photo_ocr_job(
            db,
            job=job,
            detections=[model_to_dict(detection) for detection in payload.detections],
            raw_response_json=payload.raw_response_json,
        )
    elif job.job_type == "face_photo_scan":
        complete_photo_face_job(
            db,
            job=job,
            face_detections=[model_to_dict(detection) for detection in payload.face_detections],
            raw_response_json=payload.raw_response_json,
        )
    else:
        raise HTTPException(status_code=400, detail=f"Unsupported photo job type: {job.job_type}")

    return {"status": "completed"}


@router.post("/photo-jobs/{job_id}/fail")
def fail_worker_photo_job(
    job_id: UUID,
    payload: FailJobRequest,
    db: Session = Depends(get_db),
    _token: None = Depends(require_worker_token),
) -> dict[str, str]:
    job = db.execute(
        select(PhotoJob)
        .where(PhotoJob.id == job_id)
        .options(selectinload(PhotoJob.photo))
    ).scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=404, detail="Photo job not found.")

    fail_photo_job(db, job=job, error_message=payload.error_message)
    return {"status": "failed"}


@router.post("/face-jobs/claim")
def claim_participant_face_job(
    db: Session = Depends(get_db),
    _token: None = Depends(require_worker_token),
) -> dict[str, Any]:
    statement = (
        select(ParticipantFaceJob)
        .where(ParticipantFaceJob.status == "queued", ParticipantFaceJob.job_type == "face_selfie_enroll")
        .options(selectinload(ParticipantFaceJob.face_image))
        .order_by(ParticipantFaceJob.created_at.asc())
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    job = db.execute(statement).scalar_one_or_none()
    if job is None:
        return {"job": None}

    job.status = "processing"
    job.attempt_count += 1
    job.error_message = None
    job.face_image.status = "processing"
    db.commit()
    db.refresh(job)

    return {
        "job": {
            "id": str(job.id),
            "job_type": job.job_type,
            "attempt_count": job.attempt_count,
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
    }


@router.post("/face-jobs/{job_id}/complete")
def complete_worker_participant_face_job(
    job_id: UUID,
    payload: CompleteParticipantFaceJobRequest,
    db: Session = Depends(get_db),
    _token: None = Depends(require_worker_token),
) -> dict[str, str]:
    job = db.execute(
        select(ParticipantFaceJob)
        .where(ParticipantFaceJob.id == job_id)
        .options(selectinload(ParticipantFaceJob.face_image))
    ).scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=404, detail="Face job not found.")

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
    job = db.execute(
        select(ParticipantFaceJob)
        .where(ParticipantFaceJob.id == job_id)
        .options(selectinload(ParticipantFaceJob.face_image))
    ).scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=404, detail="Face job not found.")

    fail_participant_face_job(db, job=job, error_message=payload.error_message)
    return {"status": "failed"}


@router.post("/face-search-jobs/claim")
def claim_face_search_job(
    db: Session = Depends(get_db),
    _token: None = Depends(require_worker_token),
) -> dict[str, Any]:
    statement = (
        select(FaceSearchJob)
        .where(FaceSearchJob.status == "queued", FaceSearchJob.job_type == "face_search_probe")
        .options(selectinload(FaceSearchJob.search_image), selectinload(FaceSearchJob.search_session))
        .order_by(FaceSearchJob.created_at.asc())
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    job = db.execute(statement).scalar_one_or_none()
    if job is None:
        return {"job": None}

    job.status = "processing"
    job.attempt_count += 1
    job.error_message = None
    job.search_image.status = "processing"
    job.search_session.status = "processing"
    db.commit()
    db.refresh(job)

    return {
        "job": {
            "id": str(job.id),
            "job_type": job.job_type,
            "attempt_count": job.attempt_count,
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
    }


@router.post("/face-search-jobs/{job_id}/complete")
def complete_worker_face_search_job(
    job_id: UUID,
    payload: CompleteFaceSearchJobRequest,
    db: Session = Depends(get_db),
    _token: None = Depends(require_worker_token),
) -> dict[str, str]:
    job = db.execute(
        select(FaceSearchJob)
        .where(FaceSearchJob.id == job_id)
        .options(selectinload(FaceSearchJob.search_image), selectinload(FaceSearchJob.search_session))
    ).scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=404, detail="Face search job not found.")

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
    job = db.execute(
        select(FaceSearchJob)
        .where(FaceSearchJob.id == job_id)
        .options(selectinload(FaceSearchJob.search_image), selectinload(FaceSearchJob.search_session))
    ).scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=404, detail="Face search job not found.")

    fail_face_search_job(db, job=job, error_message=payload.error_message)
    return {"status": "failed"}
