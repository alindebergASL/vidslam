"""storyboard editing: project voice, voiceover preview, shot takes, clip aspect

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-10 20:00:00

"""
from datetime import datetime
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0006'
down_revision: Union[str, Sequence[str], None] = '0005'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table: str, column: str) -> bool:
    return column in {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def _has_table(table: str) -> bool:
    return table in sa.inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    """Upgrade schema."""
    # NULL = the primary character's voice (today's behaviour).
    if not _has_column('video_projects', 'voice_id'):
        op.add_column('video_projects', sa.Column('voice_id', sa.String(length=120), nullable=True))
    if not _has_column('video_projects', 'voiceover_preview_json'):
        op.add_column('video_projects', sa.Column('voiceover_preview_json', sa.JSON(), nullable=True))
    new_aspect = not _has_column('video_shots', 'clip_aspect_ratio')
    if new_aspect:
        op.add_column('video_shots',
                      sa.Column('clip_aspect_ratio', sa.String(length=10), nullable=False, server_default=''))
    new_takes = not _has_table('shot_takes')
    if new_takes:
        op.create_table(
            'shot_takes',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('shot_id', sa.Integer(), nullable=False),
            sa.Column('clip_path', sa.String(length=500), nullable=False, server_default=''),
            sa.Column('clip_fingerprint', sa.String(length=64), nullable=False, server_default=''),
            sa.Column('aspect_ratio', sa.String(length=10), nullable=False, server_default=''),
            sa.Column('provider', sa.String(length=40), nullable=False, server_default=''),
            sa.Column('provider_model', sa.String(length=300), nullable=False, server_default=''),
            sa.Column('video_job_id', sa.Integer(), nullable=True),
            sa.Column('cost_usd', sa.Float(), nullable=True),
            sa.Column('note', sa.Text(), nullable=False, server_default=''),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(['shot_id'], ['video_shots.id'], ondelete='CASCADE'),
            sa.PrimaryKeyConstraint('id'),
        )
        op.create_index('ix_shot_takes_shot_id', 'shot_takes', ['shot_id'], unique=False)

    # Data: existing storyboard clips were made at their project's current
    # aspect (it could not be changed before), and each becomes take 1.
    bind = op.get_bind()
    rows = bind.execute(sa.text(
        "SELECT s.id, s.clip_path, s.clip_fingerprint, s.provider, s.provider_model, p.aspect_ratio "
        "FROM video_shots s JOIN video_projects p ON p.id = s.project_id "
        "WHERE s.clip_path <> '' AND s.status = 'completed' "
        "AND s.shot_type NOT IN ('end_card', 'ambient_segment', 'ambient_loop', 'ambient_variant') "
        "AND p.mode <> 'ambient_loop'"
    )).fetchall()
    now = datetime.utcnow()
    for sid, path, fp, provider, model, aspect in rows:
        if new_aspect:
            bind.execute(sa.text("UPDATE video_shots SET clip_aspect_ratio = :a WHERE id = :i"),
                         {"a": aspect or "", "i": sid})
        if new_takes:
            bind.execute(sa.text(
                "INSERT INTO shot_takes (shot_id, clip_path, clip_fingerprint, aspect_ratio, provider, "
                "provider_model, note, created_at) VALUES (:s, :p, :f, :a, :pr, :m, '', :c)"),
                {"s": sid, "p": path, "f": fp or "", "a": aspect or "", "pr": provider or "",
                 "m": model or "", "c": now})


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_shot_takes_shot_id', table_name='shot_takes')
    op.drop_table('shot_takes')
    with op.batch_alter_table('video_shots') as batch:
        batch.drop_column('clip_aspect_ratio')
    with op.batch_alter_table('video_projects') as batch:
        batch.drop_column('voiceover_preview_json')
        batch.drop_column('voice_id')
