"""parser logs for admin UI

Revision ID: 0010_parser_logs
Revises: 0009_api_key_owner_expires
Create Date: 2026-03-18
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


# revision identifiers, used by Alembic.
revision = "0010_parser_logs"
down_revision = "0009_api_key_owner_expires"
branch_labels = None
depends_on = None


def upgrade() -> None:
  op.create_table(
    "parser_logs",
    sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    sa.Column("level", sa.String(length=16), nullable=False, index=True),
    sa.Column("event", sa.String(length=64), nullable=False, index=True),
    sa.Column("message_en", sa.Text(), nullable=False),
    sa.Column("channel_username", sa.String(length=255), nullable=True, index=True),
    sa.Column("tg_message_id", sa.BigInteger(), nullable=True, index=True),
    sa.Column("vacancy_id", sa.Integer(), nullable=True, index=True),
    sa.Column("extra", JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
  )

  op.create_index("ix_parser_logs_created_at", "parser_logs", ["created_at"], unique=False)


def downgrade() -> None:
  op.drop_index("ix_parser_logs_created_at", table_name="parser_logs")
  op.drop_table("parser_logs")

