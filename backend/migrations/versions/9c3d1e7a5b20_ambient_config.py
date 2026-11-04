"""project ambient_config_json

Revision ID: 9c3d1e7a5b20
Revises: 642681883a9d
Create Date: 2026-11-04 10:12:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9c3d1e7a5b20'
down_revision: Union[str, Sequence[str], None] = '642681883a9d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Nullable JSON: existing storyboard projects simply carry NULL here.
    with op.batch_alter_table('video_projects', schema=None) as batch_op:
        batch_op.add_column(sa.Column('ambient_config_json', sa.JSON(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('video_projects', schema=None) as batch_op:
        batch_op.drop_column('ambient_config_json')
