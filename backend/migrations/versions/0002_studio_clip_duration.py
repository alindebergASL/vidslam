"""studio clip duration

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-05 04:56:38.068581

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0002'
down_revision: Union[str, Sequence[str], None] = '0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # server_default fills existing rows (live deploys already have Studio jobs).
    op.add_column('asset_generation_jobs',
                  sa.Column('duration_seconds', sa.Float(), nullable=False, server_default='5'))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('asset_generation_jobs', 'duration_seconds')
