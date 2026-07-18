from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base
from .participant_lookup import normalize_bib_lookup, normalize_name_lookup


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def default_face_search_expiry() -> datetime:
    """Keep temporary biometric search data bounded even if a caller omits a TTL."""

    return utc_now() + timedelta(hours=24)


def default_participant_bib_lookup(context) -> str:
    return normalize_bib_lookup(str(context.get_current_parameters().get("bib_number", "")))


def default_participant_name_lookup(context) -> str:
    return normalize_name_lookup(str(context.get_current_parameters().get("full_name", "")))


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (
        CheckConstraint("status IN ('draft', 'published')", name="ck_events_status"),
        Index("ix_events_status_date", "status", "event_date"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    event_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    location: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)

    participants: Mapped[list["Participant"]] = relationship(
        back_populates="event",
        cascade="all, delete-orphan",
    )
    photos: Mapped[list["Photo"]] = relationship(
        back_populates="event",
        cascade="all, delete-orphan",
    )
    photo_matches: Mapped[list["PhotoParticipantMatch"]] = relationship(
        back_populates="event",
        cascade="all, delete-orphan",
    )
    participant_face_images: Mapped[list["ParticipantFaceImage"]] = relationship(
        back_populates="event",
        cascade="all, delete-orphan",
    )
    participant_face_embeddings: Mapped[list["ParticipantFaceEmbedding"]] = relationship(
        back_populates="event",
        cascade="all, delete-orphan",
    )
    participant_face_jobs: Mapped[list["ParticipantFaceJob"]] = relationship(
        back_populates="event",
        cascade="all, delete-orphan",
    )
    photo_face_detections: Mapped[list["PhotoFaceDetection"]] = relationship(
        back_populates="event",
        cascade="all, delete-orphan",
    )
    face_matches: Mapped[list["FaceParticipantMatch"]] = relationship(
        back_populates="event",
        cascade="all, delete-orphan",
    )
    face_search_sessions: Mapped[list["FaceSearchSession"]] = relationship(
        back_populates="event",
        cascade="all, delete-orphan",
    )


class Participant(Base):
    __tablename__ = "participants"
    __table_args__ = (
        UniqueConstraint("event_id", "bib_number", name="uq_participants_event_bib_number"),
        UniqueConstraint("event_id", "bib_lookup", name="uq_participants_event_bib_lookup"),
        Index("ix_participants_event_name_lookup", "event_id", "name_lookup"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), nullable=False, index=True)
    bib_number: Mapped[str] = mapped_column(String(64), nullable=False)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    bib_lookup: Mapped[str] = mapped_column(String(64), nullable=False, default=default_participant_bib_lookup)
    name_lookup: Mapped[str] = mapped_column(String(255), nullable=False, default=default_participant_name_lookup)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)

    event: Mapped[Event] = relationship(back_populates="participants")
    photo_matches: Mapped[list["PhotoParticipantMatch"]] = relationship(
        back_populates="participant",
        cascade="all, delete-orphan",
    )
    face_images: Mapped[list["ParticipantFaceImage"]] = relationship(
        back_populates="participant",
        cascade="all, delete-orphan",
    )
    face_embeddings: Mapped[list["ParticipantFaceEmbedding"]] = relationship(
        back_populates="participant",
        cascade="all, delete-orphan",
    )
    face_jobs: Mapped[list["ParticipantFaceJob"]] = relationship(
        back_populates="participant",
        cascade="all, delete-orphan",
    )
    face_matches: Mapped[list["FaceParticipantMatch"]] = relationship(
        back_populates="participant",
        cascade="all, delete-orphan",
    )


class Photo(Base):
    __tablename__ = "photos"
    __table_args__ = (
        UniqueConstraint("event_id", "idempotency_key", name="uq_photos_event_idempotency_key"),
        UniqueConstraint("event_id", "checksum_sha256", name="uq_photos_event_checksum_sha256"),
        CheckConstraint(
            "status IN ('uploaded', 'processing', 'ready', 'partially_ready', 'failed', 'deleting')",
            name="ck_photos_status",
        ),
        CheckConstraint(
            "checksum_sha256 IS NULL OR length(checksum_sha256) = 64",
            name="ck_photos_checksum_sha256_length",
        ),
        CheckConstraint("file_size > 0", name="ck_photos_file_size_positive"),
        Index("ix_photos_event_status_created", "event_id", "status", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), nullable=False, index=True)
    original_object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    thumbnail_object_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(128), nullable=False)
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    checksum_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="uploaded", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)

    event: Mapped[Event] = relationship(back_populates="photos")
    jobs: Mapped[list["PhotoJob"]] = relationship(
        back_populates="photo",
        cascade="all, delete-orphan",
        order_by=lambda: PhotoJob.created_at.desc(),
    )
    detections: Mapped[list["PhotoTextDetection"]] = relationship(
        back_populates="photo",
        cascade="all, delete-orphan",
        order_by=lambda: PhotoTextDetection.created_at.desc(),
    )
    matches: Mapped[list["PhotoParticipantMatch"]] = relationship(
        back_populates="photo",
        cascade="all, delete-orphan",
        order_by=lambda: PhotoParticipantMatch.created_at.desc(),
    )
    face_detections: Mapped[list["PhotoFaceDetection"]] = relationship(
        back_populates="photo",
        cascade="all, delete-orphan",
        order_by=lambda: PhotoFaceDetection.created_at.desc(),
    )
    face_matches: Mapped[list["FaceParticipantMatch"]] = relationship(
        back_populates="photo",
        cascade="all, delete-orphan",
        order_by=lambda: FaceParticipantMatch.created_at.desc(),
    )


class PhotoJob(Base):
    __tablename__ = "photo_jobs"
    __table_args__ = (
        CheckConstraint("job_type IN ('ocr', 'face_photo_scan')", name="ck_photo_jobs_job_type"),
        CheckConstraint(
            "status IN ('queued', 'processing', 'completed', 'failed', 'dead_lettered')",
            name="ck_photo_jobs_status",
        ),
        CheckConstraint("attempt_count >= 0", name="ck_photo_jobs_attempt_count"),
        CheckConstraint("max_attempts > 0", name="ck_photo_jobs_max_attempts"),
        Index("ix_photo_jobs_claim", "status", "job_type", "retry_after", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    photo_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("photos.id", ondelete="CASCADE"), nullable=False, index=True)
    job_type: Mapped[str] = mapped_column(String(32), nullable=False, default="ocr")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued", index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    attempt_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retry_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dead_lettered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_response_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    photo: Mapped[Photo] = relationship(back_populates="jobs")
    detections: Mapped[list["PhotoTextDetection"]] = relationship(
        back_populates="photo_job",
        cascade="all, delete-orphan",
    )
    face_detections: Mapped[list["PhotoFaceDetection"]] = relationship(
        back_populates="photo_job",
        cascade="all, delete-orphan",
    )


class PhotoTextDetection(Base):
    __tablename__ = "photo_text_detection"
    __table_args__ = (
        CheckConstraint("confidence IS NULL OR (confidence >= 0 AND confidence <= 1)", name="ck_photo_text_confidence"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    photo_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("photos.id", ondelete="CASCADE"), nullable=False, index=True)
    photo_job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("photo_jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    detected_text: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_text: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    bounding_box_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)

    photo: Mapped[Photo] = relationship(back_populates="detections")
    photo_job: Mapped[PhotoJob] = relationship(back_populates="detections")


class PhotoParticipantMatch(Base):
    __tablename__ = "photo_participant_matches"
    __table_args__ = (
        UniqueConstraint("photo_id", "participant_id", name="uq_photo_participant_matches_photo_participant"),
        CheckConstraint("match_source IN ('ocr', 'face', 'ocr+face')", name="ck_photo_participant_match_source"),
        CheckConstraint("confidence IS NULL OR (confidence >= -1 AND confidence <= 1)", name="ck_photo_match_confidence"),
        Index("ix_photo_matches_event_participant", "event_id", "participant_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), nullable=False, index=True)
    photo_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("photos.id", ondelete="CASCADE"), nullable=False, index=True)
    participant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("participants.id", ondelete="CASCADE"), nullable=False, index=True)
    match_source: Mapped[str] = mapped_column(String(32), nullable=False, default="ocr")
    matched_value: Mapped[str] = mapped_column(String(255), nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="auto_matched")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)

    event: Mapped[Event] = relationship(back_populates="photo_matches")
    photo: Mapped[Photo] = relationship(back_populates="matches")
    participant: Mapped[Participant] = relationship(back_populates="photo_matches")


class ParticipantFaceImage(Base):
    __tablename__ = "participant_face_images"
    __table_args__ = (
        CheckConstraint("file_size > 0", name="ck_participant_face_images_file_size"),
        CheckConstraint("status IN ('queued', 'processing', 'ready', 'failed')", name="ck_participant_face_images_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), nullable=False, index=True)
    participant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("participants.id", ondelete="CASCADE"), nullable=False, index=True)
    object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(128), nullable=False)
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued", index=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)

    event: Mapped[Event] = relationship(back_populates="participant_face_images")
    participant: Mapped[Participant] = relationship(back_populates="face_images")
    embeddings: Mapped[list["ParticipantFaceEmbedding"]] = relationship(
        back_populates="face_image",
        cascade="all, delete-orphan",
    )
    jobs: Mapped[list["ParticipantFaceJob"]] = relationship(
        back_populates="face_image",
        cascade="all, delete-orphan",
        order_by=lambda: ParticipantFaceJob.created_at.desc(),
    )


class ParticipantFaceJob(Base):
    __tablename__ = "participant_face_jobs"
    __table_args__ = (
        CheckConstraint("job_type = 'face_selfie_enroll'", name="ck_participant_face_jobs_job_type"),
        CheckConstraint(
            "status IN ('queued', 'processing', 'completed', 'failed', 'dead_lettered')",
            name="ck_participant_face_jobs_status",
        ),
        CheckConstraint("attempt_count >= 0", name="ck_participant_face_jobs_attempt_count"),
        CheckConstraint("max_attempts > 0", name="ck_participant_face_jobs_max_attempts"),
        Index("ix_participant_face_jobs_claim", "status", "job_type", "retry_after", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), nullable=False, index=True)
    participant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("participants.id", ondelete="CASCADE"), nullable=False, index=True)
    face_image_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("participant_face_images.id", ondelete="CASCADE"), nullable=False, index=True)
    job_type: Mapped[str] = mapped_column(String(32), nullable=False, default="face_selfie_enroll")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued", index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    attempt_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retry_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dead_lettered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_response_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    event: Mapped[Event] = relationship(back_populates="participant_face_jobs")
    participant: Mapped[Participant] = relationship(back_populates="face_jobs")
    face_image: Mapped[ParticipantFaceImage] = relationship(back_populates="jobs")


class ParticipantFaceEmbedding(Base):
    __tablename__ = "participant_face_embeddings"
    __table_args__ = (
        UniqueConstraint("face_image_id", name="uq_participant_face_embedding_image"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), nullable=False, index=True)
    participant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("participants.id", ondelete="CASCADE"), nullable=False, index=True)
    face_image_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("participant_face_images.id", ondelete="CASCADE"), nullable=False, index=True)
    embedding_json: Mapped[list[float]] = mapped_column(JSON, nullable=False)
    bounding_box_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    detection_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    quality_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)

    event: Mapped[Event] = relationship(back_populates="participant_face_embeddings")
    participant: Mapped[Participant] = relationship(back_populates="face_embeddings")
    face_image: Mapped[ParticipantFaceImage] = relationship(back_populates="embeddings")


class PhotoFaceDetection(Base):
    __tablename__ = "photo_face_detections"
    __table_args__ = (
        UniqueConstraint("photo_job_id", "face_index", name="uq_photo_face_detections_job_face_index"),
        CheckConstraint("face_index >= 0", name="ck_photo_face_detection_index"),
        CheckConstraint("detection_score IS NULL OR (detection_score >= 0 AND detection_score <= 1)", name="ck_photo_face_detection_score"),
        CheckConstraint("quality_score IS NULL OR (quality_score >= 0 AND quality_score <= 1)", name="ck_photo_face_quality_score"),
        Index("ix_photo_face_detections_event_created", "event_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), nullable=False, index=True)
    photo_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("photos.id", ondelete="CASCADE"), nullable=False, index=True)
    photo_job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("photo_jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    face_index: Mapped[int] = mapped_column(Integer, nullable=False)
    embedding_json: Mapped[list[float]] = mapped_column(JSON, nullable=False)
    bounding_box_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    detection_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    quality_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)

    event: Mapped[Event] = relationship(back_populates="photo_face_detections")
    photo: Mapped[Photo] = relationship(back_populates="face_detections")
    photo_job: Mapped[PhotoJob] = relationship(back_populates="face_detections")
    participant_matches: Mapped[list["FaceParticipantMatch"]] = relationship(
        back_populates="photo_face_detection",
        cascade="all, delete-orphan",
    )


class FaceParticipantMatch(Base):
    __tablename__ = "face_participant_matches"
    __table_args__ = (
        UniqueConstraint("photo_face_detection_id", "participant_id", name="uq_face_participant_matches_detection_participant"),
        CheckConstraint("similarity_score >= -1 AND similarity_score <= 1", name="ck_face_participant_similarity"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), nullable=False, index=True)
    photo_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("photos.id", ondelete="CASCADE"), nullable=False, index=True)
    photo_face_detection_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("photo_face_detections.id", ondelete="CASCADE"), nullable=False, index=True)
    participant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("participants.id", ondelete="CASCADE"), nullable=False, index=True)
    similarity_score: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="auto_matched")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)

    event: Mapped[Event] = relationship(back_populates="face_matches")
    photo: Mapped[Photo] = relationship(back_populates="face_matches")
    photo_face_detection: Mapped[PhotoFaceDetection] = relationship(back_populates="participant_matches")
    participant: Mapped[Participant] = relationship(back_populates="face_matches")


class FaceSearchSession(Base):
    __tablename__ = "face_search_sessions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'processing', 'completed', 'failed', 'expired')",
            name="ck_face_search_sessions_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), nullable=False, index=True)
    participant_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("participants.id", ondelete="CASCADE"), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued", index=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=default_face_search_expiry,
        index=True,
    )
    capability_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True, index=True)
    owner_binding_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    event: Mapped[Event] = relationship(back_populates="face_search_sessions")
    participant: Mapped[Participant | None] = relationship()
    images: Mapped[list["FaceSearchImage"]] = relationship(
        back_populates="search_session",
        cascade="all, delete-orphan",
        order_by=lambda: FaceSearchImage.created_at.desc(),
    )
    jobs: Mapped[list["FaceSearchJob"]] = relationship(
        back_populates="search_session",
        cascade="all, delete-orphan",
        order_by=lambda: FaceSearchJob.created_at.desc(),
    )
    results: Mapped[list["FaceSearchResult"]] = relationship(
        back_populates="search_session",
        cascade="all, delete-orphan",
        order_by=lambda: FaceSearchResult.similarity_score.desc(),
    )


class FaceSearchImage(Base):
    __tablename__ = "face_search_images"
    __table_args__ = (
        CheckConstraint("file_size > 0", name="ck_face_search_images_file_size"),
        CheckConstraint("status IN ('queued', 'processing', 'ready', 'failed')", name="ck_face_search_images_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), nullable=False, index=True)
    search_session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("face_search_sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(128), nullable=False)
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued", index=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)

    search_session: Mapped[FaceSearchSession] = relationship(back_populates="images")
    jobs: Mapped[list["FaceSearchJob"]] = relationship(
        back_populates="search_image",
        cascade="all, delete-orphan",
        order_by=lambda: FaceSearchJob.created_at.desc(),
    )


class FaceSearchJob(Base):
    __tablename__ = "face_search_jobs"
    __table_args__ = (
        CheckConstraint("job_type = 'face_search_probe'", name="ck_face_search_jobs_job_type"),
        CheckConstraint(
            "status IN ('queued', 'processing', 'completed', 'failed', 'dead_lettered')",
            name="ck_face_search_jobs_status",
        ),
        CheckConstraint("attempt_count >= 0", name="ck_face_search_jobs_attempt_count"),
        CheckConstraint("max_attempts > 0", name="ck_face_search_jobs_max_attempts"),
        Index("ix_face_search_jobs_claim", "status", "job_type", "retry_after", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), nullable=False, index=True)
    search_session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("face_search_sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    search_image_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("face_search_images.id", ondelete="CASCADE"), nullable=False, index=True)
    job_type: Mapped[str] = mapped_column(String(32), nullable=False, default="face_search_probe")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued", index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    attempt_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retry_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dead_lettered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_response_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    search_session: Mapped[FaceSearchSession] = relationship(back_populates="jobs")
    search_image: Mapped[FaceSearchImage] = relationship(back_populates="jobs")


class FaceSearchResult(Base):
    __tablename__ = "face_search_results"
    __table_args__ = (
        UniqueConstraint("search_session_id", "photo_face_detection_id", name="uq_face_search_results_session_detection"),
        CheckConstraint("similarity_score >= -1 AND similarity_score <= 1", name="ck_face_search_result_similarity"),
        Index("ix_face_search_results_session_score", "search_session_id", "similarity_score"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), nullable=False, index=True)
    search_session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("face_search_sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    photo_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("photos.id", ondelete="CASCADE"), nullable=False, index=True)
    photo_face_detection_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("photo_face_detections.id", ondelete="CASCADE"), nullable=False, index=True)
    similarity_score: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)

    search_session: Mapped[FaceSearchSession] = relationship(back_populates="results")
    photo: Mapped[Photo] = relationship()
    photo_face_detection: Mapped[PhotoFaceDetection] = relationship()


class ObjectDeletionTask(Base):
    """Durable tombstone for eventual R2 deletion and retry reconciliation."""

    __tablename__ = "object_deletion_tasks"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'processing', 'completed', 'dead_lettered')",
            name="ck_object_deletion_tasks_status",
        ),
        CheckConstraint("attempt_count >= 0", name="ck_object_deletion_tasks_attempt_count"),
        CheckConstraint("max_attempts > 0", name="ck_object_deletion_tasks_max_attempts"),
        Index("ix_object_deletion_tasks_claim", "status", "retry_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    object_key: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued", index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    attempt_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dead_lettered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RateLimitBucket(Base):
    """Cross-process fixed-window counters keyed only by a privacy-preserving hash."""

    __tablename__ = "rate_limit_buckets"
    __table_args__ = (
        CheckConstraint("count >= 0", name="ck_rate_limit_buckets_count"),
        CheckConstraint("length(key_hash) = 64", name="ck_rate_limit_buckets_key_hash_length"),
        Index("ix_rate_limit_buckets_expires_at", "expires_at"),
    )

    bucket: Mapped[str] = mapped_column(String(64), primary_key=True)
    key_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )


class AdminAuditLog(Base):
    """Append-only record of privileged operations; callers must never update rows."""

    __tablename__ = "admin_audit_logs"
    __table_args__ = (
        CheckConstraint("length(action) > 0", name="ck_admin_audit_logs_action_nonempty"),
        CheckConstraint(
            "metadata_json IS NULL OR length(CAST(metadata_json AS TEXT)) <= 16384",
            name="ck_admin_audit_logs_metadata_size",
        ),
        Index("ix_admin_audit_logs_occurred_at", "occurred_at"),
        Index("ix_admin_audit_logs_event_occurred", "event_id", "occurred_at"),
        Index("ix_admin_audit_logs_target", "target_type", "target_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    actor_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    actor_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    target_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    target_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    event_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("events.id", ondelete="SET NULL"),
        nullable=True,
    )
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    metadata_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class WorkerHeartbeat(Base):
    """Latest liveness state for an explicitly named, non-host-identifying worker."""

    __tablename__ = "worker_heartbeats"
    __table_args__ = (
        CheckConstraint("length(worker_id) > 0", name="ck_worker_heartbeats_worker_id_nonempty"),
        CheckConstraint(
            "status IN ('idle', 'active', 'draining')",
            name="ck_worker_heartbeats_status",
        ),
        CheckConstraint(
            "current_job_type IS NULL OR current_job_type IN "
            "('ocr', 'face_photo_scan', 'face_selfie_enroll', 'face_search_probe')",
            name="ck_worker_heartbeats_job_type",
        ),
        CheckConstraint(
            "(status = 'active' AND current_job_id IS NOT NULL AND current_job_type IS NOT NULL) OR "
            "(status IN ('idle', 'draining') AND current_job_id IS NULL AND current_job_type IS NULL)",
            name="ck_worker_heartbeats_state_job",
        ),
        Index("ix_worker_heartbeats_last_seen_at", "last_seen_at"),
        Index("ix_worker_heartbeats_status_last_seen", "status", "last_seen_at"),
    )

    worker_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    worker_version: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="idle")
    current_job_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    current_job_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class AdminSessionLock(Base):
    __tablename__ = "admin_session_locks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[str] = mapped_column(String(128), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
