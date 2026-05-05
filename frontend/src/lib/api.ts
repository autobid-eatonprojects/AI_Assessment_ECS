import { useAuthStore } from "./auth";
import type { Document, Project, TokenResponse, User } from "./types";

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

  listDocuments: (projectId: string) =>
    request<Document[]>(`/api/projects/${projectId}/documents`),
  uploadDocuments: (projectId: string, files: File[]) => {
    const fd = new FormData();
    files.forEach((f) => fd.append("files", f));
    return request<Document[]>(`/api/projects/${projectId}/documents`, {
      method: "POST",
      body: fd,
    });
  },
  deleteDocument: (projectId: string, documentId: string) =>
    request<void>(`/api/projects/${projectId}/documents/${documentId}`, {
      method: "DELETE",
    }),
  documentDownloadUrl: (projectId: string, documentId: string) => {
    const token = useAuthStore.getState().token;
    return `${API_URL}/api/projects/${projectId}/documents/${documentId}/download?token=${token}`;
  },
};

export { ApiError };
