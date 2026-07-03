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


class AdminSessionLock(Base):
    __tablename__ = "admin_session_locks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[str] = mapped_column(String(128), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
