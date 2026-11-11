"""live progress events, Studio first frames + real clip size/length, and the
prompt safety-check cache

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-11 20:00:00

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0009'
down_revision: Union[str, Sequence[str], None] = '0008'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_JOB_COLUMNS = [
    ('first_frame_job_id', lambda: sa.Column('first_frame_job_id', sa.Integer(), nullable=True)),
    ('first_frame_asset_id', lambda: sa.Column('first_frame_asset_id', sa.Integer(), nullable=True)),
    ('result_width', lambda: sa.Column('result_width', sa.Integer(), nullable=True)),
    ('result_height', lambda: sa.Column('result_height', sa.Integer(), nullable=True)),
    ('result_duration_seconds', lambda: sa.Column('result_duration_seconds', sa.Float(), nullable=True)),
    ('queue_job_id', lambda: sa.Column('queue_job_id', sa.String(length=80), nullable=False, server_default='')),
]


def _columns(table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    """Upgrade schema."""
    have = _columns('asset_generation_jobs')
    for name, col in _JOB_COLUMNS:
        if name not in have:
            op.add_column('asset_generation_jobs', col())
    if 'queue_job_id' not in _columns('video_projects'):
        op.add_column('video_projects', sa.Column('queue_job_id', sa.String(length=80), nullable=False,
                                                  server_default=''))
    if 'claim_token' not in _columns('video_projects'):
        op.add_column('video_projects', sa.Column('claim_token', sa.String(length=40), nullable=False,
                                                  server_default=''))
    tables = _tables()
    if 'events' not in tables:
        op.create_table(
            'events',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('kind', sa.String(length=40), nullable=False),
            sa.Column('entity_type', sa.String(length=40), nullable=False),
            sa.Column('entity_id', sa.Integer(), nullable=False),
            sa.Column('project_id', sa.Integer(), nullable=True),
            sa.Column('status', sa.String(length=20), nullable=False),
            sa.Column('title', sa.String(length=200), nullable=False),
            sa.Column('detail', sa.Text(), nullable=False),
            sa.Column('url', sa.String(length=300), nullable=False),
            sa.Column('severity', sa.String(length=10), nullable=False),
            sa.Column('read_at', sa.DateTime(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id'),
        )
        op.create_index('ix_events_project_id', 'events', ['project_id'])
        op.create_index('ix_events_created_at', 'events', ['created_at'])
    if 'prompt_checks' not in tables:
        op.create_table(
            'prompt_checks',
            sa.Column('text_hash', sa.String(length=64), nullable=False),
            sa.Column('verdict', sa.String(length=16), nullable=False),
            sa.Column('model', sa.String(length=120), nullable=False),
            sa.Column('reason', sa.Text(), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('text_hash'),
        )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('prompt_checks')
    op.drop_index('ix_events_created_at', table_name='events')
    op.drop_index('ix_events_project_id', table_name='events')
    op.drop_table('events')
    with op.batch_alter_table('video_projects') as batch:
        batch.drop_column('claim_token')
        batch.drop_column('queue_job_id')
    with op.batch_alter_table('asset_generation_jobs') as batch:
        for name, _ in reversed(_JOB_COLUMNS):
            batch.drop_column(name)
