"""app_settings key/value table

Revision ID: b41f2c9d7e10
Revises: 9c3d1e7a5b20
Create Date: 2026-11-04 14:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b41f2c9d7e10'
down_revision: Union[str, Sequence[str], None] = '9c3d1e7a5b20'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'app_settings',
        sa.Column('key', sa.String(length=120), nullable=False),
        sa.Column('value', sa.JSON(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('key'),
    )


def downgrade() -> None:
    op.drop_table('app_settings')
