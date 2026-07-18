from __future__ import annotations

import hmac
from datetime import datetime, timezone

from fastapi import Header, HTTPException, status
from prometheus_client import CONTENT_TYPE_LATEST, Gauge, generate_latest
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.responses import Response

from .config import settings
from .db import engine
from .models import (
    FaceSearchJob,
    FaceSearchSession,
    ObjectDeletionTask,
    ParticipantFaceJob,
    PhotoJob,
    WorkerHeartbeat,
)


JOB_COUNT = Gauge(
    "raceframe_jobs",
    "Current durable job count by queue and status.",
    ("queue", "status"),
)
OLDEST_QUEUED_SECONDS = Gauge(
    "raceframe_oldest_queued_job_seconds",
    "Age of the oldest queued job by queue.",
    ("queue",),
)
WORKER_COUNT = Gauge(
    "raceframe_workers",
    "Known workers by state and freshness.",
    ("status", "freshness"),
)
FRESHEST_WORKER_AGE = Gauge(
    "raceframe_freshest_worker_heartbeat_age_seconds",
    "Age of the most recent worker heartbeat, or -1 when none exists.",
)
DELETION_TASK_COUNT = Gauge(
    "raceframe_object_deletion_tasks",
    "Current durable object-deletion task count by status.",
    ("status",),
)
EXPIRED_SEARCH_SESSION_COUNT = Gauge(
    "raceframe_expired_face_search_sessions",
    "Expired biometric search sessions awaiting lifecycle cleanup.",
)
DB_POOL_CHECKED_OUT = Gauge(
    "raceframe_db_pool_checked_out",
    "Database connections currently checked out by this process.",
)
DB_POOL_SIZE = Gauge(
    "raceframe_db_pool_size",
    "Configured SQLAlchemy connection pool size for this process.",
)

JOB_MODELS = {
    "photo": PhotoJob,
    "participant_face": ParticipantFaceJob,
    "face_search": FaceSearchJob,
}
JOB_STATUSES = ("queued", "processing", "completed", "failed", "dead_lettered")
WORKER_STATUSES = ("idle", "active", "draining")
DELETION_STATUSES = ("queued", "processing", "completed", "dead_lettered")


def require_metrics_token(authorization: str = Header(default="")) -> None:
    expected = settings.metrics_api_token
    supplied = authorization.removeprefix("Bearer ").strip() if authorization.startswith("Bearer ") else ""
    if not expected or not supplied or not hmac.compare_digest(supplied, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Valid metrics credentials are required.",
            headers={"WWW-Authenticate": "Bearer"},
        )


def _age_seconds(value: datetime | None, *, now: datetime) -> float:
    if value is None:
        return -1.0
    aware = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    return max(0.0, (now - aware).total_seconds())


def refresh_database_metrics(db: Session) -> None:
    now = datetime.now(timezone.utc)
    for queue_name, model in JOB_MODELS.items():
        counts = dict(
            db.execute(select(model.status, func.count(model.id)).group_by(model.status)).all()
        )
        for job_status in JOB_STATUSES:
            JOB_COUNT.labels(queue=queue_name, status=job_status).set(int(counts.get(job_status, 0)))
        oldest = db.scalar(select(func.min(model.created_at)).where(model.status == "queued"))
        OLDEST_QUEUED_SECONDS.labels(queue=queue_name).set(_age_seconds(oldest, now=now))

    deletion_counts = dict(
        db.execute(
            select(ObjectDeletionTask.status, func.count(ObjectDeletionTask.id)).group_by(ObjectDeletionTask.status)
        ).all()
    )
    for task_status in DELETION_STATUSES:
        DELETION_TASK_COUNT.labels(status=task_status).set(int(deletion_counts.get(task_status, 0)))

    heartbeats = list(db.execute(select(WorkerHeartbeat.status, WorkerHeartbeat.last_seen_at)).all())
    stale_after = max(1, settings.worker_heartbeat_stale_seconds)
    for worker_status in WORKER_STATUSES:
        fresh = 0
        stale = 0
        for status_value, last_seen_at in heartbeats:
            if status_value != worker_status:
                continue
            if _age_seconds(last_seen_at, now=now) <= stale_after:
                fresh += 1
            else:
                stale += 1
        WORKER_COUNT.labels(status=worker_status, freshness="fresh").set(fresh)
        WORKER_COUNT.labels(status=worker_status, freshness="stale").set(stale)
    latest_heartbeat = max((row.last_seen_at for row in heartbeats), default=None)
    FRESHEST_WORKER_AGE.set(_age_seconds(latest_heartbeat, now=now))

    EXPIRED_SEARCH_SESSION_COUNT.set(
        int(
            db.scalar(
                select(func.count(FaceSearchSession.id)).where(FaceSearchSession.expires_at <= now)
            )
            or 0
        )
    )

    pool = engine.pool
    checkedout = getattr(pool, "checkedout", None)
    size = getattr(pool, "size", None)
    DB_POOL_CHECKED_OUT.set(float(checkedout()) if callable(checkedout) else 0.0)
    DB_POOL_SIZE.set(float(size()) if callable(size) else 0.0)


def metrics_response(db: Session) -> Response:
    refresh_database_metrics(db)
    return Response(content=generate_latest(), headers={"Content-Type": CONTENT_TYPE_LATEST})
