"""Hook lab (alternative openings of a finished video) and what each render
was composed from: renders.composition_json, renders.hook_variant_id and
the hook_variants table

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-11 23:00:00

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0011'
down_revision: Union[str, Sequence[str], None] = '0010'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_FK = 'fk_renders_hook_variant_id'


def _columns(table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    """Upgrade schema."""
    if 'hook_variants' not in _tables():
        op.create_table(
            'hook_variants',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('project_id', sa.Integer(), nullable=False),
            sa.Column('base_render_id', sa.Integer(), nullable=False),
            sa.Column('label', sa.String(length=10), nullable=False),
            sa.Column('style', sa.String(length=30), nullable=False),
            sa.Column('hook_text', sa.Text(), nullable=False),
            sa.Column('window_seconds', sa.Float(), nullable=False),
            sa.Column('shot1_source', sa.String(length=10), nullable=False),
            sa.Column('shot1_take_id', sa.Integer(), nullable=True),
            sa.Column('shot1_prompt', sa.Text(), nullable=False, server_default=''),
            sa.Column('shot1_clip_path', sa.String(length=500), nullable=False, server_default=''),
            sa.Column('audio_path', sa.String(length=500), nullable=False, server_default=''),
            sa.Column('audio_fingerprint', sa.String(length=64), nullable=False, server_default=''),
            sa.Column('clips_json', sa.JSON(), nullable=True),
            sa.Column('status', sa.String(length=20), nullable=False),
            sa.Column('error', sa.Text(), nullable=False),
            sa.Column('render_id', sa.Integer(), nullable=True),
            sa.Column('cost_estimate', sa.Float(), nullable=False),
            sa.Column('voicing_until', sa.DateTime(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(['project_id'], ['video_projects.id'], ondelete='CASCADE'),
            sa.ForeignKeyConstraint(['base_render_id'], ['renders.id'], ondelete='CASCADE'),
            sa.ForeignKeyConstraint(['render_id'], ['renders.id'], ondelete='SET NULL'),
            sa.PrimaryKeyConstraint('id'),
        )
        op.create_index('ix_hook_variants_project_id', 'hook_variants', ['project_id'])
        op.create_index('ix_hook_variants_base_render_id', 'hook_variants', ['base_render_id'])
    elif 'voicing_until' not in _columns('hook_variants'):
        with op.batch_alter_table('hook_variants') as batch:
            batch.add_column(sa.Column('voicing_until', sa.DateTime(), nullable=True))
    have = _columns('renders')
    with op.batch_alter_table('renders') as batch:
        if 'composition_json' not in have:
            batch.add_column(sa.Column('composition_json', sa.JSON(), nullable=True))
        if 'hook_variant_id' not in have:
            batch.add_column(sa.Column('hook_variant_id', sa.Integer(), nullable=True))
            batch.create_foreign_key(_FK, 'hook_variants', ['hook_variant_id'], ['id'], ondelete='SET NULL')
            batch.create_index('ix_renders_hook_variant_id', ['hook_variant_id'])


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('renders') as batch:
        batch.drop_index('ix_renders_hook_variant_id')
        batch.drop_constraint(_FK, type_='foreignkey')
        batch.drop_column('hook_variant_id')
        batch.drop_column('composition_json')
    op.drop_index('ix_hook_variants_base_render_id', table_name='hook_variants')
    op.drop_index('ix_hook_variants_project_id', table_name='hook_variants')
    op.drop_table('hook_variants')
