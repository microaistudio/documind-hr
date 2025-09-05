// Path: ui/src/components/SummarizeLLM.tsx
// Purpose: Per-row Summarize (LLM) + shared global settings; modal uses shared AnswerBody.
// Version: 1.5.1 — default timeout 60s; LLM → preview_v2; Local uses /summary.

import React, { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import AnswerBody from "./AnswerBody";

const LS_KEY = "llm_overrides_v1";

type Overrides = {
  max_tokens: number;
  temperature: number;
  top_p: number;
  presence_penalty: number;
  frequency_penalty: number;
  topk: number;
  percent_cap: number;
  timeout_ms: number;
  groundedness: number; // 0..1 (1 = strictly extractive)
};

const DEFAULTS: Overrides = {
  max_tokens: 768,
  temperature: 0.2,
  top_p: 1.0,
  presence_penalty: 0,
  frequency_penalty: 0,
  topk: 24,
  percent_cap: 100,
  timeout_ms: 60000, // default 60s
  groundedness: 1,
};

function loadOverrides(): Overrides {
  try {
    const raw = localStorage.getItem(LS_KEY);
    return raw ? { ...DEFAULTS, ...JSON.parse(raw) } : { ...DEFAULTS };
  } catch {
    return { ...DEFAULTS };
  }
}
function saveOverrides(ov: Overrides) {
  try {
    localStorage.setItem(LS_KEY, JSON.stringify(ov));
  } catch {}
}

/* ---------- Global LLM Settings (shared) ---------- */

export function GlobalLLMSettingsButton() {
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState<Overrides>(() => loadOverrides());

  // Seed once from backend defaults if nothing in localStorage
  useEffect(() => {
    try {
      if (localStorage.getItem(LS_KEY)) return;
    } catch {}
    (async () => {
      try {
        const r = await fetch("/api/llm/defaults");
        if (!r.ok) return;
        const d = await r.json();
        const seeded: Overrides = {
          ...DEFAULTS,
          max_tokens: d.max_tokens ?? DEFAULTS.max_tokens,
          temperature: d.temperature ?? DEFAULTS.temperature,
          top_p: d.top_p ?? DEFAULTS.top_p,
          presence_penalty: d.presence_penalty ?? DEFAULTS.presence_penalty,
          frequency_penalty: d.frequency_penalty ?? DEFAULTS.frequency_penalty,
          topk: d.topk ?? DEFAULTS.topk,
          percent_cap: d.percent_cap ?? DEFAULTS.percent_cap,
          timeout_ms: d.timeout_ms ?? DEFAULTS.timeout_ms,
          groundedness: d.groundedness ?? DEFAULTS.groundedness,
        };
        saveOverrides(seeded);
        setDraft(seeded);
      } catch {}
    })();
  }, []);

  useEffect(() => {
    if (open) setDraft(loadOverrides());
  }, [open]);

  function set<K extends keyof Overrides>(k: K, v: Overrides[K]) {
    setDraft((p) => ({ ...p, [k]: v }));
  }
  function numChange<K extends keyof Overrides>(k: K) {
    return (e: React.ChangeEvent<HTMLInputElement>) =>
      set(k, (Number(e.target.value) as any) ?? (DEFAULTS[k] as any));
  }
  function onSave() {
    const g = Math.max(0, Math.min(1, Number(draft.groundedness)));
    const next = { ...draft, groundedness: g };
    saveOverrides(next);
    window.dispatchEvent(new CustomEvent("llm-overrides-updated", { detail: next }));
    setOpen(false);
  }

  const preset = (ms: number) => () => setDraft((p) => ({ ...p, timeout_ms: ms }));
  const isPreset = (ms: number) => draft.timeout_ms === ms;

  return (
    <>
      <button
        onClick={() => setOpen(true)}
        className="inline-flex items-center gap-1 px-2 py-1 rounded border text-xs bg-white hover:bg-gray-50"
        title="Global LLM Settings (applies to all Summarize calls)"
      >
        ⚙︎ LLM Settings
      </button>

      {open &&
        createPortal(
          <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-[1100]" onClick={() => setOpen(false)}>
            <div
              className="bg-white w-[720px] max-h-[85vh] overflow-y-auto rounded-xl shadow-lg p-4 relative"
              onClick={(e) => e.stopPropagation()}
            >
              <div className="flex items-center justify-between mb-2 sticky top-0 bg-white z-10 -mx-4 px-4 py-2 border-b">
                <h3 className="text-lg font-semibold">Global LLM Settings</h3>
                <button onClick={() => setOpen(false)} className="text-gray-500 hover:text-black">✕</button>
              </div>

              <div className="grid grid-cols-2 gap-3">
                <label className="flex flex-col" title="Target tokens for the summary.">
                  Max tokens
                  <input type="number" min={64} max={4096} className="border rounded px-2 py-1"
                    value={draft.max_tokens} onChange={numChange("max_tokens")} />
                </label>
                <label className="flex flex-col" title="Lower = more deterministic.">
                  Temperature
                  <input type="number" step={0.1} min={0} max={2} className="border rounded px-2 py-1"
                    value={draft.temperature} onChange={numChange("temperature")} />
                </label>
                <label className="flex flex-col" title="Nucleus sampling (0–1).">
                  Top-p
                  <input type="number" step={0.05} min={0} max={1} className="border rounded px-2 py-1"
                    value={draft.top_p} onChange={numChange("top_p")} />
                </label>
                <label className="flex flex-col" title="Discourage repeating topics.">
                  Presence penalty
                  <input type="number" step={0.1} min={0} max={2} className="border rounded px-2 py-1"
                    value={draft.presence_penalty} onChange={numChange("presence_penalty")} />
                </label>
                <label className="flex flex-col" title="Discourage repeating tokens.">
                  Frequency penalty
                  <input type="number" step={0.1} min={0} max={2} className="border rounded px-2 py-1"
                    value={draft.frequency_penalty} onChange={numChange("frequency_penalty")} />
                </label>
                <label className="flex flex-col" title="Top-K retrieval snippets.">
                  Top-K chunks
                  <input type="number" min={1} max={200} className="border rounded px-2 py-1"
                    value={draft.topk} onChange={numChange("topk")} />
                </label>
                <label className="flex flex-col" title="Keep only this % of assembled text.">
                  Percent cap (%)
                  <input type="number" min={10} max={100} className="border rounded px-2 py-1"
                    value={draft.percent_cap} onChange={numChange("percent_cap")} />
                </label>
                <label className="flex flex-col" title="Deadline for each request (ms).">
                  Timeout (ms)
                  <input type="number" min={1000} max={600000} className="border rounded px-2 py-1"
                    value={draft.timeout_ms} onChange={numChange("timeout_ms")} />
                </label>

                <label className="flex flex-col col-span-2" title="How strictly the model must stick to the provided chunks.">
                  Groundedness (0–1)
                  <div className="flex items-center gap-2">
                    <input
                      type="range" min={0} max={1} step={0.05}
                      value={draft.groundedness}
                      onChange={numChange("groundedness")}
                      className="w-full"
                    />
                    <input
                      type="number" min={0} max={1} step={0.05}
                      value={draft.groundedness}
                      onChange={numChange("groundedness")}
                      className="border rounded px-2 py-1 w-24 text-right"
                    />
                  </div>
                  <span className="text-xs text-gray-500 mt-1">
                    1 = strictly extractive (no outside facts) • 0 = allow background context (clearly labeled).
                  </span>
                </label>
              </div>

              <div className="mt-3 flex items-center justify-between">
                <div className="flex items-center gap-2 text-sm">
                  <span className="font-medium mr-1">Preset:</span>
                  <button
                    className={`px-2 py-1 rounded border hover:bg-gray-50 ${isPreset(30000) ? "bg-violet-50 border-violet-300" : ""}`}
                    onClick={preset(30000)}
                  >
                    30s
                  </button>
                  <button
                    className={`px-2 py-1 rounded border hover:bg-gray-50 ${isPreset(60000) ? "bg-violet-50 border-violet-300" : ""}`}
                    onClick={preset(60000)}
                  >
                    60s
                  </button>
                  <button
                    className={`px-2 py-1 rounded border hover:bg-gray-50 ${isPreset(120000) ? "bg-violet-50 border-violet-300" : ""}`}
                    onClick={preset(120000)}
                  >
                    120s
                  </button>
                </div>

                <div className="flex items-center gap-2">
                  <button className="px-2 py-1 rounded border text-sm" onClick={() => setDraft(loadOverrides())}>
                    Revert
                  </button>
                  <button className="px-2 py-1 rounded border text-sm" onClick={() => setDraft({ ...DEFAULTS })}>
                    Reset
                  </button>
                  <button
                    className="px-3 py-1.5 rounded bg-violet-600 text-white text-sm font-medium hover:bg-violet-700"
                    onClick={onSave}
                  >
                    Save
                  </button>
                </div>
              </div>
            </div>
          </div>,
          document.body
        )}
    </>
  );
}

/* ---------- Row Summarize (with engine toggle + portal modal) ---------- */

type RowProps = { docId: string; defaultStyle?: "bullet" | "short" | "detailed" };

export default function SummarizeLLM({ docId, defaultStyle = "bullet" }: RowProps) {
  const [style, setStyle] = useState(defaultStyle);
  const initialLang = /-HI-/i.test(docId) ? "hi" : "en";
  
  const [lang, setLang] = useState<"en" | "hi">(initialLang);

  // keep it in sync if the row changes
  useEffect(() => {
    setLang(/-HI-/i.test(docId) ? "hi" : "en");
  }, [docId]);

    const [engine, setEngine] = useState<"llm" | "local">("llm");

  const [loading, setLoading] = useState(false);
  const [open, setOpen] = useState(false);
  const [summary, setSummary] = useState("");
  const [note, setNote] = useState("");

  const [ov, setOv] = useState<Overrides>(() => loadOverrides());
  useEffect(() => {
    const h = (e: any) => setOv(e.detail ?? loadOverrides());
    window.addEventListener("llm-overrides-updated", h as EventListener);
    return () => window.removeEventListener("llm-overrides-updated", h as EventListener);
  }, []);

  const abortRef = useRef<AbortController | null>(null);
  const [tookMs, setTookMs] = useState<number | null>(null);
  const [rawSource, setRawSource] = useState<string>("");
  const [llmStatus, setLlmStatus] = useState<"" | "ok" | "fallback">("");
  const [topkUsed, setTopkUsed] = useState<number | null>(null);
  const [percentCapUsed, setPercentCapUsed] = useState<number | null>(null);
  const [tokensIn, setTokensIn] = useState<number | null>(null);
  const [tokensOut, setTokensOut] = useState<number | null>(null);
  const [overridesUsed, setOverridesUsed] = useState<any>(null);

  const title = `${engine === "llm" ? "LLM Summary" : "Local Summary"} — ${docId}`;
  const sourceLabel = rawSource;

  async function doPreview() {
    setLoading(true);
    setSummary(""); setNote(""); setTookMs(null); setRawSource(""); setLlmStatus("");
    setTopkUsed(null); setPercentCapUsed(null); setOverridesUsed(null);
    setTokensIn(null); setTokensOut(null);

    const controller = new AbortController(); abortRef.current = controller;
    try {
      // Local engine → use extractive summary route directly (fast).
      if (engine === "local") {
        const res = await fetch(`/api/docs/${docId}/summary?k=${ov.topk || 5}&probes=11`, { method: "GET", signal: controller.signal });
        setTookMs(Number(res.headers.get("X-Response-Time-ms") || "0"));
        setRawSource("local");
        setLlmStatus("ok");
        const data = await res.json();
        setSummary(data?.text || "");
        setNote("");
        setOpen(true);
        return;
      }

      const res = await fetch(`/api/docs/${docId}/llm_summarize/preview_v2`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ style, lang, overrides: ov }),
        signal: controller.signal,
      });

      setTookMs(Number(res.headers.get("X-Response-Time-ms") || "0"));
      setRawSource(res.headers.get("X-LLM-Source") || "");
      setLlmStatus((res.headers.get("X-LLM-Status") as any) || "");
      setTopkUsed(Number(res.headers.get("X-LLM-TopK-Used") || "0") || null);
      setPercentCapUsed(Number(res.headers.get("X-LLM-PercentCap-Used") || "0") || null);
      setTokensIn(Number(res.headers.get("X-Tokens-In") || "0") || null);
      setTokensOut(Number(res.headers.get("X-Tokens-Out") || "0") || null);

      const ovHeader = res.headers.get("X-LLM-Overrides");
      if (ovHeader) { try { setOverridesUsed(JSON.parse(ovHeader)); } catch { setOverridesUsed(ovHeader); } }

      let data: any = null;
      const ctype = (res.headers.get("content-type") || "").toLowerCase();
      if (ctype.includes("application/json")) data = await res.json();
      else {
        const txt = await res.text();
        try { data = JSON.parse(txt); } catch { data = { summary: "", note: `non-json response (${res.status})` }; }
      }

      setSummary(data?.summary || ""); setNote(data?.note || ""); setOpen(true);

      try {
        window.dispatchEvent(new CustomEvent("llm-latency", {
          detail: {
            docId, tookMs: Number(res.headers.get("X-Response-Time-ms") || "0"),
          },
        }));
      } catch {}
    } catch {
      setSummary(""); setNote("Request failed."); setOpen(true);
    } finally {
      setLoading(false); abortRef.current = null;
    }
  }

  async function cancelPreview() {
    try { abortRef.current?.abort(); } catch {}
    setLoading(false);
    if (engine === "local") { setNote("Canceled."); setOpen(false); return; }
    try {
      // On cancel, quickly show a local extractive summary instead of waiting on LLM.
      const res = await fetch(`/api/docs/${docId}/summary?k=${ov.topk || 5}&probes=11`);
      setTookMs(Number(res.headers.get("X-Response-Time-ms") || "0"));
      setRawSource("local");
      setLlmStatus("ok");
      const data = await res.json();
      setSummary(data?.text || "");
      setNote("Canceled. Showing local summary."); setOpen(true);
    } catch { setSummary(""); setNote("Canceled."); setOpen(true); }
  }

  return (
    <div className="flex flex-wrap items-center gap-2">
      <select className="border rounded px-2 py-1 text-sm" value={style} onChange={(e) => setStyle(e.target.value as any)}>
        <option value="bullet">Bullet</option>
        <option value="short">Short</option>
        <option value="detailed">Detailed</option>
      </select>

      <select className="border rounded px-2 py-1 text-sm" value={lang} onChange={(e) => setLang(e.target.value as "en" | "hi")} title="Language for the output">
        <option value="en">en</option>
        <option value="hi">hi</option>
      </select>

      {/* Engine toggle (LLM vs Local) */}
      <div className="inline-flex rounded border overflow-hidden text-sm shrink-0" role="tablist" aria-label="Summary engine">
        <button
          type="button"
          onClick={() => setEngine("llm")}
          className={`px-2 py-1 ${engine === "llm" ? "bg-violet-600 text-white" : "bg-white"} border-r`}
          aria-selected={engine === "llm"}
        >
          LLM
        </button>
        <button
          type="button"
          onClick={() => setEngine("local")}
          className={`px-2 py-1 ${engine === "local" ? "bg-violet-600 text-white" : "bg-white"}`}
          aria-selected={engine === "local"}
        >
          Local
        </button>
      </div>

      <button onClick={doPreview} disabled={loading} className="px-2 py-1.5 rounded bg-violet-600 text-white text-sm font-medium hover:bg-violet-700 disabled:opacity-40">
        {loading ? "Summarizing…" : "Preview"}
      </button>

      {loading && (
        <button onClick={cancelPreview} className="px-2 py-1.5 rounded border text-sm" title="Cancel request">
          Cancel
        </button>
      )}

      <GlobalLLMSettingsButton />

      {/* Modal via Portal (escapes parent stacking contexts) */}
      {open &&
        createPortal(
          <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-[1200]">
            <div className="bg-white max-w-3xl w-[92%] max-h-[85vh] overflow-y-auto rounded-xl shadow-lg p-4 relative">
              <div className="flex items-center justify-between mb-2 sticky top-0 z-40 bg-white -mx-4 px-4 py-2 border-b">
                <h3 className="text-lg font-semibold">{title}</h3>
                <button onClick={() => setOpen(false)} className="text-gray-500 hover:text-black">✕</button>
              </div>

              <AnswerBody
                status={llmStatus}
                sourceLabel={sourceLabel}
                tookMs={tookMs}
                note={note}
                summary={summary}
                kUsed={topkUsed ?? undefined}
                percentCap={percentCapUsed ?? undefined}
                tokensIn={tokensIn ?? undefined}
                tokensOut={tokensOut ?? undefined}
                overrides={overridesUsed}
              />

              <div className="mt-4 flex items-center justify-between">
                <div className="text-xs text-gray-500">
                  <span className="mr-2">doc_id:</span>
                  <code className="bg-gray-50 px-1.5 py-0.5 rounded border">{docId}</code>
                </div>
                <div className="flex items-center gap-2">
                  <button
                    onClick={() => setOpen(false)}
                    className="px-3 py-1.5 rounded border text-sm hover:bg-gray-50"
                  >
                    Close
                  </button>
                  <button
                    onClick={async () => {
                      try {
                        const res = await fetch(`/api/docs/${docId}/llm_summarize/save`, {
                          method: "POST",
                          headers: { "Content-Type": "application/json" },
                          body: JSON.stringify({
                            style,
                            lang,
                            overrides: { topk: (overridesUsed?.topk ?? undefined), percent_cap: (overridesUsed?.percent_cap ?? undefined) },
                            summary,
                          }),
                        });
                        if (!res.ok) throw new Error("save failed");
                        setNote("Saved to DB.");
                      } catch {
                        setNote("Save failed.");
                      }
                    }}
                    disabled={loading || !summary}
                    className="px-3 py-1.5 rounded bg-violet-600 text-white text-sm font-medium hover:bg-violet-700 disabled:opacity-40"
                  >
                    {loading ? "Saving…" : "Save"}
                  </button>
                </div>
              </div>
            </div>
          </div>,
          document.body
        )}
    </div>
  );
}
