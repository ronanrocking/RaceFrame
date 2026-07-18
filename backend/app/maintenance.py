from __future__ import annotations

import argparse
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, null, or_, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, selectinload

from .config import settings
from .db import SessionLocal
from .models import (
    FaceSearchJob,
    FaceSearchSession,
    ObjectDeletionTask,
    ParticipantFaceJob,
    PhotoJob,
    RateLimitBucket,
    WorkerHeartbeat,
)
from .storage import delete_object


logger = logging.getLogger(__name__)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def enqueue_object_deletion(session: Session, object_key: str | None) -> None:
    if not object_key:
        return
    table = ObjectDeletionTask.__table__
    insert_factory = sqlite_insert if session.get_bind().dialect.name == "sqlite" else postgresql_insert
    statement = insert_factory(table).values(object_key=object_key, status="queued")
    session.execute(
        statement.on_conflict_do_update(
            index_elements=[table.c.object_key],
            set_={
                "status": "queued",
                "attempt_count": 0,
                "attempt_id": None,
                "retry_at": None,
                "claimed_at": None,
                "lease_expires_at": None,
                "last_error": None,
                "completed_at": None,
                "dead_lettered_at": None,
                "updated_at": utc_now(),
            },
        )
    )


def process_object_deletions(session: Session, *, limit: int | None = None) -> tuple[int, int]:
    now = utc_now()
    batch_size = max(1, min(limit or settings.deletion_retry_batch_size, 1_000))
    session.execute(
        update(ObjectDeletionTask)
        .where(
            ObjectDeletionTask.status == "processing",
            ObjectDeletionTask.attempt_count >= ObjectDeletionTask.max_attempts,
            or_(ObjectDeletionTask.lease_expires_at.is_(None), ObjectDeletionTask.lease_expires_at <= now),
        )
        .values(
            status="dead_lettered",
            dead_lettered_at=now,
            lease_expires_at=None,
            attempt_id=None,
            retry_at=None,
            last_error="Maximum object-deletion attempts exhausted after a lost lease.",
        )
    )
    session.commit()
    tasks = (
        session.execute(
            select(ObjectDeletionTask)
            .where(
                or_(
                    ObjectDeletionTask.status == "queued",
                    (ObjectDeletionTask.status == "processing")
                    & (ObjectDeletionTask.lease_expires_at.is_(None) | (ObjectDeletionTask.lease_expires_at <= now)),
                ),
                ObjectDeletionTask.attempt_count < ObjectDeletionTask.max_attempts,
                or_(ObjectDeletionTask.retry_at.is_(None), ObjectDeletionTask.retry_at <= now),
            )
            .order_by(ObjectDeletionTask.created_at)
            .limit(batch_size)
            .with_for_update(skip_locked=True)
        )
        .scalars()
        .all()
    )
    attempts: dict[uuid.UUID, uuid.UUID] = {}
    for task in tasks:
        attempt_id = uuid.uuid4()
        attempts[task.id] = attempt_id
        task.status = "processing"
        task.attempt_id = attempt_id
        task.claimed_at = now
        task.lease_expires_at = now + timedelta(minutes=2)
        task.attempt_count += 1
    session.commit()

    completed = 0
    failed = 0
    for task_id, expected_attempt_id in attempts.items():
        task = session.get(ObjectDeletionTask, task_id)
        if task is None or task.status != "processing" or task.attempt_id != expected_attempt_id:
            continue
        try:
            delete_object(object_key=task.object_key)
        except Exception as exc:  # noqa: BLE001
            failed += 1
            task.last_error = f"{type(exc).__name__}: object deletion failed"[:2_000]
            task.lease_expires_at = None
            task.attempt_id = None
            if task.attempt_count >= task.max_attempts:
                task.status = "dead_lettered"
                task.dead_lettered_at = utc_now()
                task.retry_at = None
                logger.error("Object deletion exhausted retries", extra={"task_id": str(task.id)})
            else:
                task.status = "queued"
                delay = min(3_600, 2 ** min(task.attempt_count, 10))
                task.retry_at = utc_now() + timedelta(seconds=delay)
                logger.warning("Object deletion scheduled for retry", extra={"task_id": str(task.id)})
        else:
            completed += 1
            task.status = "completed"
            task.completed_at = utc_now()
            task.lease_expires_at = None
            task.attempt_id = None
            task.retry_at = None
            task.last_error = None
        session.commit()
    return completed, failed


def purge_expired_face_searches(session: Session, *, limit: int = 500) -> int:
    now = utc_now()
    sessions = (
        session.execute(
            select(FaceSearchSession)
            .where(FaceSearchSession.expires_at <= now)
            .options(selectinload(FaceSearchSession.images))
            .order_by(FaceSearchSession.expires_at)
            .limit(max(1, min(limit, 5_000)))
            .with_for_update(skip_locked=True)
        )
        .scalars()
        .unique()
        .all()
    )
    for search_session in sessions:
        for image in search_session.images:
            enqueue_object_deletion(session, image.object_key)
        session.delete(search_session)
    session.commit()
    return len(sessions)


def purge_old_job_diagnostics(session: Session) -> int:
    cutoff = utc_now() - timedelta(hours=max(1, settings.raw_response_retention_hours))
    affected = 0
    for model in (PhotoJob, ParticipantFaceJob, FaceSearchJob):
        result = session.execute(
            update(model)
            .where(model.finished_at.is_not(None), model.finished_at < cutoff, model.raw_response_json.is_not(None))
            # SQLAlchemy's generic JSON type persists Python None as a JSON
            # literal null by default. Use SQL NULL so the retention predicate
            # no longer selects the same already-purged row forever.
            .values(raw_response_json=null())
        )
        affected += int(result.rowcount or 0)
    session.commit()
    return affected


def purge_expired_rate_limits(session: Session) -> int:
    result = session.execute(delete(RateLimitBucket).where(RateLimitBucket.expires_at < utc_now()))
    session.commit()
    return int(result.rowcount or 0)


def purge_completed_deletion_tasks(session: Session) -> int:
    cutoff = utc_now() - timedelta(days=max(1, settings.deletion_task_retention_days))
    result = session.execute(
        delete(ObjectDeletionTask).where(
            ObjectDeletionTask.status == "completed",
            ObjectDeletionTask.completed_at.is_not(None),
            ObjectDeletionTask.completed_at < cutoff,
        )
    )
    session.commit()
    return int(result.rowcount or 0)


def purge_stale_worker_heartbeats(session: Session) -> int:
    cutoff = utc_now() - timedelta(days=max(1, settings.worker_heartbeat_retention_days))
    result = session.execute(delete(WorkerHeartbeat).where(WorkerHeartbeat.last_seen_at < cutoff))
    session.commit()
    return int(result.rowcount or 0)


@dataclass(frozen=True)
class MaintenanceResult:
    expired_searches: int
    deletion_completed: int
    deletion_failed: int
    diagnostics_purged: int
    rate_limit_buckets_purged: int
    completed_deletion_tasks_purged: int
    stale_worker_heartbeats_purged: int


def run_once() -> MaintenanceResult:
    with SessionLocal() as session:
        expired_searches = purge_expired_face_searches(session)
        deletion_completed, deletion_failed = process_object_deletions(session)
        diagnostics_purged = purge_old_job_diagnostics(session)
        rate_limit_buckets_purged = purge_expired_rate_limits(session)
        completed_deletion_tasks_purged = purge_completed_deletion_tasks(session)
        stale_worker_heartbeats_purged = purge_stale_worker_heartbeats(session)
    result = MaintenanceResult(
        expired_searches=expired_searches,
        deletion_completed=deletion_completed,
        deletion_failed=deletion_failed,
        diagnostics_purged=diagnostics_purged,
        rate_limit_buckets_purged=rate_limit_buckets_purged,
        completed_deletion_tasks_purged=completed_deletion_tasks_purged,
        stale_worker_heartbeats_purged=stale_worker_heartbeats_purged,
    )
    logger.info("Maintenance completed", extra={"maintenance": result.__dict__})
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Run RaceFrame idempotent maintenance tasks.")
    parser.add_argument("command", choices=("run",), default="run", nargs="?")
    parser.parse_args()
    result = run_once()
    print(
        f"expired_searches={result.expired_searches} "
        f"deletion_completed={result.deletion_completed} "
        f"deletion_failed={result.deletion_failed} "
        f"diagnostics_purged={result.diagnostics_purged}"
        f" rate_limit_buckets_purged={result.rate_limit_buckets_purged}"
        f" completed_deletion_tasks_purged={result.completed_deletion_tasks_purged}"
        f" stale_worker_heartbeats_purged={result.stale_worker_heartbeats_purged}"
    )


if __name__ == "__main__":
    main()
