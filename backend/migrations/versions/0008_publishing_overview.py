"""publishing overview + results: YouTube's own state on each publish job,
and daily YouTube statistics per uploaded video

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-11 18:00:00

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0008'
down_revision: Union[str, Sequence[str], None] = '0007'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_JOB_COLUMNS = [
    ('uploaded_at', lambda: sa.Column('uploaded_at', sa.DateTime(), nullable=True)),
    ('yt_privacy_status', lambda: sa.Column('yt_privacy_status', sa.String(length=20), nullable=False,
                                            server_default='')),
    ('yt_upload_status', lambda: sa.Column('yt_upload_status', sa.String(length=20), nullable=False,
                                           server_default='')),
    ('yt_publish_at', lambda: sa.Column('yt_publish_at', sa.DateTime(), nullable=True)),
    ('yt_failure_reason', lambda: sa.Column('yt_failure_reason', sa.String(length=100), nullable=False,
                                            server_default='')),
    ('yt_title', lambda: sa.Column('yt_title', sa.String(length=200), nullable=False, server_default='')),
    ('yt_removed', lambda: sa.Column('yt_removed', sa.Boolean(), nullable=False, server_default=sa.false())),
    ('yt_checked_at', lambda: sa.Column('yt_checked_at', sa.DateTime(), nullable=True)),
    ('channel_id', lambda: sa.Column('channel_id', sa.String(length=64), nullable=False, server_default='')),
]


def _columns(table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    """Upgrade schema."""
    have = _columns('publish_jobs')
    for name, col in _JOB_COLUMNS:
        if name not in have:
            op.add_column('publish_jobs', col())
    # Videos uploaded before uploaded_at existed: fix their upload time once,
    # from the last write before this migration, so later writes (YouTube
    # checks bump updated_at) can't move their age.
    op.execute(sa.text("UPDATE publish_jobs SET uploaded_at = COALESCE(updated_at, created_at) "
                       "WHERE uploaded_at IS NULL AND external_id IS NOT NULL AND external_id != ''"))
    if 'video_stats' not in _tables():
        op.create_table(
            'video_stats',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('publish_job_id', sa.Integer(), nullable=False),
            sa.Column('fetched_at', sa.DateTime(), nullable=False),
            # The UTC day: one row per video per day, even with two fetches at once.
            sa.Column('day', sa.Date(), nullable=False),
            sa.Column('views', sa.BigInteger(), nullable=False),
            sa.Column('likes', sa.Integer(), nullable=False),
            sa.Column('comments', sa.Integer(), nullable=False),
            sa.ForeignKeyConstraint(['publish_job_id'], ['publish_jobs.id'], ondelete='CASCADE'),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('publish_job_id', 'day', name='uq_video_stats_job_day'),
        )
        op.create_index('ix_video_stats_job_fetched', 'video_stats', ['publish_job_id', 'fetched_at'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_video_stats_job_fetched', table_name='video_stats')
    op.drop_table('video_stats')
    with op.batch_alter_table('publish_jobs') as batch:
        for name, _ in reversed(_JOB_COLUMNS):
            batch.drop_column(name)
