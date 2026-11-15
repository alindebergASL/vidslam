"""Sound Studio: audio stories (sound_stories), their generated clips and
takes (sound_clips), the sound library and shared sound-effect cache
(sound_assets), and finished mixes (sound_renders)

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-14 12:00:00

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0012'
down_revision: Union[str, Sequence[str], None] = '0011'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _indexes(table: str) -> set[str]:
    return {i["name"] for i in sa.inspect(op.get_bind()).get_indexes(table)}


def _index(name: str, table: str, cols: list[str]) -> None:
    if name not in _indexes(table):
        op.create_index(name, table, cols)


def upgrade() -> None:
    """Upgrade schema."""
    have = _tables()
    if 'sound_stories' not in have:
        op.create_table(
            'sound_stories',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('title', sa.String(length=200), nullable=False),
            sa.Column('kind', sa.String(length=20), nullable=False),
            sa.Column('premise', sa.Text(), nullable=False),
            sa.Column('target_minutes', sa.Integer(), nullable=False),
            sa.Column('settings_json', sa.JSON(), nullable=True),
            sa.Column('script_text', sa.Text(), nullable=False),
            sa.Column('script_version', sa.Integer(), nullable=False),
            sa.Column('cast_json', sa.JSON(), nullable=True),
            sa.Column('mix_json', sa.JSON(), nullable=True),
            sa.Column('status', sa.String(length=20), nullable=False),
            sa.Column('claim_token', sa.String(length=40), nullable=False),
            sa.Column('queue_job_id', sa.String(length=80), nullable=False),
            sa.Column('heartbeat_at', sa.DateTime(), nullable=True),
            sa.Column('progress_json', sa.JSON(), nullable=True),
            sa.Column('estimated_cost', sa.Float(), nullable=False),
            sa.Column('actual_cost', sa.Float(), nullable=False),
            sa.Column('error', sa.Text(), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id'),
        )
    _index('ix_sound_stories_status', 'sound_stories', ['status'])
    _index('ix_sound_stories_updated_at', 'sound_stories', ['updated_at'])

    if 'sound_clips' not in have:
        op.create_table(
            'sound_clips',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('story_id', sa.Integer(), nullable=False),
            sa.Column('cue_key', sa.String(length=64), nullable=False),
            sa.Column('chapter', sa.Integer(), nullable=False),
            sa.Column('seq', sa.Integer(), nullable=False),
            sa.Column('kind', sa.String(length=10), nullable=False),
            sa.Column('take_no', sa.Integer(), nullable=False),
            sa.Column('selected', sa.Boolean(), nullable=False),
            sa.Column('fingerprint', sa.String(length=64), nullable=False),
            sa.Column('text_or_prompt', sa.Text(), nullable=False),
            sa.Column('params_json', sa.JSON(), nullable=True),
            sa.Column('path', sa.String(length=500), nullable=False),
            sa.Column('duration_seconds', sa.Float(), nullable=True),
            sa.Column('provider', sa.String(length=40), nullable=False),
            sa.Column('model', sa.String(length=200), nullable=False),
            sa.Column('cost_usd', sa.Float(), nullable=False),
            sa.Column('attempts', sa.Integer(), nullable=False),
            sa.Column('status', sa.String(length=20), nullable=False),
            sa.Column('error', sa.Text(), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(['story_id'], ['sound_stories.id'], ondelete='CASCADE'),
            sa.PrimaryKeyConstraint('id'),
        )
    _index('ix_sound_clips_story_chapter_seq', 'sound_clips', ['story_id', 'chapter', 'seq'])
    _index('ix_sound_clips_fingerprint', 'sound_clips', ['fingerprint'])

    if 'sound_assets' not in have:
        op.create_table(
            'sound_assets',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('kind', sa.String(length=20), nullable=False),
            sa.Column('name', sa.String(length=200), nullable=False),
            sa.Column('tags', sa.String(length=500), nullable=False),
            sa.Column('prompt', sa.Text(), nullable=False),
            sa.Column('key_hash', sa.String(length=64), nullable=True),
            sa.Column('seconds', sa.Float(), nullable=False),
            sa.Column('loop', sa.Boolean(), nullable=False),
            sa.Column('provider', sa.String(length=40), nullable=False),
            sa.Column('model', sa.String(length=200), nullable=False),
            sa.Column('path', sa.String(length=500), nullable=False),
            sa.Column('source', sa.String(length=20), nullable=False),
            sa.Column('story_id', sa.Integer(), nullable=True),
            sa.Column('cost_usd', sa.Float(), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(['story_id'], ['sound_stories.id'], ondelete='SET NULL'),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('key_hash', name='uq_sound_assets_key_hash'),
        )
    _index('ix_sound_assets_story_id', 'sound_assets', ['story_id'])

    if 'sound_renders' not in have:
        op.create_table(
            'sound_renders',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('story_id', sa.Integer(), nullable=False),
            sa.Column('status', sa.String(length=20), nullable=False),
            sa.Column('script_version', sa.Integer(), nullable=False),
            sa.Column('files_json', sa.JSON(), nullable=True),
            sa.Column('chapters_json', sa.JSON(), nullable=True),
            sa.Column('loudness_json', sa.JSON(), nullable=True),
            sa.Column('duration_seconds', sa.Float(), nullable=True),
            sa.Column('error', sa.Text(), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(['story_id'], ['sound_stories.id'], ondelete='CASCADE'),
            sa.PrimaryKeyConstraint('id'),
        )
    _index('ix_sound_renders_story_id', 'sound_renders', ['story_id'])


def downgrade() -> None:
    """Downgrade schema."""
    have = _tables()
    for table in ('sound_renders', 'sound_assets', 'sound_clips', 'sound_stories'):
        if table in have:
            for name in sorted(_indexes(table)):
                if name and name.startswith('ix_'):
                    op.drop_index(name, table_name=table)
            op.drop_table(table)
