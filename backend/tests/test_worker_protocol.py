from __future__ import annotations

import math
import unittest
import uuid
from datetime import timedelta
from types import SimpleNamespace

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, selectinload

from app.models import Base, Event, Photo, PhotoJob, WorkerHeartbeat, utc_now
from app.worker_api import (
    CompleteEmbeddingJobRequest,
    CompletePhotoJobRequest,
    FaceDetectionPayload,
    FailJobRequest,
    WorkerHeartbeatRequest,
    _claim_job,
    _fail_job,
    _verify_active_attempt,
    complete_photo_job,
    record_worker_heartbeat,
)


def unit_embedding() -> list[float]:
    return [1.0] + [0.0] * 511


class WorkerPayloadValidationTests(unittest.TestCase):
    def test_embedding_requires_exact_finite_512_dimensions(self) -> None:
        valid = CompleteEmbeddingJobRequest(attempt_id=uuid.uuid4(), embedding=unit_embedding())
        self.assertEqual(len(valid.embedding), 512)

        for invalid in ([1.0] * 511, [1.0] * 513, [math.nan] + [0.0] * 511):
            with self.subTest(length=len(invalid)):
                with self.assertRaises(ValueError):
                    CompleteEmbeddingJobRequest(attempt_id=uuid.uuid4(), embedding=invalid)

    def test_face_indexes_must_be_unique(self) -> None:
        face = {
            "face_index": 0,
            "embedding": unit_embedding(),
            "detection_score": 0.9,
        }
        with self.assertRaises(ValueError):
            CompletePhotoJobRequest(
                attempt_id=uuid.uuid4(),
                face_detections=[face, face],
            )

    def test_non_finite_scores_and_oversized_json_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            FaceDetectionPayload(
                face_index=0,
                embedding=unit_embedding(),
                detection_score=math.inf,
            )
        with self.assertRaises(ValueError):
            CompletePhotoJobRequest(
                attempt_id=uuid.uuid4(),
                raw_response_json={"oversized": "x" * (257 * 1024)},
            )


class AttemptOwnershipTests(unittest.TestCase):
    def test_only_current_unexpired_attempt_may_mutate_job(self) -> None:
        attempt_id = uuid.uuid4()
        job = SimpleNamespace(
            attempt_id=attempt_id,
            status="processing",
            lease_expires_at=utc_now() + timedelta(minutes=1),
        )
        self.assertEqual(_verify_active_attempt(job, attempt_id), "processing")

        with self.assertRaises(HTTPException) as stale:
            _verify_active_attempt(job, uuid.uuid4())
        self.assertEqual(stale.exception.status_code, 409)

        job.lease_expires_at = utc_now() - timedelta(seconds=1)
        with self.assertRaises(HTTPException) as expired:
            _verify_active_attempt(job, attempt_id)
        self.assertEqual(expired.exception.status_code, 409)

    def test_duplicate_completion_for_same_attempt_is_idempotent(self) -> None:
        attempt_id = uuid.uuid4()
        job = SimpleNamespace(
            attempt_id=attempt_id,
            status="completed",
            lease_expires_at=None,
        )
        self.assertEqual(
            _verify_active_attempt(job, attempt_id, allow_terminal=True),
            "completed",
        )


class DurableJobStateMachineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        event = Event(name="Test", slug="test")
        photo = Photo(
            event=event,
            original_object_key="events/test/photo.jpg",
            file_name="photo.jpg",
            content_type="image/jpeg",
            file_size=1_024,
        )
        self.job = PhotoJob(photo=photo, job_type="ocr", max_attempts=2)
        self.db.add_all([event, photo, self.job])
        self.db.commit()

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    def claim(self) -> PhotoJob:
        job = _claim_job(
            self.db,
            PhotoJob,
            job_type_condition=PhotoJob.job_type == "ocr",
            relationship_options=(selectinload(PhotoJob.photo),),
        )
        self.assertIsNotNone(job)
        return job

    def test_retry_then_dead_letter_has_bounded_attempts(self) -> None:
        first = self.claim()
        first_attempt = first.attempt_id
        result = _fail_job(
            self.db,
            first,
            FailJobRequest(
                attempt_id=first_attempt,
                error_message="provider unavailable",
                retryable=True,
                error_code="provider_unavailable",
            ),
        )
        self.assertEqual(result["status"], "retry_scheduled")
        first.retry_after = utc_now() - timedelta(seconds=1)
        self.db.commit()

        second = self.claim()
        self.assertNotEqual(second.attempt_id, first_attempt)
        self.assertEqual(second.attempt_count, 2)
        result = _fail_job(
            self.db,
            second,
            FailJobRequest(
                attempt_id=second.attempt_id,
                error_message="provider unavailable",
                retryable=True,
                error_code="provider_unavailable",
            ),
        )
        self.assertEqual(result["status"], "dead_lettered")
        self.assertEqual(second.photo.status, "failed")
        self.assertIsNone(
            _claim_job(
                self.db,
                PhotoJob,
                job_type_condition=PhotoJob.job_type == "ocr",
                relationship_options=(selectinload(PhotoJob.photo),),
            )
        )

    def test_completion_is_idempotent_and_stale_attempt_is_rejected(self) -> None:
        claimed = self.claim()
        payload = CompletePhotoJobRequest(attempt_id=claimed.attempt_id, detections=[])
        self.assertEqual(complete_photo_job(claimed.id, payload, self.db, None), {"status": "completed"})
        self.assertEqual(complete_photo_job(claimed.id, payload, self.db, None), {"status": "completed"})

        with self.assertRaises(HTTPException) as stale:
            complete_photo_job(
                claimed.id,
                CompletePhotoJobRequest(attempt_id=uuid.uuid4(), detections=[]),
                self.db,
                None,
            )
        self.assertEqual(stale.exception.status_code, 409)


class WorkerPresenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    def test_heartbeat_upsert_keeps_one_stable_worker_row(self) -> None:
        process_started_at = utc_now()
        idle = WorkerHeartbeatRequest(
            worker_id="worker-1",
            worker_version="release.1",
            started_at=process_started_at,
            status="idle",
        )
        self.assertEqual(record_worker_heartbeat(idle, self.db, None)["status"], "ok")
        started_at = self.db.get(WorkerHeartbeat, "worker-1").started_at

        job_id = uuid.uuid4()
        active = WorkerHeartbeatRequest(
            worker_id="worker-1",
            worker_version="release.2",
            started_at=process_started_at,
            status="active",
            current_job_id=job_id,
            current_job_type="ocr",
        )
        record_worker_heartbeat(active, self.db, None)
        rows = self.db.query(WorkerHeartbeat).all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].worker_version, "release.2")
        self.assertEqual(rows[0].current_job_id, job_id)
        self.assertEqual(rows[0].started_at, started_at)

    def test_idle_heartbeat_cannot_claim_a_current_job(self) -> None:
        invalid = WorkerHeartbeatRequest(
            worker_id="worker-1",
            worker_version="release.1",
            started_at=utc_now(),
            status="idle",
            current_job_id=uuid.uuid4(),
            current_job_type="ocr",
        )
        with self.assertRaises(HTTPException) as rejected:
            record_worker_heartbeat(invalid, self.db, None)
        self.assertEqual(rejected.exception.status_code, 422)


if __name__ == "__main__":
    unittest.main()
