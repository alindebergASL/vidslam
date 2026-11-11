"""Studio image operations: remove background and upscale (a job made
from a finished Studio picture or a Cast picture)

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-11 22:00:00

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0010'
down_revision: Union[str, Sequence[str], None] = '0009'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_FK = 'fk_asset_generation_jobs_source_asset_id'


def _columns(table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    """Upgrade schema."""
    have = _columns('asset_generation_jobs')
    with op.batch_alter_table('asset_generation_jobs') as batch:
        if 'operation' not in have:
            batch.add_column(sa.Column('operation', sa.String(length=30), nullable=False, server_default=''))
        if 'source_job_id' not in have:
            batch.add_column(sa.Column('source_job_id', sa.Integer(), nullable=True))
        if 'source_asset_id' not in have:
            batch.add_column(sa.Column('source_asset_id', sa.Integer(), nullable=True))
            batch.create_foreign_key(_FK, 'assets', ['source_asset_id'], ['id'], ondelete='SET NULL')
        if 'options_json' not in have:
            batch.add_column(sa.Column('options_json', sa.JSON(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('asset_generation_jobs') as batch:
        batch.drop_constraint(_FK, type_='foreignkey')
        batch.drop_column('options_json')
        batch.drop_column('source_asset_id')
        batch.drop_column('source_job_id')
        batch.drop_column('operation')
