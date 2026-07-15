from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

from sqlalchemy import BigInteger, Date, DateTime, Float, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Event(Base):
    __tablename__ = "events"

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
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), nullable=False, index=True)
    bib_number: Mapped[str] = mapped_column(String(64), nullable=False)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
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

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), nullable=False, index=True)
    original_object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    thumbnail_object_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(128), nullable=False)
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
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

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    photo_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("photos.id", ondelete="CASCADE"), nullable=False, index=True)
    job_type: Mapped[str] = mapped_column(String(32), nullable=False, default="ocr")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued", index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
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

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), nullable=False, index=True)
    participant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("participants.id", ondelete="CASCADE"), nullable=False, index=True)
    face_image_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("participant_face_images.id", ondelete="CASCADE"), nullable=False, index=True)
    job_type: Mapped[str] = mapped_column(String(32), nullable=False, default="face_selfie_enroll")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued", index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_response_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    event: Mapped[Event] = relationship(back_populates="participant_face_jobs")
    participant: Mapped[Participant] = relationship(back_populates="face_jobs")
    face_image: Mapped[ParticipantFaceImage] = relationship(back_populates="jobs")


class ParticipantFaceEmbedding(Base):
    __tablename__ = "participant_face_embeddings"

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

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), nullable=False, index=True)
    participant_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("participants.id", ondelete="CASCADE"), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued", index=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
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

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), nullable=False, index=True)
    search_session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("face_search_sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    search_image_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("face_search_images.id", ondelete="CASCADE"), nullable=False, index=True)
    job_type: Mapped[str] = mapped_column(String(32), nullable=False, default="face_search_probe")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued", index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
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


class AdminSessionLock(Base):
    __tablename__ = "admin_session_locks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[str] = mapped_column(String(128), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
