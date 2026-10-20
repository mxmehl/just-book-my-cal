"""Add the profile a booking was made under

Revision ID: a41f0c3e8d52
Revises: 7b2e5d9c4a10
Create Date: 2026-10-20 20:30:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a41f0c3e8d52'
down_revision = '7b2e5d9c4a10'
branch_labels = None
depends_on = None


def upgrade():
    # Existing bookings stay NULL: they use the top-level settings of the config
    with op.batch_alter_table('booking', schema=None) as batch_op:
        batch_op.add_column(sa.Column('profile', sa.String(length=64), nullable=True))


def downgrade():
    with op.batch_alter_table('booking', schema=None) as batch_op:
        batch_op.drop_column('profile')
