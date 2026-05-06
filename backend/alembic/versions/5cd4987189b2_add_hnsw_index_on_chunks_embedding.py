"""add HNSW index on chunks.embedding

Revision ID: 5cd4987189b2
Revises: 683b94d07af4
Create Date: 2026-05-05 23:54:25.961917

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '5cd4987189b2'
down_revision: Union[str, Sequence[str], None] = '683b94d07af4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add HNSW index for fast pgvector ANN over chunks.embedding.

    HNSW (Hierarchical Navigable Small World) gives sub-millisecond
    nearest-neighbour over hundreds of thousands of vectors with high
    recall. Cosine distance (vector_cosine_ops) is what `<=>` uses.

    Tuning: m=16, ef_construction=64 are pgvector defaults — good
    balance of build time vs query recall. Bump ef_construction to 128
    if recall plateaus; bump m to 32 for more persistent storage cost.
    """
    op.execute(
        "CREATE INDEX IF NOT EXISTS chunks_embedding_hnsw_idx "
        "ON chunks USING hnsw (embedding vector_cosine_ops) "
        "WITH (m = 16, ef_construction = 64)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS chunks_embedding_hnsw_idx")
