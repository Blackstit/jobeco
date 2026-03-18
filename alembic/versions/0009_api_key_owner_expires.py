"""api key owner + expiration

Revision ID: 0009_api_key_owner_expires
Revises: 0008_api_keys
Create Date: 2026-03-18
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0009_api_key_owner_expires"
down_revision = "0008_api_keys"
branch_labels = None
depends_on = None


def upgrade() -> None:
  op.add_column(
    "api_keys",
    sa.Column(
      "owner_id",
      sa.Integer(),
      sa.ForeignKey("admin_users.id", ondelete="SET NULL"),
      nullable=True,
    ),
  )
  op.add_column(
    "api_keys",
    sa.Column(
      "expires_at",
      sa.DateTime(timezone=True),
      nullable=True,
    ),
  )

  op.create_index("ix_api_keys_owner_id", "api_keys", ["owner_id"], unique=False)
  op.create_index("ix_api_keys_expires_at", "api_keys", ["expires_at"], unique=False)


def downgrade() -> None:
  op.drop_index("ix_api_keys_expires_at", table_name="api_keys")
  op.drop_index("ix_api_keys_owner_id", table_name="api_keys")
  op.drop_column("api_keys", "expires_at")
  op.drop_column("api_keys", "owner_id")

