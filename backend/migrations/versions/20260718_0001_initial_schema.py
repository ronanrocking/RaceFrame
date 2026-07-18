"""Initial RaceFrame schema baseline.

Revision ID: 20260718_0001
Revises:
Create Date: 2026-07-18

This revision mirrors the schema that was originally created with SQLAlchemy
``create_all``. Existing production databases must be inspected and stamped at
this revision; they must not execute this revision over populated tables.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260718_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("slug", sa.String(length=255), nullable=False),
        sa.Column("event_date", sa.Date(), nullable=True),
        sa.Column("location", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_events_slug", "events", ["slug"], unique=True)
    op.create_index("ix_events_status", "events", ["status"], unique=False)

    op.create_table(
        "admin_session_locks",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("session_id", sa.String(length=128), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "participants",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("bib_number", sa.String(length=64), nullable=False),
        sa.Column("full_name", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_id", "bib_number", name="uq_participants_event_bib_number"),
    )
    op.create_index("ix_participants_event_id", "participants", ["event_id"], unique=False)
    op.create_index("ix_participants_full_name", "participants", ["full_name"], unique=False)

    op.create_table(
        "photos",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("original_object_key", sa.String(length=512), nullable=False),
        sa.Column("thumbnail_object_key", sa.String(length=512), nullable=True),
        sa.Column("file_name", sa.String(length=255), nullable=False),
        sa.Column("content_type", sa.String(length=128), nullable=False),
        sa.Column("file_size", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_photos_event_id", "photos", ["event_id"], unique=False)
    op.create_index("ix_photos_status", "photos", ["status"], unique=False)

    op.create_table(
        "photo_jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("photo_id", sa.Uuid(), nullable=False),
        sa.Column("job_type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("raw_response_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["photo_id"], ["photos.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_photo_jobs_photo_id", "photo_jobs", ["photo_id"], unique=False)
    op.create_index("ix_photo_jobs_status", "photo_jobs", ["status"], unique=False)

    op.create_table(
        "participant_face_images",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("participant_id", sa.Uuid(), nullable=False),
        sa.Column("object_key", sa.String(length=512), nullable=False),
        sa.Column("file_name", sa.String(length=255), nullable=False),
        sa.Column("content_type", sa.String(length=128), nullable=False),
        sa.Column("file_size", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["participant_id"], ["participants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_participant_face_images_event_id", "participant_face_images", ["event_id"], unique=False)
    op.create_index(
        "ix_participant_face_images_participant_id", "participant_face_images", ["participant_id"], unique=False
    )
    op.create_index("ix_participant_face_images_status", "participant_face_images", ["status"], unique=False)

    op.create_table(
        "participant_face_jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("participant_id", sa.Uuid(), nullable=False),
        sa.Column("face_image_id", sa.Uuid(), nullable=False),
        sa.Column("job_type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("raw_response_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["face_image_id"], ["participant_face_images.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["participant_id"], ["participants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_participant_face_jobs_event_id", "participant_face_jobs", ["event_id"], unique=False)
    op.create_index("ix_participant_face_jobs_face_image_id", "participant_face_jobs", ["face_image_id"], unique=False)
    op.create_index(
        "ix_participant_face_jobs_participant_id", "participant_face_jobs", ["participant_id"], unique=False
    )
    op.create_index("ix_participant_face_jobs_status", "participant_face_jobs", ["status"], unique=False)

    op.create_table(
        "participant_face_embeddings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("participant_id", sa.Uuid(), nullable=False),
        sa.Column("face_image_id", sa.Uuid(), nullable=False),
        sa.Column("embedding_json", sa.JSON(), nullable=False),
        sa.Column("bounding_box_json", sa.JSON(), nullable=True),
        sa.Column("detection_score", sa.Float(), nullable=True),
        sa.Column("quality_score", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["face_image_id"], ["participant_face_images.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["participant_id"], ["participants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_participant_face_embeddings_event_id", "participant_face_embeddings", ["event_id"], unique=False
    )
    op.create_index(
        "ix_participant_face_embeddings_face_image_id",
        "participant_face_embeddings",
        ["face_image_id"],
        unique=False,
    )
    op.create_index(
        "ix_participant_face_embeddings_participant_id",
        "participant_face_embeddings",
        ["participant_id"],
        unique=False,
    )

    op.create_table(
        "photo_text_detection",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("photo_id", sa.Uuid(), nullable=False),
        sa.Column("photo_job_id", sa.Uuid(), nullable=False),
        sa.Column("detected_text", sa.Text(), nullable=False),
        sa.Column("normalized_text", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("bounding_box_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["photo_id"], ["photos.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["photo_job_id"], ["photo_jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_photo_text_detection_photo_id", "photo_text_detection", ["photo_id"], unique=False)
    op.create_index(
        "ix_photo_text_detection_photo_job_id", "photo_text_detection", ["photo_job_id"], unique=False
    )

    op.create_table(
        "photo_participant_matches",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("photo_id", sa.Uuid(), nullable=False),
        sa.Column("participant_id", sa.Uuid(), nullable=False),
        sa.Column("match_source", sa.String(length=32), nullable=False),
        sa.Column("matched_value", sa.String(length=255), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["participant_id"], ["participants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["photo_id"], ["photos.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("photo_id", "participant_id", name="uq_photo_participant_matches_photo_participant"),
    )
    op.create_index("ix_photo_participant_matches_event_id", "photo_participant_matches", ["event_id"], unique=False)
    op.create_index(
        "ix_photo_participant_matches_participant_id", "photo_participant_matches", ["participant_id"], unique=False
    )
    op.create_index("ix_photo_participant_matches_photo_id", "photo_participant_matches", ["photo_id"], unique=False)

    op.create_table(
        "photo_face_detections",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("photo_id", sa.Uuid(), nullable=False),
        sa.Column("photo_job_id", sa.Uuid(), nullable=False),
        sa.Column("face_index", sa.Integer(), nullable=False),
        sa.Column("embedding_json", sa.JSON(), nullable=False),
        sa.Column("bounding_box_json", sa.JSON(), nullable=True),
        sa.Column("detection_score", sa.Float(), nullable=True),
        sa.Column("quality_score", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["photo_id"], ["photos.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["photo_job_id"], ["photo_jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("photo_job_id", "face_index", name="uq_photo_face_detections_job_face_index"),
    )
    op.create_index("ix_photo_face_detections_event_id", "photo_face_detections", ["event_id"], unique=False)
    op.create_index("ix_photo_face_detections_photo_id", "photo_face_detections", ["photo_id"], unique=False)
    op.create_index("ix_photo_face_detections_photo_job_id", "photo_face_detections", ["photo_job_id"], unique=False)

    op.create_table(
        "face_participant_matches",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("photo_id", sa.Uuid(), nullable=False),
        sa.Column("photo_face_detection_id", sa.Uuid(), nullable=False),
        sa.Column("participant_id", sa.Uuid(), nullable=False),
        sa.Column("similarity_score", sa.Float(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["participant_id"], ["participants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["photo_face_detection_id"], ["photo_face_detections.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["photo_id"], ["photos.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "photo_face_detection_id",
            "participant_id",
            name="uq_face_participant_matches_detection_participant",
        ),
    )
    op.create_index("ix_face_participant_matches_event_id", "face_participant_matches", ["event_id"], unique=False)
    op.create_index(
        "ix_face_participant_matches_participant_id", "face_participant_matches", ["participant_id"], unique=False
    )
    op.create_index(
        "ix_face_participant_matches_photo_face_detection_id",
        "face_participant_matches",
        ["photo_face_detection_id"],
        unique=False,
    )
    op.create_index("ix_face_participant_matches_photo_id", "face_participant_matches", ["photo_id"], unique=False)

    op.create_table(
        "face_search_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("participant_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["participant_id"], ["participants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_face_search_sessions_event_id", "face_search_sessions", ["event_id"], unique=False)
    op.create_index(
        "ix_face_search_sessions_participant_id", "face_search_sessions", ["participant_id"], unique=False
    )
    op.create_index("ix_face_search_sessions_status", "face_search_sessions", ["status"], unique=False)

    op.create_table(
        "face_search_images",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("search_session_id", sa.Uuid(), nullable=False),
        sa.Column("object_key", sa.String(length=512), nullable=False),
        sa.Column("file_name", sa.String(length=255), nullable=False),
        sa.Column("content_type", sa.String(length=128), nullable=False),
        sa.Column("file_size", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["search_session_id"], ["face_search_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_face_search_images_event_id", "face_search_images", ["event_id"], unique=False)
    op.create_index(
        "ix_face_search_images_search_session_id", "face_search_images", ["search_session_id"], unique=False
    )
    op.create_index("ix_face_search_images_status", "face_search_images", ["status"], unique=False)

    op.create_table(
        "face_search_jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("search_session_id", sa.Uuid(), nullable=False),
        sa.Column("search_image_id", sa.Uuid(), nullable=False),
        sa.Column("job_type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("raw_response_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["search_image_id"], ["face_search_images.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["search_session_id"], ["face_search_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_face_search_jobs_event_id", "face_search_jobs", ["event_id"], unique=False)
    op.create_index("ix_face_search_jobs_search_image_id", "face_search_jobs", ["search_image_id"], unique=False)
    op.create_index(
        "ix_face_search_jobs_search_session_id", "face_search_jobs", ["search_session_id"], unique=False
    )
    op.create_index("ix_face_search_jobs_status", "face_search_jobs", ["status"], unique=False)

    op.create_table(
        "face_search_results",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("search_session_id", sa.Uuid(), nullable=False),
        sa.Column("photo_id", sa.Uuid(), nullable=False),
        sa.Column("photo_face_detection_id", sa.Uuid(), nullable=False),
        sa.Column("similarity_score", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["photo_face_detection_id"], ["photo_face_detections.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["photo_id"], ["photos.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["search_session_id"], ["face_search_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "search_session_id",
            "photo_face_detection_id",
            name="uq_face_search_results_session_detection",
        ),
    )
    op.create_index("ix_face_search_results_event_id", "face_search_results", ["event_id"], unique=False)
    op.create_index(
        "ix_face_search_results_photo_face_detection_id",
        "face_search_results",
        ["photo_face_detection_id"],
        unique=False,
    )
    op.create_index("ix_face_search_results_photo_id", "face_search_results", ["photo_id"], unique=False)
    op.create_index(
        "ix_face_search_results_search_session_id", "face_search_results", ["search_session_id"], unique=False
    )


def downgrade() -> None:
    op.drop_table("face_search_results")
    op.drop_table("face_search_jobs")
    op.drop_table("face_search_images")
    op.drop_table("face_search_sessions")
    op.drop_table("face_participant_matches")
    op.drop_table("photo_face_detections")
    op.drop_table("photo_participant_matches")
    op.drop_table("photo_text_detection")
    op.drop_table("participant_face_embeddings")
    op.drop_table("participant_face_jobs")
    op.drop_table("participant_face_images")
    op.drop_table("photo_jobs")
    op.drop_table("photos")
    op.drop_table("participants")
    op.drop_table("admin_session_locks")
    op.drop_table("events")
