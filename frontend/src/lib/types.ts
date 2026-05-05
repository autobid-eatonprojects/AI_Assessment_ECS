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
