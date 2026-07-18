from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.admin import acquire_admin_lock, force_admin_lock
from app.db import Base
from app.maintenance import (
    enqueue_object_deletion,
    process_object_deletions,
    purge_expired_face_searches,
    purge_old_job_diagnostics,
)
from app.models import (
    AdminSessionLock,
    Event,
    FaceSearchImage,
    FaceSearchSession,
    ObjectDeletionTask,
    PhotoJob,
    RateLimitBucket,
)
from app.rate_limits import hit_persistent_limit
from app.web_security import decode_search_capability, issue_search_capability


def make_session_factory():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def test_signed_search_capability_is_event_bound_and_tamper_evident() -> None:
    event_id = str(uuid.uuid4())
    visitor_id = "visitor-token-1234567890"
    capability = issue_search_capability(session_id=str(uuid.uuid4()), event_id=event_id, visitor_id=visitor_id)

    decoded = decode_search_capability(capability.secret, event_id=event_id, visitor_id=visitor_id)
    assert decoded is not None
    assert decoded.capability_hash == capability.capability_hash
    assert decode_search_capability(capability.secret, event_id=str(uuid.uuid4()), visitor_id=visitor_id) is None
    assert decode_search_capability(capability.secret + "x", event_id=event_id, visitor_id=visitor_id) is None


def test_persistent_rate_limit_is_atomic_across_sessions(monkeypatch) -> None:
    factory = make_session_factory()
    monkeypatch.setattr("app.rate_limits.SessionLocal", factory)

    assert hit_persistent_limit(bucket="search-minute", key="visitor", limit=2, window_seconds=60) is None
    assert hit_persistent_limit(bucket="search-minute", key="visitor", limit=2, window_seconds=60) is None
    assert hit_persistent_limit(bucket="search-minute", key="visitor", limit=2, window_seconds=60) is not None
    with factory() as session:
        assert session.query(RateLimitBucket).count() == 1


def test_persistent_rate_limit_accounts_for_request_cost(monkeypatch) -> None:
    factory = make_session_factory()
    monkeypatch.setattr("app.rate_limits.SessionLocal", factory)

    assert hit_persistent_limit(bucket="upload", key="event", limit=5, window_seconds=60, units=3) is None
    assert hit_persistent_limit(bucket="upload", key="event", limit=5, window_seconds=60, units=3) is not None


def test_admin_lock_acquisition_and_takeover_are_atomic() -> None:
    factory = make_session_factory()
    with factory() as session:
        assert acquire_admin_lock(session, session_id="admin-a") is True
        assert acquire_admin_lock(session, session_id="admin-a") is True
        assert acquire_admin_lock(session, session_id="admin-b") is False
        force_admin_lock(session, session_id="admin-b")
        assert acquire_admin_lock(session, session_id="admin-b") is True
        assert session.get(AdminSessionLock, 1).session_id == "admin-b"


def test_expired_biometric_session_queues_object_deletion(monkeypatch) -> None:
    factory = make_session_factory()
    event_id = uuid.uuid4()
    search_id = uuid.uuid4()
    with factory() as session:
        session.add(Event(id=event_id, name="Race", slug="race", status="published"))
        session.add(
            FaceSearchSession(
                id=search_id,
                event_id=event_id,
                status="completed",
                expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            )
        )
        session.add(
            FaceSearchImage(
                event_id=event_id,
                search_session_id=search_id,
                object_key=f"events/{event_id}/face-search/{search_id}/selfie.jpg",
                file_name="selfie.jpg",
                content_type="image/jpeg",
                file_size=100,
                status="ready",
            )
        )
        session.commit()

        assert purge_expired_face_searches(session) == 1
        assert session.get(FaceSearchSession, search_id) is None
        task = session.scalar(select(ObjectDeletionTask))
        assert task is not None
        assert "/face-search/" in task.object_key


def test_object_deletion_is_retried_durably(monkeypatch) -> None:
    factory = make_session_factory()
    calls = 0

    def flaky_delete(*, object_key: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TimeoutError(object_key)

    monkeypatch.setattr("app.maintenance.delete_object", flaky_delete)
    with factory() as session:
        enqueue_object_deletion(session, "events/test/face-search/a.jpg")
        session.commit()
        completed, failed = process_object_deletions(session)
        assert (completed, failed) == (0, 1)
        task = session.scalar(select(ObjectDeletionTask))
        assert task is not None
        assert task.status == "queued"
        task.retry_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        session.commit()

        completed, failed = process_object_deletions(session)
        assert (completed, failed) == (1, 0)
        session.refresh(task)
        assert task.status == "completed"


def test_diagnostic_retention_writes_sql_null() -> None:
    factory = make_session_factory()
    with factory() as session:
        job = PhotoJob(
            photo_id=uuid.uuid4(),
            job_type="ocr",
            status="completed",
            raw_response_json={"provider": "test"},
            finished_at=datetime.now(timezone.utc) - timedelta(days=2),
        )
        session.add(job)
        session.commit()

        assert purge_old_job_diagnostics(session) == 1
        session.refresh(job)
        assert job.raw_response_json is None
        assert session.query(PhotoJob).filter(PhotoJob.raw_response_json.is_not(None)).count() == 0
