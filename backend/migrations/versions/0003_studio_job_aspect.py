"""studio job aspect ratio

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-07 12:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0003'
down_revision: Union[str, Sequence[str], None] = '0002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table: str, column: str) -> bool:
    return column in {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    """Upgrade schema."""
    # A SQLite dev database built by create_all before revisions were tracked
    # is stamped at the baseline and upgraded; it may already have the column.
    if _has_column('asset_generation_jobs', 'aspect_ratio'):
        return
    # server_default "" = the owner's default aspect for every existing job.
    op.add_column('asset_generation_jobs',
                  sa.Column('aspect_ratio', sa.String(length=10), nullable=False, server_default=''))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('asset_generation_jobs', 'aspect_ratio')
