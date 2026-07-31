"""Add an optional participant-facing event thumbnail.

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
    op.add_column("events", sa.Column("thumbnail_object_key", sa.String(length=512), nullable=True))


def downgrade() -> None:
    op.drop_column("events", "thumbnail_object_key")
