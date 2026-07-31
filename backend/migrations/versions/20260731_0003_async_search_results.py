"""Materialize temporary search results and move bib-only work off the request path.

Revision ID: 20260731_0003
Revises: 20260718_0002
Create Date: 2026-07-31
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260731_0003"
down_revision = "20260718_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "search_session_photo_results",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("search_session_id", sa.Uuid(), nullable=False),
        sa.Column("photo_id", sa.Uuid(), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("best_face_score", sa.Float(), nullable=True),
        sa.Column("face_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("evidence_labels", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("rank > 0", name="ck_search_session_photo_results_rank"),
        sa.CheckConstraint(
            "best_face_score IS NULL OR (best_face_score >= -1 AND best_face_score <= 1)",
            name="ck_search_session_photo_results_face_score",
        ),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["search_session_id"], ["face_search_sessions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["photo_id"], ["photos.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("search_session_id", "photo_id", name="uq_search_session_photo_results_session_photo"),
        sa.UniqueConstraint("search_session_id", "rank", name="uq_search_session_photo_results_session_rank"),
    )
    op.create_index("ix_search_session_photo_results_event_id", "search_session_photo_results", ["event_id"], unique=False)
    op.create_index("ix_search_session_photo_results_search_session_id", "search_session_photo_results", ["search_session_id"], unique=False)
    op.create_index("ix_search_session_photo_results_photo_id", "search_session_photo_results", ["photo_id"], unique=False)
    op.create_index(
        "ix_search_session_photo_results_page",
        "search_session_photo_results",
        ["search_session_id", "rank", "photo_id"],
        unique=False,
    )

    op.create_table(
        "bib_search_jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("search_session_id", sa.Uuid(), nullable=False),
        sa.Column("participant_id", sa.Uuid(), nullable=False),
        sa.Column("job_type", sa.String(length=32), nullable=False, server_default="bib_only_search"),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="queued"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("attempt_id", sa.Uuid(), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retry_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dead_lettered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("job_type = 'bib_only_search'", name="ck_bib_search_jobs_job_type"),
        sa.CheckConstraint(
            "status IN ('queued', 'processing', 'completed', 'failed', 'dead_lettered')",
            name="ck_bib_search_jobs_status",
        ),
        sa.CheckConstraint("attempt_count >= 0", name="ck_bib_search_jobs_attempt_count"),
        sa.CheckConstraint("max_attempts > 0", name="ck_bib_search_jobs_max_attempts"),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["search_session_id"], ["face_search_sessions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["participant_id"], ["participants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("search_session_id"),
    )
    op.create_index("ix_bib_search_jobs_event_id", "bib_search_jobs", ["event_id"], unique=False)
    op.create_index("ix_bib_search_jobs_search_session_id", "bib_search_jobs", ["search_session_id"], unique=False)
    op.create_index("ix_bib_search_jobs_participant_id", "bib_search_jobs", ["participant_id"], unique=False)
    op.create_index("ix_bib_search_jobs_status", "bib_search_jobs", ["status"], unique=False)
    op.create_index(
        "ix_bib_search_jobs_claim", "bib_search_jobs", ["status", "job_type", "retry_after", "created_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_bib_search_jobs_claim", table_name="bib_search_jobs")
    op.drop_index("ix_bib_search_jobs_status", table_name="bib_search_jobs")
    op.drop_index("ix_bib_search_jobs_participant_id", table_name="bib_search_jobs")
    op.drop_index("ix_bib_search_jobs_search_session_id", table_name="bib_search_jobs")
    op.drop_index("ix_bib_search_jobs_event_id", table_name="bib_search_jobs")
    op.drop_table("bib_search_jobs")
    op.drop_index("ix_search_session_photo_results_page", table_name="search_session_photo_results")
    op.drop_index("ix_search_session_photo_results_photo_id", table_name="search_session_photo_results")
    op.drop_index("ix_search_session_photo_results_search_session_id", table_name="search_session_photo_results")
    op.drop_index("ix_search_session_photo_results_event_id", table_name="search_session_photo_results")
    op.drop_table("search_session_photo_results")
