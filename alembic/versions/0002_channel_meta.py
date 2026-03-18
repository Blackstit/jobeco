from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0002_channel_meta"
down_revision = "0001_init"
branch_labels = None
depends_on = None


def upgrade() -> None:
  op.add_column("channels", sa.Column("bio", sa.Text(), nullable=True))
  op.add_column("channels", sa.Column("members_count", sa.Integer(), nullable=True))


def downgrade() -> None:
  op.drop_column("channels", "members_count")
  op.drop_column("channels", "bio")

