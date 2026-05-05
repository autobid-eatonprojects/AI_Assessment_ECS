export type LifecycleState = "setup" | "open-for-bids" | "complete";
export type DocumentSource = "project_document" | "bid_submission";

export interface Project {
  id: string;
  name: string;
  description: string | null;
  lifecycle_state: LifecycleState;
  created_at: string;
  updated_at: string;
  document_count: number;
  project_document_count: number;
  bid_submission_count: number;
}

export type DocType =
  | "drawing-set"
  | "written-spec"
  | "trade-list"
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
  | "ocr"
  | "indexing"
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
  source: DocumentSource;
  vendor_name: string | null;
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

export interface DocumentPageText {
  page_number: number;
  width: number;
  height: number;
  text: string | null;
  text_source: "pymupdf" | "ocr-gemini" | "ocr-anthropic" | null;
  char_count: number;
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

export interface TradeDivisionRelevance {
  id: string;
  project_id: string;
  csi_division: string;
  division_label: string;
  is_relevant: boolean;
  reasoning: string | null;
  confidence: number | null;
  operator_override: boolean;
  override_value: boolean | null;
  cost_usd: number | null;
  latency_ms: number | null;
  created_at: string;
}

export interface TradeRelevanceMatrix {
  project_id: string;
  relevant_count: number;
  skipped_count: number;
  total_cost_usd: number;
  divisions: TradeDivisionRelevance[];
}

export interface ScopeCitation {
  id: string;
  chunk_id: string;
  document_id: string | null;
  page_number: number | null;
  sheet_number: string | null;
  bbox: BoundingBox | null;
  rerank_score: number | null;
  extraction_query: string | null;
  excerpt: string | null;
}

export type QtyConfidence = "high" | "medium" | "conflicting" | "unverified";
export type VerifierStatus = "keep" | "revised" | "rejected";

export interface VerifierReview {
  verdict: VerifierStatus;
  reasoning: string | null;
  consistency_check: {
    schedule_says?: string | null;
    note_says?: string | null;
    spec_says?: string | null;
    agree?: boolean | null;
  } | null;
  model: string | null;
  latency_ms?: number | null;
  cost_usd?: number | null;
}

export interface ScopeItem {
  id: string;
  project_id: string;
  run_id: string;
  csi_code: string;
  csi_division: string;
  division_label: string;
  section_title: string | null;
  description: string;
  specification: string | null;
  quantity: string | null;
  unit: string | null;
  location: string | null;
  confidence: number;
  extraction_method: string | null;
  qty_confidence: QtyConfidence | null;
  qty_provenance: Record<string, unknown> | null;
  verifier_status: VerifierStatus | null;
  verifier_review: VerifierReview | null;
  citations: ScopeCitation[];
  created_at: string;
  updated_at: string;
}

export interface ScopeRun {
  id: string;
  project_id: string;
  status: "running" | "complete" | "failed" | "cancelled";
  started_at: string;
  completed_at: string | null;
  error: string | null;
  sections_total: number;
  sections_completed: number;
  sections_failed: number;
  candidates_generated: number;
  items_validated: number;
  items_after_dedupe: number;
  total_cost_usd: number;
  total_latency_ms: number;
  config: Record<string, unknown> | null;
}

export interface ScopeOverview {
  project_id: string;
  latest_run: ScopeRun | null;
  total_items: number;
  by_division: { csi_division: string; division_label: string; count: number }[];
}

export interface ProjectProfile {
  id: string;
  project_id: string;
  building_type: string | null;
  size_sf: number | null;
  occupancy: string | null;
  construction_type: string | null;
  sprinklered: boolean | null;
  stories: number | null;
  location: string | null;
  project_number: string | null;
  codes: string[] | null;
  reasoning: string | null;
  model: string | null;
  cost_usd: number | null;
  latency_ms: number | null;
  created_at: string;
  updated_at: string;
}

// Phase 11 — vendor profile + bid leveling
export interface VendorQualifications {
  has_license_or_insurance: boolean;
  has_safety_manual: boolean;
  has_contractor_info: boolean;
  is_complete: boolean;
  license_or_insurance_count: number;
  safety_manual_count: number;
  contractor_info_count: number;
}

export interface VendorCoverageStats {
  covered: number;
  partial: number;
  excluded: number;
  not_covered: number;
}

export interface VendorSummary {
  canonical_vendor: string;
  primary_csi_divisions: string[];
  bid_total_usd: number | null;
  line_item_count: number;
  inclusion_count: number;
  exclusion_count: number;
  document_count: number;
  has_priced_bid: boolean;
  qualifications: VendorQualifications;
  coverage: VendorCoverageStats | null;
  aliases: string[];
}

export interface VendorDocSummary {
  id: string;
  filename: string;
  doc_type: string | null;
  classification_confidence: number | null;
  processing_status: string;
  page_count: number | null;
}

export interface VendorProfile {
  canonical_vendor: string;
  primary_csi_divisions: string[];
  bid_total_usd: number | null;
  line_item_count: number;
  inclusion_count: number;
  exclusion_count: number;
  qualifications: VendorQualifications;
  coverage: VendorCoverageStats | null;
  aliases: string[];
  documents: VendorDocSummary[];
  line_items: Array<{
    id: string;
    description: string;
    quantity: string | null;
    unit: string | null;
    unit_price_usd: number | null;
    total_price_usd: number | null;
    csi_section_guess: string | null;
    page_number: number | null;
  }>;
  inclusions: Array<{ id: string; text: string; page_number: number | null }>;
  exclusions: Array<{ id: string; text: string; page_number: number | null }>;
  covered_scope_item_ids: string[];
  partial_scope_item_ids: string[];
  excluded_scope_item_ids: string[];
  not_covered_scope_item_ids: string[];
}

export interface BidLevelingCell {
  vendor: string;
  status: BidCoverageStatus;
  matched_line_description: string | null;
  matched_line_total_usd: number | null;
  confidence: number;
  reasoning: string | null;
}

export interface BidLevelingRow {
  scope_item_id: string;
  csi_code: string;
  description: string;
  quantity: string | null;
  unit: string | null;
  cells: BidLevelingCell[];
}

export interface BidLevelingResponse {
  csi_division: string | null;
  vendors: string[];
  rows: BidLevelingRow[];
}

// Phase 8 — bid analysis
export interface BidLineItem {
  id: string;
  bid_document_id: string;
  description: string;
  quantity: string | null;
  unit: string | null;
  unit_price_usd: number | null;
  total_price_usd: number | null;
  csi_section_guess: string | null;
  page_number: number | null;
}

export interface BidExclusion {
  id: string;
  bid_document_id: string;
  text: string;
  page_number: number | null;
}

export interface BidInclusion {
  id: string;
  bid_document_id: string;
  text: string;
  page_number: number | null;
}

export interface BidSummary {
  id: string;
  project_id: string;
  run_id: string;
  bid_document_id: string;
  vendor_name: string | null;
  bid_total_usd: number | null;
  primary_csi_divisions: string[] | null;
  line_item_count: number;
  inclusion_count: number;
  exclusion_count: number;
  extraction_cost_usd: number | null;
  extraction_latency_ms: number | null;
}

export type BidCoverageStatus =
  | "covered"
  | "partial"
  | "excluded"
  | "not_covered"
  | "not_applicable";

export interface BidCoverage {
  id: string;
  scope_item_id: string;
  bid_document_id: string;
  status: BidCoverageStatus;
  confidence: number;
  reasoning: string | null;
  matched_line_item_id: string | null;
  judge_model: string | null;
}

export interface BidRun {
  id: string;
  project_id: string;
  scope_run_id: string | null;
  status: "running" | "complete" | "failed";
  started_at: string;
  completed_at: string | null;
  error: string | null;
  bids_total: number;
  bids_extracted: number;
  coverage_pairs_total: number;
  coverage_pairs_completed: number;
  total_cost_usd: number;
  total_latency_ms: number;
  config: Record<string, unknown> | null;
}

export interface BidAnalysisOverview {
  project_id: string;
  latest_run: BidRun | null;
  bid_summaries: BidSummary[];
  coverage_counts: {
    covered: number;
    partial: number;
    excluded: number;
    not_covered: number;
  };
}

export interface BidDetail {
  summary: BidSummary;
  line_items: BidLineItem[];
  inclusions: BidInclusion[];
  exclusions: BidExclusion[];
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
