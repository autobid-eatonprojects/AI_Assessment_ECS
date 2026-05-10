"""scope_extraction_runs reproducibility (model_versions + input_pdf_hash)

Revision ID: 0bce48a16238
Revises: 5cd4987189b2
Create Date: 2026-05-06 00:09:34.606082

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0bce48a16238'
down_revision: Union[str, Sequence[str], None] = '5cd4987189b2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    NOTE: autogenerate also flagged the HNSW index for removal because
    SQLAlchemy can't express it in metadata reflection. We KEEP the index
    — it's defined by hand-rolled SQL in the prior migration. The
    drop_index line is intentionally removed below.
    """
    op.add_column('scope_extraction_runs', sa.Column('model_versions', sa.JSON(), nullable=True))
    op.add_column('scope_extraction_runs', sa.Column('input_pdf_hash', sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column('scope_extraction_runs', 'input_pdf_hash')
    op.drop_column('scope_extraction_runs', 'model_versions')
