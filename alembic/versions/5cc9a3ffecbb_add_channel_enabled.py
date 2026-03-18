"""add_channel_enabled

Revision ID: 5cc9a3ffecbb
Revises: 0004_vacancy_raw_text
Create Date: 2026-03-17 21:50:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '5cc9a3ffecbb'
down_revision: Union[str, None] = '0004_vacancy_raw_text'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('channels', sa.Column('enabled', sa.Boolean(), nullable=False, server_default='true'))


def downgrade() -> None:
    op.drop_column('channels', 'enabled')
