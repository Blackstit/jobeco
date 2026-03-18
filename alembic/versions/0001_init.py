"""init

Revision ID: 0001_init
Revises: 
Create Date: 2026-03-17

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from pgvector.sqlalchemy import Vector


revision = "0001_init"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
  # pgvector extension
  op.execute("CREATE EXTENSION IF NOT EXISTS vector")

  op.create_table(
    "channels",
    sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
    sa.Column("tg_id", sa.BigInteger(), nullable=False),
    sa.Column("username", sa.String(length=255), nullable=True),
    sa.Column("title", sa.String(length=255), nullable=True),
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
  )
  op.create_index("ix_channels_tg_id", "channels", ["tg_id"], unique=True)
  op.create_index("ix_channels_username", "channels", ["username"], unique=False)

  op.create_table(
    "vacancies",
    sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
    sa.Column("tg_message_id", sa.BigInteger(), nullable=True),
    sa.Column("tg_channel_id", sa.BigInteger(), nullable=True),
    sa.Column("tg_channel_username", sa.String(length=255), nullable=True),
    sa.Column("source_url", sa.Text(), nullable=True),
    sa.Column("company_name", sa.String(length=255), nullable=True),
    sa.Column("title", sa.String(length=512), nullable=True),
    sa.Column("location_type", sa.String(length=64), nullable=True),
    sa.Column("salary_min_usd", sa.Integer(), nullable=True),
    sa.Column("salary_max_usd", sa.Integer(), nullable=True),
    sa.Column("stack", postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'"), nullable=False),
    sa.Column("category", sa.String(length=64), nullable=True),
    sa.Column("ai_score_value", sa.Integer(), nullable=True),
    sa.Column("summary_ru", sa.Text(), nullable=True),
    sa.Column("summary_en", sa.Text(), nullable=True),
    sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column("embedding", Vector(1536), nullable=True),
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
  )
  op.create_index("ix_vacancies_category", "vacancies", ["category"], unique=False)
  op.create_index("ix_vacancies_company_name", "vacancies", ["company_name"], unique=False)
  op.create_index("ix_vacancies_tg_channel_id", "vacancies", ["tg_channel_id"], unique=False)
  op.create_index("ix_vacancies_tg_message_id", "vacancies", ["tg_message_id"], unique=False)

  # cosine similarity index (ivfflat) can be added later after we have enough rows.


def downgrade() -> None:
  op.drop_index("ix_vacancies_tg_message_id", table_name="vacancies")
  op.drop_index("ix_vacancies_tg_channel_id", table_name="vacancies")
  op.drop_index("ix_vacancies_company_name", table_name="vacancies")
  op.drop_index("ix_vacancies_category", table_name="vacancies")
  op.drop_table("vacancies")

  op.drop_index("ix_channels_username", table_name="channels")
  op.drop_index("ix_channels_tg_id", table_name="channels")
  op.drop_table("channels")
