"""Replace the room name by the full meeting link of each booking

Revision ID: 7b2e5d9c4a10
Revises: c9331cd54be6
Create Date: 2026-10-19 23:10:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '7b2e5d9c4a10'
down_revision = 'c9331cd54be6'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('booking', schema=None) as batch_op:
        batch_op.add_column(sa.Column('meeting_url', sa.String(length=2048), nullable=True))

    # Existing bookings: the link they were given before, i.e. the former default base URL + room
    op.execute("UPDATE booking SET meeting_url = 'https://meet.jit.si/' || room")

    with op.batch_alter_table('booking', schema=None) as batch_op:
        batch_op.alter_column('meeting_url', existing_type=sa.String(length=2048), nullable=False)
        batch_op.drop_column('room')


def downgrade():
    with op.batch_alter_table('booking', schema=None) as batch_op:
        batch_op.add_column(sa.Column('room', sa.String(length=100), nullable=True))

    # The room name is what follows the former default base URL; other links are kept as they are
    op.execute(
        "UPDATE booking SET room = CASE WHEN meeting_url LIKE 'https://meet.jit.si/%' "
        "THEN substr(meeting_url, 21) ELSE substr(meeting_url, -100) END"
    )

    with op.batch_alter_table('booking', schema=None) as batch_op:
        batch_op.alter_column('room', existing_type=sa.String(length=100), nullable=False)
        batch_op.drop_column('meeting_url')
