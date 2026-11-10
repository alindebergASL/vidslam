"""fingerprints: what each shot clip and each voiceover was made from

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-10 18:00:00

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0005'
down_revision: Union[str, Sequence[str], None] = '0004'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table: str, column: str) -> bool:
    return column in {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    """Upgrade schema."""
    # "" = made before fingerprints existed; such clips count as current.
    if not _has_column('video_shots', 'clip_fingerprint'):
        op.add_column('video_shots',
                      sa.Column('clip_fingerprint', sa.String(length=64), nullable=False, server_default=''))
    if not _has_column('renders', 'voice_fingerprint'):
        op.add_column('renders',
                      sa.Column('voice_fingerprint', sa.String(length=64), nullable=False, server_default=''))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('renders', 'voice_fingerprint')
    op.drop_column('video_shots', 'clip_fingerprint')
