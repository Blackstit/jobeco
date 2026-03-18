"""api keys for public vacancies access

Revision ID: 0008_api_keys
Revises: 0007_admin_users
Create Date: 2026-03-18
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "0008_api_keys"
down_revision = "0007_admin_users"
branch_labels = None
depends_on = None


def upgrade() -> None:
  op.create_table(
    "api_keys",
    sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
    sa.Column("name", sa.String(length=255), nullable=False),
    sa.Column("api_key_hash", sa.Text(), nullable=False),
    sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    sa.Column("config", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
    sa.Column("limits", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
  )
  op.create_index("ix_api_keys_api_key_hash", "api_keys", ["api_key_hash"], unique=True)
  op.create_index("ix_api_keys_is_active", "api_keys", ["is_active"])

  op.create_table(
    "api_key_usage",
    sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
    sa.Column(
      "api_key_id",
      sa.Integer(),
      sa.ForeignKey("api_keys.id", ondelete="CASCADE"),
      nullable=False,
    ),
    sa.Column("endpoint", sa.String(length=255), nullable=False),
    sa.Column("status_code", sa.Integer(), nullable=False),
    sa.Column("requested_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
  )
  op.create_index("ix_api_key_usage_api_key_id", "api_key_usage", ["api_key_id", "requested_at"])


def downgrade() -> None:
  op.drop_index("ix_api_key_usage_api_key_id", table_name="api_key_usage")
  op.drop_table("api_key_usage")

  op.drop_index("ix_api_keys_is_active", table_name="api_keys")
  op.drop_index("ix_api_keys_api_key_hash", table_name="api_keys")
  op.drop_table("api_keys")

