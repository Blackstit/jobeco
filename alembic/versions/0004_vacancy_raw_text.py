from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0004_vacancy_raw_text"
down_revision = "0003_channel_tg_id_nullable"
branch_labels = None
depends_on = None


def upgrade() -> None:
  op.add_column("vacancies", sa.Column("raw_text", sa.Text(), nullable=True))


def downgrade() -> None:
  op.drop_column("vacancies", "raw_text")

