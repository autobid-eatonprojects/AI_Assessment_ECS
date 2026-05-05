import { useAuthStore } from "./auth";
import type {
  BidAnalysisOverview,
  BidCoverage,
  BidDetail,
  BidLevelingResponse,
  BidRun,
  Document,
  VendorProfile,
  VendorSummary,
  DocumentExtractionOverview,
  DocumentPage,
  DocumentPageText,
  PageExtraction,
  Project,
  ProjectProfile,
  ScopeItem,
  ScopeOverview,
  ScopeRun,
  SearchResponse,
  TokenResponse,
  TradeRelevanceMatrix,
  User,
} from "./types";

const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

async function request<T>(
  path: string,
  init: RequestInit = {},
  { auth = true }: { auth?: boolean } = {},
): Promise<T> {
  const headers = new Headers(init.headers);
  if (auth) {
    const token = useAuthStore.getState().token;
    if (token) headers.set("Authorization", `Bearer ${token}`);
  }
  if (init.body && !(init.body instanceof FormData) && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }

  const res = await fetch(`${API_URL}${path}`, { ...init, headers });

  if (res.status === 401 && auth) {
    useAuthStore.getState().clearAuth();
  }

  if (!res.ok) {
    let message = res.statusText;
    try {
      const body = await res.json();
      if (typeof body.detail === "string") message = body.detail;
      else if (Array.isArray(body.detail)) message = body.detail.map((d: { msg: string }) => d.msg).join(", ");
    } catch {
      /* ignore */
    }
    throw new ApiError(message, res.status);
  }

  if (res.status === 204) return undefined as T;
  return res.json() as Promise<T>;
}

export const api = {
  login: (email: string, password: string) =>
    request<TokenResponse>(
      "/api/auth/login",
      { method: "POST", body: JSON.stringify({ email, password }) },
      { auth: false },
    ),
  me: () => request<User>("/api/auth/me"),

  listProjects: () => request<Project[]>("/api/projects"),
  createProject: (data: { name: string; description?: string }) =>
    request<Project>("/api/projects", { method: "POST", body: JSON.stringify(data) }),
  getProject: (id: string) => request<Project>(`/api/projects/${id}`),
  deleteProject: (id: string) =>
    request<void>(`/api/projects/${id}`, { method: "DELETE" }),
  transitionLifecycle: (
    id: string,
    new_state: "setup" | "open-for-bids" | "complete",
  ) =>
    request<Project>(`/api/projects/${id}/lifecycle`, {
      method: "POST",
      body: JSON.stringify({ new_state }),
    }),

  listDocuments: (projectId: string) =>
    request<Document[]>(`/api/projects/${projectId}/documents`),
  uploadDocuments: (
    projectId: string,
    files: File[],
    options?: {
      source?: "project_document" | "bid_submission";
      vendor_name?: string;
    },
  ) => {
    const fd = new FormData();
    files.forEach((f) => fd.append("files", f));
    if (options?.source) fd.append("source", options.source);
    if (options?.vendor_name) fd.append("vendor_name", options.vendor_name);
    return request<Document[]>(`/api/projects/${projectId}/documents`, {
      method: "POST",
      body: fd,
    });
  },
  deleteDocument: (projectId: string, documentId: string) =>
    request<void>(`/api/projects/${projectId}/documents/${documentId}`, {
      method: "DELETE",
    }),
  getDocument: (projectId: string, documentId: string) =>
    request<Document>(
      `/api/projects/${projectId}/documents/${documentId}`,
    ),
  reclassifyDocument: (projectId: string, documentId: string) =>
    request<Document>(
      `/api/projects/${projectId}/documents/${documentId}/reclassify`,
      { method: "POST" },
    ),
  listPages: (projectId: string, documentId: string) =>
    request<DocumentPage[]>(
      `/api/projects/${projectId}/documents/${documentId}/pages`,
    ),
  getPageText: (projectId: string, documentId: string, pageNumber: number) =>
    request<DocumentPageText>(
      `/api/projects/${projectId}/documents/${documentId}/pages/${pageNumber}/text`,
    ),

  // Authenticated image fetch — caller turns the blob into an object URL
  async fetchPageImage(
    projectId: string,
    documentId: string,
    pageNumber: number,
    variant: "image" | "thumbnail",
  ): Promise<Blob> {
    const token = useAuthStore.getState().token;
    const res = await fetch(
      `${API_URL}/api/projects/${projectId}/documents/${documentId}/pages/${pageNumber}/${variant}`,
      { headers: { Authorization: `Bearer ${token}` } },
    );
    if (!res.ok) throw new ApiError(`failed to fetch ${variant}`, res.status);
    return res.blob();
  },

  // Extraction (Phase 2)
  getExtractionOverview: (projectId: string, documentId: string) =>
    request<DocumentExtractionOverview>(
      `/api/projects/${projectId}/documents/${documentId}/extraction`,
    ),
  getPageExtraction: (projectId: string, documentId: string, pageNumber: number) =>
    request<PageExtraction>(
      `/api/projects/${projectId}/documents/${documentId}/pages/${pageNumber}/extraction`,
    ),
  reextractPage: (projectId: string, documentId: string, pageNumber: number) =>
    request<PageExtraction>(
      `/api/projects/${projectId}/documents/${documentId}/pages/${pageNumber}/reextract`,
      { method: "POST" },
    ),

  // Phase 3: Search
  search: (projectId: string, query: string, top_k = 10) =>
    request<SearchResponse>(`/api/projects/${projectId}/search`, {
      method: "POST",
      body: JSON.stringify({ query, top_k }),
    }),

  // Phase 4.1: Project profile
  getProjectProfile: (projectId: string) =>
    request<ProjectProfile | null>(`/api/projects/${projectId}/profile`),
  runProjectProfiler: (projectId: string) =>
    request<ProjectProfile>(`/api/projects/${projectId}/profile`, {
      method: "POST",
    }),

  // Phase 4.2: Trade relevance filter
  getTradeRelevance: (projectId: string) =>
    request<TradeRelevanceMatrix>(`/api/projects/${projectId}/trade-relevance`),
  runTradeRelevance: (projectId: string, force = false) =>
    request<TradeRelevanceMatrix>(
      `/api/projects/${projectId}/trade-relevance${force ? "?force=true" : ""}`,
      { method: "POST" },
    ),
  overrideTradeRelevance: (
    projectId: string,
    csi_division: string,
    is_relevant: boolean,
  ) =>
    request<unknown>(`/api/projects/${projectId}/trade-relevance`, {
      method: "PATCH",
      body: JSON.stringify({ csi_division, is_relevant }),
    }),

  // Phase 4.3 / 4.4: scope of work
  getScopeOverview: (projectId: string) =>
    request<ScopeOverview>(`/api/projects/${projectId}/scope`),
  startScopeRun: (projectId: string) =>
    request<ScopeRun>(`/api/projects/${projectId}/scope/runs`, {
      method: "POST",
    }),
  getScopeRun: (projectId: string, runId: string) =>
    request<ScopeRun>(`/api/projects/${projectId}/scope/runs/${runId}`),
  listScopeItems: (projectId: string, csi_division?: string) => {
    const qs = csi_division ? `?csi_division=${csi_division}` : "";
    return request<ScopeItem[]>(`/api/projects/${projectId}/scope/items${qs}`);
  },
  getScopeItem: (projectId: string, itemId: string) =>
    request<ScopeItem>(`/api/projects/${projectId}/scope/items/${itemId}`),

  // Phase 8: bid analysis
  getBidAnalysisOverview: (projectId: string) =>
    request<BidAnalysisOverview>(`/api/projects/${projectId}/bid-analysis`),
  startBidAnalysisRun: (projectId: string) =>
    request<BidRun>(`/api/projects/${projectId}/bid-analysis/runs`, {
      method: "POST",
    }),
  getBidAnalysisRun: (projectId: string, runId: string) =>
    request<BidRun>(`/api/projects/${projectId}/bid-analysis/runs/${runId}`),
  listBidCoverage: (projectId: string) =>
    request<BidCoverage[]>(`/api/projects/${projectId}/bid-analysis/coverage`),
  listBidGaps: (projectId: string) =>
    request<ScopeItem[]>(`/api/projects/${projectId}/bid-analysis/gaps`),
  getBidDetail: (projectId: string, bidDocumentId: string) =>
    request<BidDetail>(
      `/api/projects/${projectId}/bid-analysis/bids/${bidDocumentId}`,
    ),

  // Phase 11: vendor profile + bid leveling
  listVendors: (projectId: string) =>
    request<VendorSummary[]>(`/api/projects/${projectId}/vendors`),
  getVendorProfile: (projectId: string, vendorName: string) =>
    request<VendorProfile>(
      `/api/projects/${projectId}/vendors/${encodeURIComponent(vendorName)}`,
    ),
  getBidLeveling: (projectId: string, csi_division?: string) => {
    const qs = csi_division
      ? `?csi_division=${encodeURIComponent(csi_division)}`
      : "";
    return request<BidLevelingResponse>(
      `/api/projects/${projectId}/bid-leveling${qs}`,
    );
  },
};

export { ApiError };
