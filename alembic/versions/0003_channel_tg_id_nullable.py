from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0003_channel_tg_id_nullable"
down_revision = "0002_channel_meta"
branch_labels = None
depends_on = None


def upgrade() -> None:
  # сделать tg_id nullable и индекс неуникальным
  op.drop_index("ix_channels_tg_id", table_name="channels")
  op.alter_column("channels", "tg_id", existing_type=sa.BigInteger(), nullable=True)
  op.create_index("ix_channels_tg_id", "channels", ["tg_id"], unique=False)


def downgrade() -> None:
  op.drop_index("ix_channels_tg_id", table_name="channels")
  op.alter_column("channels", "tg_id", existing_type=sa.BigInteger(), nullable=False)
  op.create_index("ix_channels_tg_id", "channels", ["tg_id"], unique=True)

