"""Add csi_section column to chunks (spec section index)

Revision ID: c1f5a8e2b001
Revises: dc6326abf1e7
Create Date: 2026-05-06 22:00:00.000000

Per CSI section is the natural unit of construction estimating extraction.
Storing the section code on each spec chunk lets section_extractor pull
all chunks for "08 14 16" with a single indexed query — replacing the
fragile substring matching the old discipline_agent corpus assembly used.

Backfill is in scripts/backfill_chunk_csi_sections.py and runs separately.

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c1f5a8e2b001'
down_revision: Union[str, Sequence[str], None] = 'dc6326abf1e7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'chunks',
        sa.Column('csi_section', sa.String(length=16), nullable=True),
    )
    op.create_index(
        op.f('ix_chunks_csi_section'),
        'chunks',
        ['csi_section'],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_chunks_csi_section'), table_name='chunks')
    op.drop_column('chunks', 'csi_section')
