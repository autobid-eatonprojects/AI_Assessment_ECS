import { useAuthStore } from "./auth";
import type {
  AppSettings,
  AuditLogEntry,
  BidAnalysisOverview,
  BidCoverage,
  BidDetail,
  BidLevelingResponse,
  BidRun,
  Conflict,
  Document,
  Gap,
  VendorProfile,
  VendorSummary,
  DocumentExtractionOverview,
  DocumentPage,
  DocumentPageText,
  PageExtraction,
  Project,
  ProjectProfile,
  RfiItem,
  ScopeItem,
  ScopeOverview,
  ScopeRun,
  SearchResponse,
  SystemStatus,
  TokenResponse,
  TradePackage,
  TradePackageDetail,
  TradeRelevanceMatrix,
  TrustScore,
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

  // ----- Stage 6 — Trust score -----
  getTrustScore: (projectId: string) =>
    request<TrustScore | null>(`/api/projects/${projectId}/scope/trust-score`),

  // ----- Stage 7 — Review queues -----
  listConflicts: (projectId: string, status?: string) => {
    const qs = status === undefined ? "" : `?status=${encodeURIComponent(status)}`;
    return request<Conflict[]>(`/api/projects/${projectId}/review/conflicts${qs}`);
  },
  resolveConflict: (
    projectId: string,
    conflictId: string,
    body: { winner_member_id: string; note?: string },
  ) =>
    request<Conflict>(
      `/api/projects/${projectId}/review/conflicts/${conflictId}/resolve`,
      { method: "POST", body: JSON.stringify(body) },
    ),
  listGaps: (
    projectId: string,
    opts?: { status?: string; severity?: string[] },
  ) => {
    const params = new URLSearchParams();
    if (opts?.status !== undefined) params.set("status", opts.status);
    for (const sev of opts?.severity ?? []) params.append("severity", sev);
    const qs = params.toString() ? `?${params.toString()}` : "";
    return request<Gap[]>(`/api/projects/${projectId}/review/gaps${qs}`);
  },
  acknowledgeGap: (projectId: string, gapId: string, body: { note?: string }) =>
    request<Gap>(
      `/api/projects/${projectId}/review/gaps/${gapId}/acknowledge`,
      { method: "POST", body: JSON.stringify(body) },
    ),
  listLowConfidence: (projectId: string) =>
    request<ScopeItem[]>(`/api/projects/${projectId}/review/low-confidence`),
  reclassifyItem: (
    projectId: string,
    itemId: string,
    body: { new_csi_code: string; note?: string },
  ) =>
    request<ScopeItem>(
      `/api/projects/${projectId}/review/items/${itemId}/reclassify`,
      { method: "POST", body: JSON.stringify(body) },
    ),

  // ----- Stage 7 — Trade packages -----
  listPackages: (projectId: string) =>
    request<TradePackage[]>(`/api/projects/${projectId}/packages`),
  getPackageDetail: (projectId: string, packageId: string) =>
    request<TradePackageDetail>(
      `/api/projects/${projectId}/packages/${packageId}`,
    ),
  movePackageItem: (
    projectId: string,
    fromPackageId: string,
    itemId: string,
    body: { target_package_id: string; note?: string },
  ) =>
    request<ScopeItem>(
      `/api/projects/${projectId}/packages/${fromPackageId}/items/${itemId}/move`,
      { method: "POST", body: JSON.stringify(body) },
    ),

  // ----- Stage 7 — Audit log -----
  listAuditLog: (
    projectId: string,
    opts?: { entity_type?: string; limit?: number },
  ) => {
    const params = new URLSearchParams();
    if (opts?.entity_type) params.set("entity_type", opts.entity_type);
    if (opts?.limit !== undefined) params.set("limit", String(opts.limit));
    const qs = params.toString() ? `?${params.toString()}` : "";
    return request<AuditLogEntry[]>(`/api/projects/${projectId}/audit-log${qs}`);
  },
  listLLMCalls: (
    projectId: string,
    opts?: { purpose?: string; limit?: number },
  ) => {
    const params = new URLSearchParams();
    if (opts?.purpose) params.set("purpose", opts.purpose);
    if (opts?.limit !== undefined) params.set("limit", String(opts.limit));
    const qs = params.toString() ? `?${params.toString()}` : "";
    return request<
      Array<{
        id: string;
        purpose: string;
        model: string;
        provider: string;
        prompt_tokens: number | null;
        completion_tokens: number | null;
        cache_read_tokens: number | null;
        cost_usd: number | null;
        latency_ms: number | null;
        status: string;
        created_at: string;
      }>
    >(`/api/projects/${projectId}/audit-log/llm-calls${qs}`);
  },

  // ----- Stage 8 — App settings + system status -----
  getSettings: () => request<AppSettings>(`/api/settings`),
  patchSettings: (body: Partial<AppSettings>) =>
    request<AppSettings>(`/api/settings`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  providerHealth: (provider: string) =>
    request<{ provider: string; ok: boolean; detail: string | null }>(
      `/api/settings/health/${encodeURIComponent(provider)}`,
    ),
  getSystemStatus: () => request<SystemStatus>(`/api/settings/status`),

  // ----- P9 — RFI list -----
  generateRfis: (projectId: string, runId?: string) => {
    const qs = runId ? `?run_id=${encodeURIComponent(runId)}` : "";
    return request<{
      project_id: string;
      project_name: string;
      run_id: string;
      rfi_count: number;
      cost_usd: number;
      rfi_list: RfiItem[];
    }>(`/api/projects/${projectId}/rfi/generate${qs}`, { method: "POST" });
  },
  getRfis: (projectId: string, runId?: string) => {
    const qs = runId ? `?run_id=${encodeURIComponent(runId)}` : "";
    return request<{
      project_id: string;
      project_name: string;
      run_id: string;
      rfi_count: number;
      cost_usd: number;
      rfi_list: RfiItem[];
    }>(`/api/projects/${projectId}/rfi${qs}`);
  },

  // ----- P8 — Fine-tuning dataset URL -----
  linkJudgeDatasetUrl: (projectId: string) =>
    `/api/projects/${projectId}/exports/link-judge-dataset`,
};

export { ApiError };
