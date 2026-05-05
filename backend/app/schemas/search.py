from pydantic import BaseModel, Field


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    top_k: int = Field(default=10, ge=1, le=50)


class SearchHit(BaseModel):
    chunk_id: str
    document_id: str
    document_filename: str
    page_id: str | None
    page_number: int | None

    chunk_type: str
    text: str
    snippet: str | None
    bbox: dict | None

    sheet_number: str | None = None
    sheet_title: str | None = None
    discipline: str | None = None

    dense_score: float
    sparse_score: float
    rrf_score: float
    rerank_score: float | None


class SearchResponse(BaseModel):
    query: str
    hits: list[SearchHit]
    candidates_considered: int
    rerank_used: str  # "cohere" | "claude" | "none"
