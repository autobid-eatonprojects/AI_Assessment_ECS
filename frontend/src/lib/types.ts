export interface Project {
  id: string;
  name: string;
  description: string | null;
  created_at: string;
  updated_at: string;
  document_count: number;
}

export interface Document {
  id: string;
  project_id: string;
  filename: string;
  content_type: string;
  size_bytes: number;
  sha256: string;
  doc_type: string | null;
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
