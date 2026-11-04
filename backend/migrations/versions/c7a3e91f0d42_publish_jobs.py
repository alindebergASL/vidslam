"""publish_jobs

Revision ID: c7a3e91f0d42
Revises: b41f2c9d7e10
Create Date: 2026-11-04 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c7a3e91f0d42'
down_revision: Union[str, Sequence[str], None] = 'b41f2c9d7e10'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'publish_jobs',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('render_id', sa.Integer(), nullable=False),
        sa.Column('platform', sa.String(length=20), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('title', sa.String(length=200), nullable=False),
        sa.Column('description', sa.Text(), nullable=False),
        sa.Column('tags_json', sa.JSON(), nullable=True),
        sa.Column('category_id', sa.String(length=10), nullable=False),
        sa.Column('privacy', sa.String(length=20), nullable=False),
        sa.Column('publish_at', sa.DateTime(), nullable=True),
        sa.Column('made_for_kids', sa.Boolean(), nullable=False),
        sa.Column('contains_synthetic_media', sa.Boolean(), nullable=False),
        sa.Column('set_thumbnail', sa.Boolean(), nullable=False),
        sa.Column('upload_url', sa.String(length=1000), nullable=False),
        sa.Column('bytes_total', sa.Integer(), nullable=False),
        sa.Column('bytes_sent', sa.Integer(), nullable=False),
        sa.Column('external_id', sa.String(length=100), nullable=False),
        sa.Column('external_url', sa.String(length=300), nullable=False),
        sa.Column('error', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['render_id'], ['renders.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_publish_jobs_render_id', 'publish_jobs', ['render_id'])


def downgrade() -> None:
    op.drop_index('ix_publish_jobs_render_id', table_name='publish_jobs')
    op.drop_table('publish_jobs')
