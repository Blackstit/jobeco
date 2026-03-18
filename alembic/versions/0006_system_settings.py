"""system settings

Revision ID: 0006_system_settings
Revises: 5cc9a3ffecbb
Create Date: 2026-03-18
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "0006_system_settings"
down_revision = "5cc9a3ffecbb"
branch_labels = None
depends_on = None


def upgrade() -> None:
  op.create_table(
    "system_settings",
    sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
    sa.Column("data", JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
  )

  # Keep at least one row for simpler reads.
  op.execute("INSERT INTO system_settings (data, created_at, updated_at) VALUES ('{}'::jsonb, now(), now());")


def downgrade() -> None:
  op.drop_table("system_settings")

