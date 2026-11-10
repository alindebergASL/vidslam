"""studio library: favorite + label on studio jobs

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-10 12:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0004'
down_revision: Union[str, Sequence[str], None] = '0003'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table: str, column: str) -> bool:
    return column in {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    """Upgrade schema."""
    # A SQLite dev database built by create_all before revisions were tracked
    # is stamped at the baseline and upgraded; it may already have the columns.
    if not _has_column('asset_generation_jobs', 'favorite'):
        op.add_column('asset_generation_jobs',
                      sa.Column('favorite', sa.Boolean(), nullable=False, server_default=sa.false()))
    if not _has_column('asset_generation_jobs', 'label'):
        # "" = no tag; character-sheet jobs carry their view ("Front view").
        op.add_column('asset_generation_jobs',
                      sa.Column('label', sa.String(length=80), nullable=False, server_default=''))
    # The Studio list sums each job's ledger rows by ref.
    if 'ix_spend_entries_ref' not in {i["name"] for i in sa.inspect(op.get_bind()).get_indexes('spend_entries')}:
        op.create_index('ix_spend_entries_ref', 'spend_entries', ['ref'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_spend_entries_ref', table_name='spend_entries')
    op.drop_column('asset_generation_jobs', 'label')
    op.drop_column('asset_generation_jobs', 'favorite')
