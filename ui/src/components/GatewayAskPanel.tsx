// Path: ui/src/components/GatewayAskPanel.tsx
// Purpose: Ask panel that now forwards global LLM overrides (incl. groundedness) and exposes the same Settings button.

import { useEffect, useMemo, useState } from "react";
import { GlobalLLMSettingsButton } from "./SummarizeLLM";

type DocItem = {
  doc_id: string;
  title?: string;
};

type AskResp = {
  answer: string;
  cites: { doc_id: string; chunk_index: number | null }[];
};

type Overrides = {
  max_tokens?: number;
  temperature?: number;
  top_p?: number;
  presence_penalty?: number;
  frequency_penalty?: number;
  timeout_ms?: number;
  topk?: number;
  percent_cap?: number;
  groundedness?: number; // NEW
};

const LS_KEY = "llm_overrides_v1";
function loadOverrides(): Overrides {
  try {
    const raw = localStorage.getItem(LS_KEY);
    const d = raw ? JSON.parse(raw) : {};
    // ensure groundedness has a sane default
    if (typeof d.groundedness !== "number") d.groundedness = 1;
    return d;
  } catch {
    return { groundedness: 1 };
  }
}

function backendBase(): string {
  // Use same host as the UI, but port 9000
  return `${window.location.protocol}//${window.location.hostname}:9000`;
}

export default function GatewayAskPanel() {
  const [docs, setDocs] = useState<DocItem[]>([]);
  const [docId, setDocId] = useState<string>("");
  const [q, setQ] = useState<string>("");
  const [k, setK] = useState<number>(3);
  const [loading, setLoading] = useState(false);
  const [resp, setResp] = useState<AskResp | null>(null);
  const [error, setError] = useState<string>("");
  const [ov, setOv] = useState<Overrides>(() => loadOverrides()); // NEW

  const base = useMemo(backendBase, []);

  useEffect(() => {
    // load a small list of docs for the selector
    fetch(`${base}/api/docs?limit=50`)
      .then(r => r.json())
      .then(data => {
        const items = (data?.items ?? []) as any[];
        const list = items.map((it) => ({
          doc_id: it.doc_id,
          title: it.title || it.doc_id
        })) as DocItem[];
        setDocs(list);
        if (!docId && list.length > 0) setDocId(list[0].doc_id);
      })
      .catch(() => {
        // non-fatal for demo
      });
  }, [base]); // eslint-disable-line react-hooks/exhaustive-deps

  // Listen for settings changes fired by the Settings modal (shared with Summarize)
  useEffect(() => {
    const h = (e: any) => setOv(e.detail ?? loadOverrides());
    window.addEventListener("llm-overrides-updated", h as EventListener);
    return () => window.removeEventListener("llm-overrides-updated", h as EventListener);
  }, []);

  async function onAsk() {
    setError("");
    setResp(null);
    if (!q.trim()) {
      setError("Please enter a question.");
      return;
    }
    try {
      setLoading(true);
      const body: any = { q: q.trim(), k, overrides: ov }; // NEW: pass overrides (includes groundedness)
      if (docId) body.doc_id = docId;
      const r = await fetch(`${base}/gateway/ask`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await r.json();
      if (!r.ok) {
        setError(typeof data?.detail === "string" ? data.detail : "Request failed");
      } else {
        setResp(data as AskResp);
      }
    } catch (e: any) {
      setError(e?.message || "Network error");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="rounded-2xl border p-4 mb-4">
      <div className="flex flex-wrap gap-3 items-end">
        <div className="flex-1 min-w-[280px]">
          <label className="block text-sm mb-1">Question</label>
          <input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="Ask about benefits, eligibility, incentives..."
            className="w-full border rounded-xl px-3 py-2"
          />
        </div>

        <div>
          <label className="block text-sm mb-1">Document</label>
          <select
            value={docId}
            onChange={(e) => setDocId(e.target.value)}
            className="border rounded-xl px-3 py-2 min-w-[220px]"
          >
            {docs.map((d) => (
              <option key={d.doc_id} value={d.doc_id}>
                {d.title || d.doc_id}
              </option>
            ))}
          </select>
        </div>

        <div>
          <label className="block text-sm mb-1">K</label>
          <select
            value={k}
            onChange={(e) => setK(parseInt(e.target.value, 10))}
            className="border rounded-xl px-3 py-2"
          >
            {[1, 2, 3, 4, 5].map((n) => (
              <option key={n} value={n}>{n}</option>
            ))}
          </select>
        </div>

        <button
          onClick={onAsk}
          disabled={loading}
          className="px-4 py-2 rounded-xl bg-black text-white disabled:opacity-50"
        >
          {loading ? "Asking…" : "Ask"}
        </button>

        {/* NEW: share the same LLM Settings modal used by Summarize */}
        <GlobalLLMSettingsButton />
        {/* Optional tiny badge showing current groundedness */}
        <span className="text-xs text-gray-600 ml-1" title="Groundedness (0..1)">
          g={typeof ov.groundedness === "number" ? ov.groundedness.toFixed(2) : "1.00"}
        </span>
      </div>

      {error && (
        <div className="mt-3 text-red-600 text-sm">{error}</div>
      )}

      {resp && (
        <div className="mt-4 grid gap-3">
          <div>
            <div className="text-sm font-medium mb-1">Answer (stitched previews)</div>
            <pre className="whitespace-pre-wrap text-sm border rounded-xl p-3 bg-white">
{resp.answer}
            </pre>
          </div>

          <div>
            <div className="text-sm font-medium mb-2">Citations</div>
            <div className="flex flex-col gap-2">
              {resp.cites.map((c, i) => {
                const chunk = c.chunk_index ?? 0;
                return (
                  <div key={i} className="flex items-center gap-3 text-sm">
                    <code className="px-2 py-1 rounded bg-gray-100">
                      {c.doc_id} · chunk #{chunk}
                    </code>
                    <a
                      className="underline"
                      href={`${base}/api/docs/${encodeURIComponent(c.doc_id)}/chunk/${chunk}`}
                      target="_blank" rel="noreferrer"
                      title="Open chunk JSON"
                    >
                      Open chunk
                    </a>
                    <a
                      className="underline"
                      href={`${base}/api/docs/${encodeURIComponent(c.doc_id)}/pdf`}
                      target="_blank" rel="noreferrer"
                      title="Open PDF"
                    >
                      Open PDF
                    </a>
                  </div>
                );
              })}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
