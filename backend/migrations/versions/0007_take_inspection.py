"""clip check: the inspector's verdict on each shot take

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-11 12:00:00

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0007'
down_revision: Union[str, Sequence[str], None] = '0006'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table: str, column: str) -> bool:
    return column in {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    """Upgrade schema."""
    # NULL = not checked (every take made before the clip check existed).
    if not _has_column('shot_takes', 'inspection_json'):
        op.add_column('shot_takes', sa.Column('inspection_json', sa.JSON(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('shot_takes') as batch:
        batch.drop_column('inspection_json')
