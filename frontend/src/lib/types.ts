export interface Project {
  id: string;
  name: string;
  description: string | null;
  created_at: string;
  updated_at: string;
  document_count: number;
}

export type DocType =
  | "drawing-set"
  | "written-spec"
  | "bid-quote"
  | "scope-letter"
  | "license-insurance"
  | "safety-manual"
  | "contractor-info"
  | "other";

export type ProcessingStatus =
  | "pending"
  | "classifying"
  | "rendering"
  | "extracting"
  | "ready"
  | "failed"
  | "needs-api-key";

export interface Document {
  id: string;
  project_id: string;
  filename: string;
  content_type: string;
  size_bytes: number;
  sha256: string;
  doc_type: DocType | null;
  classification_confidence: number | null;
  classification_reasoning: string | null;
  page_count: number | null;
  processing_status: ProcessingStatus;
  processing_error: string | null;
  processed_at: string | null;
  created_at: string;
}

export interface DocumentPage {
  id: string;
  document_id: string;
  page_number: number;
  width: number;
  height: number;
  created_at: string;
}

export interface BoundingBox {
  x: number;
  y: number;
  width: number;
  height: number;
}

export type EntityType =
  | "material"
  | "manufacturer"
  | "code"
  | "dimension"
  | "room"
  | "equipment"
  | "symbol"
  | "other";

export type ExtractionStatus = "pending" | "extracting" | "ready" | "failed";

export interface ExtractedSchedule {
  id: string;
  name: string;
  columns: string[];
  rows: Record<string, unknown>[];
  bbox: BoundingBox | null;
}

export interface ExtractedNote {
  id: string;
  text: string;
  bbox: BoundingBox | null;
}

export interface ExtractedCrossReference {
  id: string;
  target_sheet: string;
  detail_id: string | null;
  context: string | null;
  bbox: BoundingBox | null;
}

export interface ExtractedEntity {
  id: string;
  entity_type: EntityType;
  value: string;
  bbox: BoundingBox | null;
  extra: Record<string, unknown> | null;
}

export interface PageExtraction {
  id: string;
  document_id: string;
  page_id: string;
  page_number: number;
  sheet_number: string | null;
  sheet_title: string | null;
  discipline: string | null;
  drawing_scale: string | null;
  status: ExtractionStatus;
  error: string | null;
  model: string | null;
  cost_usd: number | null;
  latency_ms: number | null;
  created_at: string;
  extracted_at: string | null;
  schedules: ExtractedSchedule[];
  notes: ExtractedNote[];
  cross_references: ExtractedCrossReference[];
  entities: ExtractedEntity[];
}

export interface PageExtractionSummary {
  page_number: number;
  status: ExtractionStatus;
  sheet_number: string | null;
  sheet_title: string | null;
  discipline: string | null;
  schedule_count: number;
  note_count: number;
  cross_reference_count: number;
  entity_count: number;
  cost_usd: number | null;
}

export interface DocumentExtractionOverview {
  document_id: string;
  total_pages: number;
  pages_ready: number;
  pages_failed: number;
  pages_pending: number;
  pages_extracting: number;
  total_cost_usd: number;
  pages: PageExtractionSummary[];
}

export interface SearchHit {
  chunk_id: string;
  document_id: string;
  document_filename: string;
  page_id: string | null;
  page_number: number | null;
  chunk_type: string;
  text: string;
  snippet: string | null;
  bbox: BoundingBox | null;
  sheet_number: string | null;
  sheet_title: string | null;
  discipline: string | null;
  dense_score: number;
  sparse_score: number;
  rrf_score: number;
  rerank_score: number | null;
}

export interface SearchResponse {
  query: string;
  hits: SearchHit[];
  candidates_considered: number;
  rerank_used: "cohere" | "claude" | "none";
}

export interface TokenResponse {
  access_token: string;
  token_type: string;
  expires_in: number;
}

export interface User {
  email: string;
}

export interface ApiError {
  detail: string | { msg: string }[];
}
