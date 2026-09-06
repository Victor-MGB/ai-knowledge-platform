const BASE = "";

class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
    public details?: unknown,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

let accessToken: string | null = localStorage.getItem("kf_access");
let refreshToken: string | null = localStorage.getItem("kf_refresh");

export function getAccessToken() {
  return accessToken;
}

export function getRefreshToken() {
  return refreshToken;
}

export function setTokens(access: string, refresh: string) {
  accessToken = access;
  refreshToken = refresh;
  localStorage.setItem("kf_access", access);
  localStorage.setItem("kf_refresh", refresh);
}

export function clearTokens() {
  accessToken = null;
  refreshToken = null;
  localStorage.removeItem("kf_access");
  localStorage.removeItem("kf_refresh");
}

let onUnauthorized: (() => void) | null = null;
export function setOnUnauthorized(fn: () => void) {
  onUnauthorized = fn;
}

async function tryRefresh(): Promise<boolean> {
  if (!refreshToken) return false;
  try {
    const res = await fetch(`${BASE}/auth/refresh`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ refreshToken }),
    });
    if (!res.ok) return false;
    const data = await res.json();
    setTokens(data.accessToken, data.refreshToken);
    return true;
  } catch {
    return false;
  }
}

export async function api<T>(
  path: string,
  opts: RequestInit = {},
): Promise<T> {
  const headers = new Headers(opts.headers);

  if (accessToken && !headers.has("Authorization")) {
    headers.set("Authorization", `Bearer ${accessToken}`);
  }
  if (
    opts.body &&
    !(opts.body instanceof FormData) &&
    !headers.has("Content-Type")
  ) {
    headers.set("Content-Type", "application/json");
  }

  let res = await fetch(`${BASE}${path}`, { ...opts, headers });

  if (res.status === 401 && accessToken) {
    const refreshed = await tryRefresh();
    if (refreshed) {
      headers.set("Authorization", `Bearer ${accessToken}`);
      res = await fetch(`${BASE}${path}`, { ...opts, headers });
    }
  }

  if (res.status === 401) {
    clearTokens();
    onUnauthorized?.();
    throw new ApiError(401, "UNAUTHORIZED", "Session expired");
  }

  if (res.status === 204) return undefined as T;

  const body = await res.json().catch(() => ({}));

  if (!res.ok) {
    const err = body?.error ?? body;
    throw new ApiError(
      res.status,
      err?.code ?? "UNKNOWN",
      err?.message ?? `Request failed (${res.status})`,
      err?.details,
    );
  }

  return body as T;
}

// Auth
export const authApi = {
  register: (data: {
    email: string;
    password: string;
    organization?: string;
    name?: string;
  }) =>
    api<{
      user: User;
      organization: Org;
      tokens: { accessToken: string; refreshToken: string };
    }>("/auth/register", {
      method: "POST",
      body: JSON.stringify(data),
    }),

  login: (data: { email: string; password: string }) =>
    api<{
      user: User;
      tokens: { accessToken: string; refreshToken: string };
    }>("/auth/login", {
      method: "POST",
      body: JSON.stringify(data),
    }),

  logout: () =>
    api<void>("/auth/logout", {
      method: "POST",
      body: JSON.stringify({ refreshToken }),
    }),
};

// Me
export const meApi = {
  get: () => api<User>("/api/v1/me"),
};

// Documents
export const documentsApi = {
  list: (params?: {
    limit?: number;
    offset?: number;
    sourceType?: string;
    status?: string;
    search?: string;
  }) => {
    const q = new URLSearchParams();
    if (params?.limit) q.set("limit", String(params.limit));
    if (params?.offset) q.set("offset", String(params.offset));
    if (params?.sourceType) q.set("sourceType", params.sourceType);
    if (params?.status) q.set("status", params.status);
    if (params?.search) q.set("search", params.search);
    const qs = q.toString();
    return api<{ items: Document[]; pagination: Pagination }>(
      `/api/v1/documents${qs ? `?${qs}` : ""}`,
    );
  },

  get: (id: string) => api<Document>(`/api/v1/documents/${id}`),

  status: (id: string) => api<DocumentStatus>(`/api/v1/documents/${id}/status`),

  upload: (file: File, onProgress?: (pct: number) => void) => {
    return new Promise<Document>((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", `${BASE}/api/v1/documents`);
      if (accessToken) {
        xhr.setRequestHeader("Authorization", `Bearer ${accessToken}`);
      }
      xhr.onload = () => {
        if (xhr.status >= 200 && xhr.status < 300) {
          resolve(JSON.parse(xhr.responseText));
        } else {
          try {
            const err = JSON.parse(xhr.responseText);
            reject(
              new ApiError(
                xhr.status,
                err?.error?.code ?? "UPLOAD_FAILED",
                err?.error?.message ?? "Upload failed",
              ),
            );
          } catch {
            reject(new ApiError(xhr.status, "UPLOAD_FAILED", "Upload failed"));
          }
        }
      };
      xhr.onerror = () => reject(new ApiError(0, "NETWORK", "Network error"));
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable) onProgress?.(Math.round((e.loaded / e.total) * 100));
      };
      const form = new FormData();
      form.append("file", file);
      xhr.send(form);
    });
  },

  remove: (id: string) =>
    api<void>(`/api/v1/documents/${id}`, { method: "DELETE" }),
};

// Search
export const searchApi = {
  search: (query: string, opts?: { limit?: number; minSimilarity?: number }) =>
    api<SearchResponse>("/api/v1/search", {
      method: "POST",
      body: JSON.stringify({ query, ...opts }),
    }),
};

// RAG
export const ragApi = {
  generate: (question: string, opts?: { limit?: number; minScore?: number }) =>
    api<RagResponse>("/api/v1/rag/generate", {
      method: "POST",
      body: JSON.stringify({ question, ...opts }),
    }),

  /** Day 27 streaming — POST to the SSE endpoint and resolve the answer
   * tokens + final generation. Returns a controller with `read()` that
   * resolves to `{ deltas, done }` and `.abort()` to cancel. */
  generateStream: (question: string, opts?: { limit?: number; minScore?: number }) => {
    let controller: AbortController | null = null;

    const read = async (): Promise<{ deltas: string; done: RagResponse }> => {
      controller = new AbortController();
      let deltas = "";
      const done: RagResponse = {
        question,
        answer: "",
        refused: false,
        provider: "",
        model: "",
        retrieval: { query: "", model: "", retrieved: 0 },
        evidence: [],
        citations: [],
        usage: { promptTokens: 0, completionTokens: 0, totalTokens: 0 },
      };

      const res = await fetch(`${BASE}/api/v1/rag/generate/stream`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...(accessToken ? { Authorization: `Bearer ${accessToken}` } : {}),
        },
        body: JSON.stringify({ question, ...opts }),
        signal: controller.signal,
      });

      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        const err = body?.error ?? {};
        throw new ApiError(
          res.status,
          err?.code ?? "RAG_STREAM_FAILED",
          err?.message ?? `Request failed (${res.status})`,
        );
      }
      if (!res.body) throw new ApiError(0, "RAG_STREAM_FAILED", "No response body");

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      for (;;) {
        const { done: readerDone, value } = await reader.read();
        if (readerDone) break;
        buffer += decoder.decode(value, { stream: true });

        // entries separated by blank lines
        let newlineIndex: number;
        while ((newlineIndex = buffer.indexOf("\n\n")) !== -1) {
          const chunk = buffer.slice(0, newlineIndex);
          buffer = buffer.slice(newlineIndex + 2);
          const line = chunk.trim();
          if (!line.startsWith("data:")) continue;
          const payload = line.slice(5).trim();
          if (payload === "[DONE]") continue;
          try {
            const parsed = JSON.parse(payload);
            if (typeof parsed.delta === "string") {
              deltas += parsed.delta;
            } else if (parsed.done) {
              Object.assign(done, parsed.done as RagResponse);
            }
          } catch {
            /* ignore malformed chunk */
          }
        }
      }

      return { deltas, done };
    };

    return {
      read,
      abort: () => controller?.abort(),
    };
  },
};

// Sources
export const sourcesApi = {
  get: (documentId: string) =>
    api<SourceResponse>(`/api/v1/sources/${documentId}`),
};

// Conversations
export const conversationsApi = {
  list: (params?: { limit?: number; offset?: number }) => {
    const q = new URLSearchParams();
    if (params?.limit) q.set("limit", String(params.limit));
    if (params?.offset) q.set("offset", String(params.offset));
    const qs = q.toString();
    return api<{ items: Conversation[]; pagination: Pagination }>(
      `/api/v1/conversations${qs ? `?${qs}` : ""}`,
    );
  },

  get: (id: string) => api<Conversation>(`/api/v1/conversations/${id}`),

  create: (title?: string) =>
    api<Conversation>("/api/v1/conversations", {
      method: "POST",
      body: JSON.stringify({ title }),
    }),

  addMessage: (id: string, content: string) =>
    api<Conversation>(`/api/v1/conversations/${id}/messages`, {
      method: "POST",
      body: JSON.stringify({ content, rag: {} }),
    }),

  remove: (id: string) =>
    api<void>(`/api/v1/conversations/${id}`, { method: "DELETE" }),
};

// Organizations
export const orgApi = {
  get: () => api<OrgProfile>("/api/v1/organizations"),

  members: () =>
    api<{ organization: OrgProfile; items: Member[] }>(
      "/api/v1/organizations/members",
    ),

  updateRole: (userId: string, role: string) =>
    api<Member>(`/api/v1/organizations/members/${userId}/role`, {
      method: "PATCH",
      body: JSON.stringify({ role }),
    }),

  invitations: () =>
    api<{ organization: OrgProfile; items: Invitation[] }>(
      "/api/v1/organizations/invitations",
    ),

  invite: (email: string, role: string) =>
    api<Invitation>("/api/v1/organizations/invitations", {
      method: "POST",
      body: JSON.stringify({ email, role }),
    }),

  revokeInvitation: (id: string) =>
    api<void>(`/api/v1/organizations/invitations/${id}`, {
      method: "DELETE",
    }),
};

// Types
export interface User {
  id: string;
  email: string;
  role: string;
  organizationId: string;
  createdAt: string;
  lastLoginAt: string | null;
}

export interface Org {
  id: string;
  name: string;
  slug: string;
}

export interface Document {
  id: string;
  organizationId: string;
  uploadedBy: string;
  title: string;
  filename: string;
  mimeType: string;
  size: number;
  sourceType: string;
  storageKey: string | null;
  status: string;
  error: string | null;
  embeddingStatus: string;
  embeddingError: string | null;
  createdAt: string;
  updatedAt: string;
}

export interface DocumentStatus extends Document {
  counts: { pages: number; chunks: number; embeddings: number };
  stage: string;
}

export interface Pagination {
  limit: number;
  offset: number;
  total: number;
  hasMore: boolean;
}

export interface SearchResult {
  chunk: {
    id: string;
    chunkIndex: number;
    pageNumber: number;
    tokenCount: number;
    content: string;
  };
  similarity: number;
  document: {
    id: string;
    title: string;
    filename: string;
    mimeType: string;
    sourceType: string;
  };
  page: number;
  metadata: Record<string, unknown>;
}

export interface SearchResponse {
  query: string;
  model: string;
  results: SearchResult[];
}

export interface Citation {
  id: number;
  title: string | null;
  section: string | null;
  page: number | null;
  chunkId: string | null;
  documentId: string | null;
  similarity: number | null;
}

export interface RagResponse {
  question: string;
  answer: string;
  refused: boolean;
  provider: string;
  model: string;
  retrieval: { query: string; model: string; retrieved: number };
  evidence: {
    index: number;
    chunkId: string | null;
    documentId: string | null;
    documentTitle: string | null;
    section: string | null;
    page: number | null;
    similarity: number | null;
  }[];
  citations: Citation[];
  usage: {
    promptTokens: number;
    completionTokens: number;
    totalTokens: number;
  };
}

export interface SourceResponse {
  document: {
    id: string;
    title: string;
    filename: string;
    sourceType: string;
    size: number;
    createdAt: string;
  };
  pages: number;
}

export interface Conversation {
  id: string;
  organizationId: string;
  title: string;
  createdBy: string;
  createdAt: string;
  updatedAt: string;
  messageCount: number;
  lastMessageAt: string | null;
  messages?: Message[];
}

export interface Message {
  id: string;
  organizationId?: string;
  conversationId: string;
  position: number;
  role: "user" | "assistant" | "system";
  content: string;
  payload: Record<string, unknown>;
  createdAt: string;
}

export interface OrgProfile {
  id: string;
  name: string;
  slug: string;
  memberCount: number;
}

export interface Member {
  id: string;
  email: string;
  role: string;
  createdAt: string;
  lastLoginAt: string | null;
}

export interface Invitation {
  id: string;
  email: string;
  role: string;
  status: string;
  expiresAt: string;
  createdAt: string;
  token?: string;
}
