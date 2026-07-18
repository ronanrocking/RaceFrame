"""Add durable jobs, bounded search capabilities, and deletion outbox.

Revision ID: 20260718_0002
Revises: 20260718_0001
Create Date: 2026-07-18
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260718_0002"
down_revision = "20260718_0001"
branch_labels = None
depends_on = None


JOB_TABLES = ("photo_jobs", "participant_face_jobs", "face_search_jobs")


def _reject_unknown_values(table: str, column: str, allowed: tuple[str, ...]) -> None:
    bind = op.get_bind()
    placeholders = ", ".join(f":value_{index}" for index in range(len(allowed)))
    params = {f"value_{index}": value for index, value in enumerate(allowed)}
    values = bind.execute(
        sa.text(f"SELECT DISTINCT {column} FROM {table} WHERE {column} NOT IN ({placeholders})"),  # nosec B608
        params,
    ).scalars().all()
    if values:
        raise RuntimeError(f"Cannot constrain {table}.{column}; unexpected values: {values!r}")


def _reject_query(description: str, query: str) -> None:
    if op.get_bind().execute(sa.text(query)).first() is not None:
        raise RuntimeError(f"Migration preflight failed: {description}")


def _reject_cross_event_rows() -> None:
    checks = {
        "face_search_sessions contains a cross-event participant reference": """
            SELECT 1 FROM face_search_sessions s
            JOIN participants p ON p.id = s.participant_id
            WHERE s.participant_id IS NOT NULL AND s.event_id <> p.event_id LIMIT 1
        """,
        "photo_participant_matches contains cross-event references": """
            SELECT 1 FROM photo_participant_matches m
            JOIN photos p ON p.id = m.photo_id
            JOIN participants r ON r.id = m.participant_id
            WHERE m.event_id <> p.event_id OR m.event_id <> r.event_id LIMIT 1
        """,
        "photo_face_detections contains cross-event or cross-photo references": """
            SELECT 1 FROM photo_face_detections d
            JOIN photos p ON p.id = d.photo_id
            JOIN photo_jobs j ON j.id = d.photo_job_id
            WHERE d.event_id <> p.event_id OR j.photo_id <> d.photo_id LIMIT 1
        """,
        "face_participant_matches contains cross-event references": """
            SELECT 1 FROM face_participant_matches m
            JOIN photos p ON p.id = m.photo_id
            JOIN participants r ON r.id = m.participant_id
            JOIN photo_face_detections d ON d.id = m.photo_face_detection_id
            WHERE m.event_id <> p.event_id OR m.event_id <> r.event_id
               OR m.event_id <> d.event_id OR m.photo_id <> d.photo_id LIMIT 1
        """,
        "face_search_images contains cross-event references": """
            SELECT 1 FROM face_search_images i
            JOIN face_search_sessions s ON s.id = i.search_session_id
            WHERE i.event_id <> s.event_id LIMIT 1
        """,
        "face_search_jobs contains cross-event or cross-session references": """
            SELECT 1 FROM face_search_jobs j
            JOIN face_search_sessions s ON s.id = j.search_session_id
            JOIN face_search_images i ON i.id = j.search_image_id
            WHERE j.event_id <> s.event_id OR j.event_id <> i.event_id
               OR j.search_session_id <> i.search_session_id LIMIT 1
        """,
        "face_search_results contains cross-event references": """
            SELECT 1 FROM face_search_results r
            JOIN face_search_sessions s ON s.id = r.search_session_id
            JOIN photos p ON p.id = r.photo_id
            JOIN photo_face_detections d ON d.id = r.photo_face_detection_id
            WHERE r.event_id <> s.event_id OR r.event_id <> p.event_id
               OR r.event_id <> d.event_id OR r.photo_id <> d.photo_id LIMIT 1
        """,
    }
    for description, query in checks.items():
        _reject_query(description, query)


def _add_job_lease_columns(table: str) -> None:
    op.add_column(table, sa.Column("max_attempts", sa.Integer(), server_default="5", nullable=False))
    op.add_column(table, sa.Column("attempt_id", sa.Uuid(), nullable=True))
    op.add_column(table, sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(table, sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(table, sa.Column("retry_after", sa.DateTime(timezone=True), nullable=True))
    op.add_column(table, sa.Column("dead_lettered_at", sa.DateTime(timezone=True), nullable=True))
    op.alter_column(table, "max_attempts", server_default=None)


def _drop_job_lease_columns(table: str) -> None:
    op.drop_column(table, "dead_lettered_at")
    op.drop_column(table, "retry_after")
    op.drop_column(table, "lease_expires_at")
    op.drop_column(table, "claimed_at")
    op.drop_column(table, "attempt_id")
    op.drop_column(table, "max_attempts")


def upgrade() -> None:
    _reject_unknown_values("events", "status", ("draft", "published"))
    _reject_unknown_values(
        "photos",
        "status",
        ("uploaded", "processing", "ready", "partially_ready", "failed", "deleting"),
    )
    _reject_unknown_values("photo_jobs", "job_type", ("ocr", "face_photo_scan"))
    _reject_unknown_values("participant_face_jobs", "job_type", ("face_selfie_enroll",))
    _reject_unknown_values("face_search_jobs", "job_type", ("face_search_probe",))
    for table in JOB_TABLES:
        _reject_unknown_values(table, "status", ("queued", "processing", "completed", "failed", "dead_lettered"))

    _reject_unknown_values("participant_face_images", "status", ("queued", "processing", "ready", "failed"))
    _reject_unknown_values("face_search_images", "status", ("queued", "processing", "ready", "failed"))
    _reject_unknown_values("photo_participant_matches", "match_source", ("ocr", "face", "ocr+face"))
    _reject_query("photos has non-positive file_size", "SELECT 1 FROM photos WHERE file_size <= 0 LIMIT 1")
    _reject_query(
        "participant_face_images has non-positive file_size",
        "SELECT 1 FROM participant_face_images WHERE file_size <= 0 LIMIT 1",
    )
    _reject_query(
        "face_search_images has non-positive file_size",
        "SELECT 1 FROM face_search_images WHERE file_size <= 0 LIMIT 1",
    )
    _reject_query(
        "photo_text_detection confidence is outside [0,1]",
        "SELECT 1 FROM photo_text_detection WHERE confidence IS NOT NULL AND (confidence < 0 OR confidence > 1) LIMIT 1",
    )
    _reject_query(
        "photo_participant_matches confidence is outside [-1,1]",
        "SELECT 1 FROM photo_participant_matches WHERE confidence IS NOT NULL AND (confidence < -1 OR confidence > 1) LIMIT 1",
    )
    _reject_query(
        "photo_face_detections contains invalid indexes or scores",
        """SELECT 1 FROM photo_face_detections
           WHERE face_index < 0
              OR (detection_score IS NOT NULL AND (detection_score < 0 OR detection_score > 1))
              OR (quality_score IS NOT NULL AND (quality_score < 0 OR quality_score > 1)) LIMIT 1""",
    )
    # Legacy cosine calculations can exceed the mathematical range by a few
    # machine epsilons (for example 1.0000000000000002). Clamp only that tiny
    # rounding envelope; materially invalid values still fail the preflight.
    for table in ("face_participant_matches", "face_search_results"):
        op.execute(
            f"""UPDATE {table}
                SET similarity_score = greatest(-1.0, least(1.0, similarity_score))
                WHERE (similarity_score > 1.0 AND similarity_score <= 1.000000001)
                   OR (similarity_score < -1.0 AND similarity_score >= -1.000000001)"""  # nosec B608
        )
    _reject_query(
        "face_participant_matches similarity is outside [-1,1]",
        "SELECT 1 FROM face_participant_matches WHERE similarity_score < -1 OR similarity_score > 1 LIMIT 1",
    )
    _reject_query(
        "face_search_results similarity is outside [-1,1]",
        "SELECT 1 FROM face_search_results WHERE similarity_score < -1 OR similarity_score > 1 LIMIT 1",
    )
    _reject_query(
        "participant_face_embeddings has duplicate face_image_id values",
        "SELECT 1 FROM participant_face_embeddings GROUP BY face_image_id HAVING count(*) > 1 LIMIT 1",
    )
    _reject_cross_event_rows()

    # Some legacy deployments added participant_id before Alembic adoption but
    # missed its foreign key. Fresh 0001 databases already have it.
    participant_foreign_keys = {
        tuple(foreign_key.get("constrained_columns") or ())
        for foreign_key in sa.inspect(op.get_bind()).get_foreign_keys("face_search_sessions")
    }
    if ("participant_id",) not in participant_foreign_keys:
        op.create_foreign_key(
            "fk_face_search_sessions_participant_id",
            "face_search_sessions",
            "participants",
            ["participant_id"],
            ["id"],
            ondelete="CASCADE",
        )

    op.create_check_constraint("ck_events_status", "events", "status IN ('draft', 'published')")
    op.create_index("ix_events_status_date", "events", ["status", "event_date"], unique=False)

    op.add_column("participants", sa.Column("bib_lookup", sa.String(length=64), nullable=True))
    op.add_column("participants", sa.Column("name_lookup", sa.String(length=255), nullable=True))
    op.execute(
        """
        UPDATE participants
        SET bib_lookup = left(regexp_replace(upper(bib_number), '[^A-Z0-9]+', '', 'g'), 64),
            name_lookup = left(lower(regexp_replace(btrim(full_name), '[[:space:]]+', ' ', 'g')), 255)
        """
    )
    _reject_query(
        "participants contains a bib that has no ASCII letters or digits",
        "SELECT 1 FROM participants WHERE bib_lookup = '' LIMIT 1",
    )
    _reject_query(
        "participants contains duplicate normalized bib values within an event",
        """SELECT 1 FROM participants
           GROUP BY event_id, bib_lookup HAVING count(*) > 1 LIMIT 1""",
    )
    op.alter_column("participants", "bib_lookup", nullable=False)
    op.alter_column("participants", "name_lookup", nullable=False)
    op.create_unique_constraint(
        "uq_participants_event_bib_lookup",
        "participants",
        ["event_id", "bib_lookup"],
    )
    op.create_index(
        "ix_participants_event_name_lookup",
        "participants",
        ["event_id", "name_lookup"],
        unique=False,
    )

    op.add_column("photos", sa.Column("checksum_sha256", sa.String(length=64), nullable=True))
    op.add_column("photos", sa.Column("idempotency_key", sa.String(length=128), nullable=True))
    op.create_index("ix_photos_checksum_sha256", "photos", ["checksum_sha256"], unique=False)
    op.create_unique_constraint(
        "uq_photos_event_idempotency_key",
        "photos",
        ["event_id", "idempotency_key"],
    )
    op.create_unique_constraint(
        "uq_photos_event_checksum_sha256",
        "photos",
        ["event_id", "checksum_sha256"],
    )
    op.create_check_constraint(
        "ck_photos_status",
        "photos",
        "status IN ('uploaded', 'processing', 'ready', 'partially_ready', 'failed', 'deleting')",
    )
    op.create_check_constraint(
        "ck_photos_checksum_sha256_length",
        "photos",
        "checksum_sha256 IS NULL OR length(checksum_sha256) = 64",
    )
    op.create_check_constraint("ck_photos_file_size_positive", "photos", "file_size > 0")
    op.create_index(
        "ix_photos_event_status_created",
        "photos",
        ["event_id", "status", "created_at"],
        unique=False,
    )

    for table in JOB_TABLES:
        _add_job_lease_columns(table)

    op.create_check_constraint("ck_photo_jobs_job_type", "photo_jobs", "job_type IN ('ocr', 'face_photo_scan')")
    op.create_check_constraint(
        "ck_photo_jobs_status",
        "photo_jobs",
        "status IN ('queued', 'processing', 'completed', 'failed', 'dead_lettered')",
    )
    op.create_check_constraint("ck_photo_jobs_attempt_count", "photo_jobs", "attempt_count >= 0")
    op.create_check_constraint("ck_photo_jobs_max_attempts", "photo_jobs", "max_attempts > 0")
    op.create_index(
        "ix_photo_jobs_claim",
        "photo_jobs",
        ["status", "job_type", "retry_after", "created_at"],
        unique=False,
    )

    op.create_check_constraint(
        "ck_participant_face_jobs_job_type",
        "participant_face_jobs",
        "job_type = 'face_selfie_enroll'",
    )
    op.create_check_constraint(
        "ck_participant_face_jobs_status",
        "participant_face_jobs",
        "status IN ('queued', 'processing', 'completed', 'failed', 'dead_lettered')",
    )
    op.create_check_constraint(
        "ck_participant_face_jobs_attempt_count", "participant_face_jobs", "attempt_count >= 0"
    )
    op.create_check_constraint("ck_participant_face_jobs_max_attempts", "participant_face_jobs", "max_attempts > 0")
    op.create_index(
        "ix_participant_face_jobs_claim",
        "participant_face_jobs",
        ["status", "job_type", "retry_after", "created_at"],
        unique=False,
    )

    op.create_check_constraint(
        "ck_face_search_jobs_job_type", "face_search_jobs", "job_type = 'face_search_probe'"
    )
    op.create_check_constraint(
        "ck_face_search_jobs_status",
        "face_search_jobs",
        "status IN ('queued', 'processing', 'completed', 'failed', 'dead_lettered')",
    )
    op.create_check_constraint("ck_face_search_jobs_attempt_count", "face_search_jobs", "attempt_count >= 0")
    op.create_check_constraint("ck_face_search_jobs_max_attempts", "face_search_jobs", "max_attempts > 0")
    op.create_index(
        "ix_face_search_jobs_claim",
        "face_search_jobs",
        ["status", "job_type", "retry_after", "created_at"],
        unique=False,
    )

    op.create_check_constraint(
        "ck_photo_text_confidence",
        "photo_text_detection",
        "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
    )
    op.create_check_constraint(
        "ck_photo_participant_match_source",
        "photo_participant_matches",
        "match_source IN ('ocr', 'face', 'ocr+face')",
    )
    op.create_check_constraint(
        "ck_photo_match_confidence",
        "photo_participant_matches",
        "confidence IS NULL OR (confidence >= -1 AND confidence <= 1)",
    )
    op.create_index(
        "ix_photo_matches_event_participant",
        "photo_participant_matches",
        ["event_id", "participant_id", "created_at"],
        unique=False,
    )
    op.create_check_constraint(
        "ck_participant_face_images_file_size",
        "participant_face_images",
        "file_size > 0",
    )
    op.create_check_constraint(
        "ck_participant_face_images_status",
        "participant_face_images",
        "status IN ('queued', 'processing', 'ready', 'failed')",
    )
    op.create_unique_constraint(
        "uq_participant_face_embedding_image",
        "participant_face_embeddings",
        ["face_image_id"],
    )
    op.create_check_constraint(
        "ck_photo_face_detection_index",
        "photo_face_detections",
        "face_index >= 0",
    )
    op.create_check_constraint(
        "ck_photo_face_detection_score",
        "photo_face_detections",
        "detection_score IS NULL OR (detection_score >= 0 AND detection_score <= 1)",
    )
    op.create_check_constraint(
        "ck_photo_face_quality_score",
        "photo_face_detections",
        "quality_score IS NULL OR (quality_score >= 0 AND quality_score <= 1)",
    )
    op.create_index(
        "ix_photo_face_detections_event_created",
        "photo_face_detections",
        ["event_id", "created_at"],
        unique=False,
    )
    op.create_check_constraint(
        "ck_face_participant_similarity",
        "face_participant_matches",
        "similarity_score >= -1 AND similarity_score <= 1",
    )
    op.create_check_constraint(
        "ck_face_search_images_file_size",
        "face_search_images",
        "file_size > 0",
    )
    op.create_check_constraint(
        "ck_face_search_images_status",
        "face_search_images",
        "status IN ('queued', 'processing', 'ready', 'failed')",
    )
    op.create_check_constraint(
        "ck_face_search_result_similarity",
        "face_search_results",
        "similarity_score >= -1 AND similarity_score <= 1",
    )
    op.create_index(
        "ix_face_search_results_session_score",
        "face_search_results",
        ["search_session_id", "similarity_score"],
        unique=False,
    )

    _reject_unknown_values(
        "face_search_sessions",
        "status",
        ("queued", "processing", "completed", "failed", "expired"),
    )
    op.add_column("face_search_sessions", sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("face_search_sessions", sa.Column("capability_hash", sa.String(length=64), nullable=True))
    op.add_column("face_search_sessions", sa.Column("owner_binding_hash", sa.String(length=64), nullable=True))
    op.execute("UPDATE face_search_sessions SET expires_at = created_at + INTERVAL '24 hours' WHERE expires_at IS NULL")
    op.alter_column("face_search_sessions", "expires_at", nullable=False)
    op.create_index("ix_face_search_sessions_expires_at", "face_search_sessions", ["expires_at"], unique=False)
    op.create_index(
        "ix_face_search_sessions_capability_hash",
        "face_search_sessions",
        ["capability_hash"],
        unique=True,
    )
    op.create_index(
        "ix_face_search_sessions_owner_binding_hash",
        "face_search_sessions",
        ["owner_binding_hash"],
        unique=False,
    )
    op.create_check_constraint(
        "ck_face_search_sessions_status",
        "face_search_sessions",
        "status IN ('queued', 'processing', 'completed', 'failed', 'expired')",
    )

    op.create_table(
        "object_deletion_tasks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("object_key", sa.String(length=512), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("attempt_id", sa.Uuid(), nullable=True),
        sa.Column("retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dead_lettered_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('queued', 'processing', 'completed', 'dead_lettered')",
            name="ck_object_deletion_tasks_status",
        ),
        sa.CheckConstraint("attempt_count >= 0", name="ck_object_deletion_tasks_attempt_count"),
        sa.CheckConstraint("max_attempts > 0", name="ck_object_deletion_tasks_max_attempts"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("object_key"),
    )
    op.create_index("ix_object_deletion_tasks_status", "object_deletion_tasks", ["status"], unique=False)
    op.create_index(
        "ix_object_deletion_tasks_claim",
        "object_deletion_tasks",
        ["status", "retry_at", "created_at"],
        unique=False,
    )

    op.create_table(
        "rate_limit_buckets",
        sa.Column("bucket", sa.String(length=64), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("count >= 0", name="ck_rate_limit_buckets_count"),
        sa.CheckConstraint("length(key_hash) = 64", name="ck_rate_limit_buckets_key_hash_length"),
        sa.PrimaryKeyConstraint("bucket", "key_hash", "window_start"),
    )
    op.create_index(
        "ix_rate_limit_buckets_expires_at",
        "rate_limit_buckets",
        ["expires_at"],
        unique=False,
    )

    op.create_table(
        "admin_audit_logs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor_id", sa.String(length=255), nullable=True),
        sa.Column("actor_email", sa.String(length=320), nullable=True),
        sa.Column("action", sa.String(length=128), nullable=False),
        sa.Column("target_type", sa.String(length=64), nullable=True),
        sa.Column("target_id", sa.String(length=128), nullable=True),
        sa.Column("event_id", sa.Uuid(), nullable=True),
        sa.Column("request_id", sa.String(length=64), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.CheckConstraint("length(action) > 0", name="ck_admin_audit_logs_action_nonempty"),
        sa.CheckConstraint(
            "metadata_json IS NULL OR length(CAST(metadata_json AS TEXT)) <= 16384",
            name="ck_admin_audit_logs_metadata_size",
        ),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_admin_audit_logs_actor_id", "admin_audit_logs", ["actor_id"], unique=False)
    op.create_index("ix_admin_audit_logs_request_id", "admin_audit_logs", ["request_id"], unique=False)
    op.create_index("ix_admin_audit_logs_occurred_at", "admin_audit_logs", ["occurred_at"], unique=False)
    op.create_index(
        "ix_admin_audit_logs_event_occurred",
        "admin_audit_logs",
        ["event_id", "occurred_at"],
        unique=False,
    )
    op.create_index(
        "ix_admin_audit_logs_target",
        "admin_audit_logs",
        ["target_type", "target_id"],
        unique=False,
    )

    op.create_table(
        "worker_heartbeats",
        sa.Column("worker_id", sa.String(length=128), nullable=False),
        sa.Column("worker_version", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("current_job_id", sa.Uuid(), nullable=True),
        sa.Column("current_job_type", sa.String(length=32), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("length(worker_id) > 0", name="ck_worker_heartbeats_worker_id_nonempty"),
        sa.CheckConstraint(
            "status IN ('idle', 'active', 'draining')",
            name="ck_worker_heartbeats_status",
        ),
        sa.CheckConstraint(
            "current_job_type IS NULL OR current_job_type IN "
            "('ocr', 'face_photo_scan', 'face_selfie_enroll', 'face_search_probe')",
            name="ck_worker_heartbeats_job_type",
        ),
        sa.CheckConstraint(
            "(status = 'active' AND current_job_id IS NOT NULL AND current_job_type IS NOT NULL) OR "
            "(status IN ('idle', 'draining') AND current_job_id IS NULL AND current_job_type IS NULL)",
            name="ck_worker_heartbeats_state_job",
        ),
        sa.PrimaryKeyConstraint("worker_id"),
    )
    op.create_index("ix_worker_heartbeats_last_seen_at", "worker_heartbeats", ["last_seen_at"], unique=False)
    op.create_index(
        "ix_worker_heartbeats_status_last_seen",
        "worker_heartbeats",
        ["status", "last_seen_at"],
        unique=False,
    )

    # Foreign keys prove existence, but several hot tables duplicate event_id
    # for query speed. A deferred constraint trigger keeps those denormalized
    # keys consistent across every write path and remains safe for transactions
    # that build related rows in multiple statements.
    op.execute(
        """
        CREATE FUNCTION raceframe_assert_event_scope() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          CASE TG_TABLE_NAME
            WHEN 'photo_participant_matches' THEN
              IF NOT EXISTS (
                SELECT 1 FROM photos p, participants r
                WHERE p.id = NEW.photo_id AND r.id = NEW.participant_id
                  AND p.event_id = NEW.event_id AND r.event_id = NEW.event_id
              ) THEN RAISE EXCEPTION 'cross-event photo participant match' USING ERRCODE = '23514'; END IF;
            WHEN 'photo_face_detections' THEN
              IF NOT EXISTS (
                SELECT 1 FROM photos p JOIN photo_jobs j ON j.photo_id = p.id
                WHERE p.id = NEW.photo_id AND j.id = NEW.photo_job_id AND p.event_id = NEW.event_id
              ) THEN RAISE EXCEPTION 'cross-event photo face detection' USING ERRCODE = '23514'; END IF;
            WHEN 'face_participant_matches' THEN
              IF NOT EXISTS (
                SELECT 1 FROM photos p
                JOIN photo_face_detections d ON d.photo_id = p.id
                JOIN participants r ON r.event_id = p.event_id
                WHERE p.id = NEW.photo_id AND d.id = NEW.photo_face_detection_id
                  AND r.id = NEW.participant_id AND p.event_id = NEW.event_id
                  AND d.event_id = NEW.event_id
              ) THEN RAISE EXCEPTION 'cross-event face participant match' USING ERRCODE = '23514'; END IF;
            WHEN 'face_search_images' THEN
              IF NOT EXISTS (
                SELECT 1 FROM face_search_sessions s
                WHERE s.id = NEW.search_session_id AND s.event_id = NEW.event_id
              ) THEN RAISE EXCEPTION 'cross-event face search image' USING ERRCODE = '23514'; END IF;
            WHEN 'face_search_jobs' THEN
              IF NOT EXISTS (
                SELECT 1 FROM face_search_sessions s
                JOIN face_search_images i ON i.search_session_id = s.id
                WHERE s.id = NEW.search_session_id AND i.id = NEW.search_image_id
                  AND s.event_id = NEW.event_id AND i.event_id = NEW.event_id
              ) THEN RAISE EXCEPTION 'cross-event face search job' USING ERRCODE = '23514'; END IF;
            WHEN 'face_search_results' THEN
              IF NOT EXISTS (
                SELECT 1 FROM face_search_sessions s
                JOIN photos p ON p.event_id = s.event_id
                JOIN photo_face_detections d ON d.photo_id = p.id
                WHERE s.id = NEW.search_session_id AND p.id = NEW.photo_id
                  AND d.id = NEW.photo_face_detection_id AND s.event_id = NEW.event_id
                  AND d.event_id = NEW.event_id
              ) THEN RAISE EXCEPTION 'cross-event face search result' USING ERRCODE = '23514'; END IF;
            ELSE
              RAISE EXCEPTION 'unexpected scope trigger table: %', TG_TABLE_NAME;
          END CASE;
          RETURN NEW;
        END;
        $$
        """
    )
    trigger_columns = {
        "photo_participant_matches": "event_id, photo_id, participant_id",
        "photo_face_detections": "event_id, photo_id, photo_job_id",
        "face_participant_matches": "event_id, photo_id, photo_face_detection_id, participant_id",
        "face_search_images": "event_id, search_session_id",
        "face_search_jobs": "event_id, search_session_id, search_image_id",
        "face_search_results": "event_id, search_session_id, photo_id, photo_face_detection_id",
    }
    for table, columns in trigger_columns.items():
        op.execute(
            f"""
            CREATE CONSTRAINT TRIGGER trg_{table}_event_scope
            AFTER INSERT OR UPDATE OF {columns} ON {table}
            DEFERRABLE INITIALLY IMMEDIATE
            FOR EACH ROW EXECUTE FUNCTION raceframe_assert_event_scope()
            """  # nosec B608
        )


def downgrade() -> None:
    for table in (
        "photo_participant_matches",
        "photo_face_detections",
        "face_participant_matches",
        "face_search_images",
        "face_search_jobs",
        "face_search_results",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_event_scope ON {table}")  # nosec B608
    op.execute("DROP FUNCTION IF EXISTS raceframe_assert_event_scope()")

    # This named constraint exists only on legacy databases that were missing
    # the baseline FK. The 0001 constraint has a different generated name.
    op.execute(
        "ALTER TABLE face_search_sessions "
        "DROP CONSTRAINT IF EXISTS fk_face_search_sessions_participant_id"
    )

    op.drop_table("worker_heartbeats")
    op.drop_table("admin_audit_logs")
    op.drop_table("rate_limit_buckets")
    op.drop_table("object_deletion_tasks")

    op.drop_constraint("ck_face_search_sessions_status", "face_search_sessions", type_="check")
    op.drop_index("ix_face_search_sessions_owner_binding_hash", table_name="face_search_sessions")
    op.drop_index("ix_face_search_sessions_capability_hash", table_name="face_search_sessions")
    op.drop_index("ix_face_search_sessions_expires_at", table_name="face_search_sessions")
    op.drop_column("face_search_sessions", "owner_binding_hash")
    op.drop_column("face_search_sessions", "capability_hash")
    op.drop_column("face_search_sessions", "expires_at")

    op.drop_index("ix_face_search_results_session_score", table_name="face_search_results")
    op.drop_constraint("ck_face_search_result_similarity", "face_search_results", type_="check")
    op.drop_constraint("ck_face_search_images_status", "face_search_images", type_="check")
    op.drop_constraint("ck_face_search_images_file_size", "face_search_images", type_="check")
    op.drop_constraint("ck_face_participant_similarity", "face_participant_matches", type_="check")
    op.drop_index("ix_photo_face_detections_event_created", table_name="photo_face_detections")
    op.drop_constraint("ck_photo_face_quality_score", "photo_face_detections", type_="check")
    op.drop_constraint("ck_photo_face_detection_score", "photo_face_detections", type_="check")
    op.drop_constraint("ck_photo_face_detection_index", "photo_face_detections", type_="check")
    op.drop_constraint(
        "uq_participant_face_embedding_image",
        "participant_face_embeddings",
        type_="unique",
    )
    op.drop_constraint("ck_participant_face_images_status", "participant_face_images", type_="check")
    op.drop_constraint("ck_participant_face_images_file_size", "participant_face_images", type_="check")
    op.drop_index("ix_photo_matches_event_participant", table_name="photo_participant_matches")
    op.drop_constraint("ck_photo_match_confidence", "photo_participant_matches", type_="check")
    op.drop_constraint("ck_photo_participant_match_source", "photo_participant_matches", type_="check")
    op.drop_constraint("ck_photo_text_confidence", "photo_text_detection", type_="check")

    op.drop_index("ix_face_search_jobs_claim", table_name="face_search_jobs")
    op.drop_constraint("ck_face_search_jobs_max_attempts", "face_search_jobs", type_="check")
    op.drop_constraint("ck_face_search_jobs_attempt_count", "face_search_jobs", type_="check")
    op.drop_constraint("ck_face_search_jobs_status", "face_search_jobs", type_="check")
    op.drop_constraint("ck_face_search_jobs_job_type", "face_search_jobs", type_="check")

    op.drop_index("ix_participant_face_jobs_claim", table_name="participant_face_jobs")
    op.drop_constraint("ck_participant_face_jobs_max_attempts", "participant_face_jobs", type_="check")
    op.drop_constraint("ck_participant_face_jobs_attempt_count", "participant_face_jobs", type_="check")
    op.drop_constraint("ck_participant_face_jobs_status", "participant_face_jobs", type_="check")
    op.drop_constraint("ck_participant_face_jobs_job_type", "participant_face_jobs", type_="check")

    op.drop_index("ix_photo_jobs_claim", table_name="photo_jobs")
    op.drop_constraint("ck_photo_jobs_max_attempts", "photo_jobs", type_="check")
    op.drop_constraint("ck_photo_jobs_attempt_count", "photo_jobs", type_="check")
    op.drop_constraint("ck_photo_jobs_status", "photo_jobs", type_="check")
    op.drop_constraint("ck_photo_jobs_job_type", "photo_jobs", type_="check")

    for table in reversed(JOB_TABLES):
        _drop_job_lease_columns(table)

    op.drop_index("ix_photos_event_status_created", table_name="photos")
    op.drop_constraint("ck_photos_file_size_positive", "photos", type_="check")
    op.drop_constraint("ck_photos_checksum_sha256_length", "photos", type_="check")
    op.drop_constraint("ck_photos_status", "photos", type_="check")
    op.drop_constraint("uq_photos_event_checksum_sha256", "photos", type_="unique")
    op.drop_constraint("uq_photos_event_idempotency_key", "photos", type_="unique")
    op.drop_index("ix_photos_checksum_sha256", table_name="photos")
    op.drop_column("photos", "idempotency_key")
    op.drop_column("photos", "checksum_sha256")

    op.drop_index("ix_participants_event_name_lookup", table_name="participants")
    op.drop_constraint("uq_participants_event_bib_lookup", "participants", type_="unique")
    op.drop_column("participants", "name_lookup")
    op.drop_column("participants", "bib_lookup")

    op.drop_index("ix_events_status_date", table_name="events")
    op.drop_constraint("ck_events_status", "events", type_="check")
