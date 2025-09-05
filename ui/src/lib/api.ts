// ui/src/lib/api.ts
// Single source for API calls.
// Convention: callers pass full paths that START WITH "/api" (visible /api everywhere).

type Json = Record<string, any> | undefined;

export type FetchJSONOptions = {
  method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  body?: Json;
  headers?: Record<string, string>;
  signal?: AbortSignal;
};

export type JSONWithMeta<T> = T & {
  __meta?: {
    status: number;
    ms?: number;          // parsed from X-Response-Time-ms if present
    traceId?: string | null;
  };
};

// ---------- low-level helpers ----------

const BASE_URL = ""; // keep empty; paths already include /api and go to same origin

function joinUrl(path: string) {
  if (!path.startsWith("/")) return `/${path}`;
  return path;
}

async function fetchJSON<T = any>(
  path: string,
  opts: FetchJSONOptions = {}
): Promise<JSONWithMeta<T>> {
  const url = BASE_URL + joinUrl(path);

  const method = opts.method ?? (opts.body ? "POST" : "GET");

  const headers: Record<string, string> = {
    "Accept": "application/json",
    ...(opts.body ? { "Content-Type": "application/json" } : {}),
    ...(opts.headers ?? {}),
  };

  const res = await fetch(url, {
    method,
    headers,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
    signal: opts.signal,
    credentials: "same-origin",
  });

  const text = await res.text();
  const msHeader = res.headers.get("x-response-time-ms") || res.headers.get("x-response-time");
  const traceId = res.headers.get("x-trace-id");

  let data: any;
  try {
    data = text ? JSON.parse(text) : {};
  } catch {
    data = { raw: text };
  }

  // Attach meta (never override server fields)
  data.__meta = {
    status: res.status,
    ms: msHeader ? Number(msHeader) : undefined,
    traceId,
  };

  if (!res.ok) {
    const msg =
      (data && (data.detail || data.message || data.error)) ||
      `${res.status} ${res.statusText}`;
    const err = new Error(
      typeof msg === "string" ? msg : JSON.stringify(msg)
    ) as any;
    err.status = res.status;
    err.meta = data.__meta;
    throw err;
  }

  return data as JSONWithMeta<T>;
}

// ---------- public, generic helpers ----------

export async function getJSON<T = any>(path: string, headers?: Record<string, string>) {
  return fetchJSON<T>(path, { method: "GET", headers });
}

export async function postJSON<T = any>(path: string, body?: Json, headers?: Record<string, string>) {
  return fetchJSON<T>(path, { method: "POST", body, headers });
}

// ---------- LLM helpers (used by SummarizeLLM.tsx and OpsDashboard) ----------

export type LlmPreviewResp = {
  doc_id: string;
  summary: string;
  note?: string;
};

export type LlmSaveResp = {
  ok: boolean;
  saved_to?: string;
  note?: string;
};

/**
 * Preview a summary for a document via LLM.
 * IMPORTANT: Always send an object body { style }, not a raw string.
 */
export async function llmPreview(docId: string, style: "bullet" | "short" | "detailed"): Promise<JSONWithMeta<LlmPreviewResp>> {
  const path = `/api/docs/${encodeURIComponent(docId)}/llm_summarize/preview`;
  // ✅ The previous 422 came from sending "bullet" as a raw string.
  // We now send a JSON object { style } as the backend expects.
  return postJSON<LlmPreviewResp>(path, { style });
}

/**
 * Persist the generated summary.
 */
export async function llmSave(docId: string, style: "bullet" | "short" | "detailed"): Promise<JSONWithMeta<LlmSaveResp>> {
  const path = `/api/docs/${encodeURIComponent(docId)}/llm_summarize/save`;
  return postJSON<LlmSaveResp>(path, { style });
}
