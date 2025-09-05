// ui/src/pages/Ask.tsx
import React, { useMemo, useRef, useState } from "react";

/** Ask DocuMind — Chat UI with force-local, LLM settings (top-k, evidence_k, tokens), and fixed download links */

type Citation = { doc_id: string; chunks?: number[] };
type Meta = {
  took_ms?: number;
  k?: number;
  evidence_k?: number;
  percent_cap?: number;
  passages?: number;
  source?: string;          // "llm" | "local" | "fallback"
  llm_elapsed_ms?: number;
  used_tokens?: number;
  max_tokens?: number;
};
type AskResponse = {
  answer?: string;
  citations?: Citation[];
  confidence?: number;
  grounded?: boolean;
  meta?: Meta;
};

type LlmSettings = {
  max_tokens: number;
  temperature: number;
  top_p: number;
  presence_penalty: number;
  frequency_penalty: number;
  percent_cap: number;
  top_k_chunks: number; // retrieval recall
  evidence_k: number;   // forwarded passages to LLM/local
  timeout_ms: number;
};

const DEFAULTS: LlmSettings = {
  max_tokens: 512,
  temperature: 0.2,
  top_p: 1,
  presence_penalty: 0,
  frequency_penalty: 0,
  percent_cap: 60,
  top_k_chunks: 12,
  evidence_k: 8,
  timeout_ms: 60000,
};

const SETTINGS_KEY = "documind.llm.settings";

const detectBackendBase = (): string => {
  const env = (import.meta as any)?.env?.VITE_DM_BACKEND as string | undefined;
  if (env && env.trim()) return env.replace(/\/$/, "");
  const u = new URL(window.location.href);
  const devPorts = new Set(["5173", "3000"]);
  if (devPorts.has(u.port)) return `${u.protocol}//${u.hostname}:9000`;
  return `${u.protocol}//${u.host}`;
};

const cls = (...xs: (string | false | null | undefined)[]) =>
  xs.filter(Boolean).join(" ");

/** Build a static PDF link from doc_id.
 *  Uses UI origin, strips leading zeros, and dept/lang selections.
 *  Example working path: http://host:5173/files/pdfs/animal/en/1.pdf
 */
const pdfUrl = (docId: string, lang: string, dept: string) => {
  const origin = window.location.origin; // UI host/port (works for /files/pdfs/…)
  const n = Number((docId || "").replace(/\D+/g, "")) || 1; // strip zeros -> 5 not 0005
  const d = dept || (docId?.startsWith("ANI") ? "animal" : "misc");
  const l = lang || "en";
  return `${origin}/files/pdfs/${d}/${l}/${n}.pdf`;
};

function useSettings(): [LlmSettings, (s: Partial<LlmSettings>) => void, () => void] {
  const [s, setS] = useState<LlmSettings>(() => {
    try {
      const raw = localStorage.getItem(SETTINGS_KEY);
      return raw ? { ...DEFAULTS, ...JSON.parse(raw) } : DEFAULTS;
    } catch {
      return DEFAULTS;
    }
  });
  const update = (patch: Partial<LlmSettings>) => {
    setS((prev) => {
      const next = { ...prev, ...patch };
      localStorage.setItem(SETTINGS_KEY, JSON.stringify(next));
      return next;
    });
  };
  const reset = () => {
    localStorage.setItem(SETTINGS_KEY, JSON.stringify(DEFAULTS));
    setS({ ...DEFAULTS });
  };
  return [s, update, reset];
}

function StatChip({ label, value }: { label: string; value?: number | string }) {
  if (value === undefined || value === null || value === "") return null;
  return (
    <span className="px-2 py-0.5 rounded-full text-[11px] bg-white/5 border border-white/10">
      {label}: {String(value)}
    </span>
  );
}

/** Existing mode badge (kept to avoid visual regressions) */
const ModeBadge = ({ value }: { value: "llm" | "local" }) => (
  <span
    className={
      "px-2 py-1 rounded-full text-xs border shadow-sm " +
      (value === "llm"
        ? "bg-violet-500/15 text-violet-200 border-violet-400/40"
        : "bg-fuchsia-500/15 text-fuchsia-200 border-fuchsia-400/40")
    }
    title={value === "llm" ? "LLM (grounded)" : "Local summarizer"}
  >
    {value.toUpperCase()}
  </span>
);

/** NEW: tiny LED indicator for source (“llm”, “local”, “fallback”, etc.) */
type LedMode = "llm" | "local" | "fallback" | "loading" | "error";
const ledStyles: Record<LedMode, { dot: string; ring: string; text: string; label: string }> = {
  llm:      { dot: "bg-violet-500",  ring: "ring-violet-300/60",  text: "text-violet-200",  label: "LLM" },
  local:    { dot: "bg-fuchsia-500", ring: "ring-fuchsia-300/60", text: "text-fuchsia-200", label: "Local" },
  fallback: { dot: "bg-amber-500",   ring: "ring-amber-300/60",   text: "text-amber-200",   label: "Fallback" },
  loading:  { dot: "bg-slate-400",   ring: "ring-slate-300/60",   text: "text-slate-300",   label: "Working…" },
  error:    { dot: "bg-rose-500",    ring: "ring-rose-300/60",    text: "text-rose-200",    label: "Error" },
};
function StatusLED({ mode, label, pulsing, title }: { mode: LedMode; label?: string; pulsing?: boolean; title?: string }) {
  const s = ledStyles[mode];
  return (
    <span className={`inline-flex items-center gap-1 ${s.text}`} title={title || label || s.label}>
      <span className={`w-2 h-2 rounded-full ${s.dot} ring ${s.ring} ${pulsing ? "animate-pulse" : ""}`} />
      <span className="text-[11px]">{label || s.label}</span>
    </span>
  );
}

/** Remove any [DOC#...] tags and tidy whitespace (kept from your working build) */
const cleanAnswer = (raw: string | undefined) => {
  if (!raw) return "";
  let t = raw;
  t = t.replace(/\s*\[DOC#(?:chunk|chunks?)?:[\s\S]*?\]\s*/gi, " ");
  t = t.replace(/\s*\[DOC#[\s\S]*?\]\s*/gi, " ");
  t = t.replace(/^\s*\[DOC#[\s\S]*?\]\s*$/gim, "");
  t = t.replace(/[ \t]{2,}/g, " ").replace(/\n{3,}/g, "\n\n").trim();
  return t;
};

/** NEW: make local answers look like the Ops modal (bullets + sentence case). */
const prettifyLocal = (raw: string): string => {
  const text = cleanAnswer(raw);
  if (!text) return "";

  // Split into trimmed non-empty lines
  const lines = text.split(/\r?\n/).map((l) => l.trim()).filter(Boolean);

  // If it looks like a list (≥3 lines), bulletize each line.
  if (lines.length >= 3) {
    const bullets = lines.map((l) => {
      let x = l
        // drop existing bullets, dashes, asterisks, or numbered prefixes
        .replace(/^[\u2022•\-\*\u00B7]+[\s]?/, "")
        .replace(/^\d+[\.\)]\s*/, "")
        .trim();
      if (x) x = x.charAt(0).toUpperCase() + x.slice(1);
      return `• ${x}`;
    });
    return bullets.join("\n");
  }

  // Otherwise, sentence-case the first letter and return.
  return text.replace(/(^|\n)\s*([a-z])/g, (_, p1, p2) => p1 + p2.toUpperCase());
};

export default function AskDocuMind() {
  const BACKEND_BASE = useMemo(detectBackendBase, []);
  const [dept, setDept] = useState<string>("");
  const [lang, setLang] = useState<string>("en");
  const [mode, setMode] = useState<"llm" | "local">("llm");
  const [q, setQ] = useState("");
  const [busy, setBusy] = useState(false);
  const [showSettings, setShowSettings] = useState(false);

  const [settings, updateSettings, resetSettings] = useSettings();
  const [messages, setMessages] = useState<
    { who: "user" | "bot"; text: string; cites?: Citation[]; stats?: Meta }[]
  >([]);
  const [lastQuery, setLastQuery] = useState<string>("");
  const [openSources, setOpenSources] = useState<Record<number, boolean>>({});

  const chatRef = useRef<HTMLDivElement>(null);
  const push = (m: { who: "user" | "bot"; text: string; cites?: Citation[]; stats?: Meta }) => {
    setMessages((p) => [...p, m]);
    requestAnimationFrame(() =>
      chatRef.current?.scrollTo({ top: chatRef.current.scrollHeight, behavior: "smooth" })
    );
  };

  const runAsk = async (text: string) => {
    const isLocal = mode === "local";
    const url = `${BACKEND_BASE}/api/ask/answer${isLocal ? "?mode=local" : ""}`;
    const body: any = {
      q: text,
      lang,
      dept: dept || null,
      topk: settings.top_k_chunks,       // retrieval
      evidence_k: settings.evidence_k,   // forwarded passages
      percent_cap: settings.percent_cap, // trim evidence text
      max_tokens: settings.max_tokens,   // LLM output length
      temperature: settings.temperature,
      top_p: settings.top_p,
      presence_penalty: settings.presence_penalty,
      frequency_penalty: settings.frequency_penalty,
      timeout_ms: settings.timeout_ms,
      mode, // hint
    };

    const t0 = performance.now();
    const r = await fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Ask-Mode": mode,
      },
      body: JSON.stringify(body),
    });
    const data = (await r.json()) as AskResponse;
    const dt = Math.round(performance.now() - t0);

    if (!data || !("answer" in data)) {
      push({ who: "bot", text: `Network error contacting backend. (${dt} ms)` });
      return;
    }

    const cleaned = cleanAnswer(data.answer);
    const isLlm = (data.meta?.source || "").toLowerCase() === "llm";
    const pretty = isLlm ? cleaned : prettifyLocal(cleaned);

    push({
      who: "bot",
      text: pretty,
      cites: data.citations || [],
      stats: data.meta,
    });
  };

  const ask = async (override?: string) => {
    const text = (override ?? q).trim();
    if (!text) return;
    setLastQuery(text);
    push({ who: "user", text });
    if (!override) setQ("");
    setBusy(true);
    try {
      await runAsk(text);
    } finally {
      setBusy(false);
    }
  };

  const refresh = () => {
    if (lastQuery) ask(lastQuery);
  };
  const clearChat = () => {
    setMessages([]);
    setLastQuery("");
    setOpenSources({});
  };

  const SettingsModal = () => {
    const [tmp, setTmp] = useState<LlmSettings>(settings);
    const setNum = (key: keyof LlmSettings) => (e: React.ChangeEvent<HTMLInputElement>) =>
      setTmp({ ...tmp, [key]: Number(e.target.value) });
    const preset = (ms: number) => setTmp({ ...tmp, timeout_ms: ms });

    return (
      <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60">
        <div className="w-[760px] max-w-[92vw] rounded-2xl bg-slate-900 border border-white/10 p-5 shadow-2xl">
          <div className="text-xl font-semibold mb-4">Global LLM Settings</div>

          <div className="grid grid-cols-2 md:grid-cols-3 gap-3">
            <label className="text-sm">
              <div className="mb-1 text-slate-300">Max tokens</div>
              <input
                type="number"
                className="w-full px-3 py-2 rounded-lg bg-slate-950/70 border border-white/10"
                value={tmp.max_tokens}
                onChange={setNum("max_tokens")}
              />
            </label>
            <label className="text-sm">
              <div className="mb-1 text-slate-300">Temperature</div>
              <input
                type="number"
                step="0.05"
                className="w-full px-3 py-2 rounded-lg bg-slate-950/70 border border-white/10"
                value={tmp.temperature}
                onChange={setNum("temperature")}
              />
            </label>
            <label className="text-sm">
              <div className="mb-1 text-slate-300">Top-p</div>
              <input
                type="number"
                step="0.05"
                className="w-full px-3 py-2 rounded-lg bg-slate-950/70 border border-white/10"
                value={tmp.top_p}
                onChange={setNum("top_p")}
              />
            </label>

            <label className="text-sm">
              <div className="mb-1 text-slate-300">Presence penalty</div>
              <input
                type="number"
                step="0.1"
                className="w-full px-3 py-2 rounded-lg bg-slate-950/70 border border-white/10"
                value={tmp.presence_penalty}
                onChange={setNum("presence_penalty")}
              />
            </label>
            <label className="text-sm">
              <div className="mb-1 text-slate-300">Frequency penalty</div>
              <input
                type="number"
                step="0.1"
                className="w-full px-3 py-2 rounded-lg bg-slate-950/70 border border-white/10"
                value={tmp.frequency_penalty}
                onChange={setNum("frequency_penalty")}
              />
            </label>

            <label className="text-sm">
              <div className="mb-1 text-slate-300">Percent cap (%)</div>
              <input
                type="number"
                className="w-full px-3 py-2 rounded-lg bg-slate-950/70 border border-white/10"
                value={tmp.percent_cap}
                onChange={setNum("percent_cap")}
              />
            </label>
            <label className="text-sm">
              <div className="mb-1 text-slate-300">Top-K chunks (retrieve)</div>
              <input
                type="number"
                className="w-full px-3 py-2 rounded-lg bg-slate-950/70 border border-white/10"
                value={tmp.top_k_chunks}
                onChange={setNum("top_k_chunks")}
              />
            </label>
            <label className="text-sm">
              <div className="mb-1 text-slate-300">Evidence chunks (forward)</div>
              <input
                type="number"
                className="w-full px-3 py-2 rounded-lg bg-slate-950/70 border border-white/10"
                value={tmp.evidence_k}
                onChange={setNum("evidence_k")}
              />
            </label>
            <label className="text-sm">
              <div className="mb-1 text-slate-300">Timeout (ms)</div>
              <input
                type="number"
                className="w-full px-3 py-2 rounded-lg bg-slate-950/70 border border-white/10"
                value={tmp.timeout_ms}
                onChange={setNum("timeout_ms")}
              />
            </label>
          </div>

          <div className="flex items-center gap-2 mt-3">
            <div className="text-xs text-slate-400 mr-auto">Preset:</div>
            <button
              className="px-2 py-1 text-xs rounded-lg bg-white/5 border border-white/10"
              onClick={() => preset(30000)}
            >
              30s
            </button>
            <button
              className="px-2 py-1 text-xs rounded-lg bg-white/5 border border-white/10"
              onClick={() => preset(60000)}
            >
              60s
            </button>
            <button
              className="px-2 py-1 text-xs rounded-lg bg-white/5 border border-white/10"
              onClick={() => preset(120000)}
            >
              120s
            </button>
            <button
              className="px-2 py-1 text-xs rounded-lg bg-white/5 border border-white/10"
              onClick={() => preset(300000)}
            >
              ∞
            </button>
          </div>

          <div className="flex justify-end gap-2 mt-5">
            <button
              className="px-4 py-2 rounded-lg bg-white/5 border border-white/10"
              onClick={() => setShowSettings(false)}
            >
              Cancel
            </button>
            <button
              className="px-4 py-2 rounded-lg bg-white/5 border border-white/10"
              onClick={resetSettings}
            >
              Reset
            </button>
            <button
              className="px-4 py-2 rounded-lg bg-violet-600 hover:bg-violet-500"
              onClick={() => {
                updateSettings(tmp);
                setShowSettings(false);
              }}
            >
              Apply
            </button>
          </div>
        </div>
      </div>
    );
  };

  return (
    <div className="min-h-screen bg-gradient-to-b from-slate-900 to-slate-950 text-slate-200">
      <div className="max-w-5xl mx-auto p-6">
        {/* Header */}
        <div className="flex items-center gap-3 mb-4">
          <div className="text-2xl font-semibold">Ask DocuMind</div>
          <span
            className={cls(
              "px-2 py-1 rounded-full text-xs",
              busy ? "bg-amber-500/20 text-amber-300" : "bg-emerald-500/20 text-emerald-300"
            )}
          >
            {busy ? "thinking…" : "ready"}
          </span>
          <ModeBadge value={mode} />
          {/* LED mirroring selected engine */}
          <StatusLED
            mode={mode === "llm" ? "llm" : "local"}
            label={mode === "llm" ? "LLM" : "Local"}
            title="Current engine"
          />
          <div className="ml-auto text-xs text-slate-400">
            Backend: <code>{BACKEND_BASE}</code>
          </div>
        </div>

        {/* Controls */}
        <div className="rounded-2xl border border-white/10 bg-slate-900/60 shadow-xl">
          <div className="grid grid-cols-1 md:grid-cols-5 gap-3 p-4">
            <input
              className="md:col-span-2 px-4 py-3 rounded-xl border border-white/10 bg-slate-950/70 outline-none focus:ring-1 focus:ring-violet-400"
              placeholder="Type your question…"
              value={q}
              onChange={(e) => setQ(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") ask();
              }}
            />
            <select
              className="px-3 py-3 rounded-xl bg-slate-950/70 border border-white/10"
              value={dept}
              onChange={(e) => setDept(e.target.value)}
            >
              <option value="">(any department)</option>
              <option value="animal">animal</option>
              <option value="labour">labour</option>
              <option value="education">education</option>
            </select>
            <select
              className="px-3 py-3 rounded-xl bg-slate-950/70 border border-white/10"
              value={lang}
              onChange={(e) => setLang(e.target.value)}
            >
              <option value="en">English</option>
              <option value="hi">Hindi</option>
            </select>
            <select
              className="px-3 py-3 rounded-xl bg-slate-950/70 border border-white/10"
              value={mode}
              onChange={(e) => setMode(e.target.value as any)}
            >
              <option value="llm">LLM (longer)</option>
              <option value="local">Local (fast)</option>
            </select>
          </div>

          <div className="px-4 pb-4 flex items-center justify-between">
            <div className="text-xs text-slate-400">
              <b>K:</b> {settings.top_k_chunks} · <b>Evidence:</b> {settings.evidence_k} ·{" "}
              <b>Percent cap:</b> {settings.percent_cap}% · <b>Max tokens:</b> {settings.max_tokens}
            </div>
            <div className="flex items-center gap-2">
              <button
                onClick={() => setShowSettings(true)}
                className="px-3 py-1.5 rounded-lg bg-white/5 border border-white/10 hover:bg-white/10 text-sm"
              >
                LLM Settings
              </button>
              <button
                onClick={() => ask()}
                className="px-3 py-1.5 rounded-lg bg-violet-600 hover:bg-violet-500 text-sm"
                disabled={busy}
              >
                Ask
              </button>
              <button
                onClick={refresh}
                className="px-3 py-1.5 rounded-lg bg-white/5 border border-white/10 hover:bg-white/10 text-sm"
                disabled={busy || !lastQuery}
                title="Run the last question again"
              >
                Refresh
              </button>
              <button
                onClick={clearChat}
                className="px-3 py-1.5 rounded-lg bg-white/5 border border-white/10 hover:bg-white/10 text-sm"
                disabled={busy}
                title="Clear messages"
              >
                Clear
              </button>
            </div>
          </div>
        </div>

        {/* Chat */}
        <div
          ref={chatRef}
          className="mt-4 rounded-2xl border border-white/10 bg-slate-900/60 shadow-xl p-4 h-[56vh] overflow-auto"
        >
          {messages.length === 0 ? (
            <div className="text-slate-400">
              Ask anything about the documents. Try: <code>benefits subsidy</code> (pick dept if needed)
            </div>
          ) : (
            <div className="space-y-3">
              {messages.map((m, i) => (
                <div
                  key={i}
                  className={cls(
                    "max-w-[85%] rounded-xl px-3 py-2",
                    m.who === "user" ? "bg-slate-800 ml-auto" : "bg-slate-950/70 border border-white/10"
                  )}
                >
                  <div className="whitespace-pre-wrap leading-relaxed">{m.text}</div>

                  {m.who === "bot" && (
                    <>
                      {/* Source LED */}
                      <div className="mt-1 mb-1">
                        <StatusLED
                          mode={
                            (m.stats?.source as LedMode) ||
                            "local"
                          }
                          label={
                            m.stats?.source === "llm"
                              ? "LLM"
                              : m.stats?.source === "fallback"
                              ? "Local (fallback)"
                              : "Local"
                          }
                          title="Answer source"
                        />
                      </div>

                      {/* Stats row (existing) */}
                      <div className="text-[11px] text-slate-300/90 flex gap-2 flex-wrap">
                        <StatChip label="source" value={m.stats?.source} />
                        <StatChip
                          label="took"
                          value={m.stats?.took_ms ? `${m.stats.took_ms} ms` : undefined}
                        />
                        <StatChip
                          label="LLM"
                          value={m.stats?.llm_elapsed_ms ? `${m.stats.llm_elapsed_ms} ms` : undefined}
                        />
                        <StatChip label="tokens" value={m.stats?.used_tokens} />
                        <StatChip label="k" value={m.stats?.k} />
                        <StatChip label="evidence_k" value={m.stats?.evidence_k} />
                        <StatChip label="passages" value={m.stats?.passages} />
                        <StatChip label="max_tokens" value={m.stats?.max_tokens} />
                      </div>

                      {/* Collapsible sources */}
                      {m.cites && m.cites.length > 0 && (
                        <div className="mt-2">
                          <button
                            className="px-2 py-1 text-xs rounded-md bg-white/5 border border-white/10 hover:bg-white/10"
                            onClick={() =>
                              setOpenSources((prev) => ({ ...prev, [i]: !prev[i] }))
                            }
                            aria-expanded={!!openSources[i]}
                          >
                            {openSources[i] ? "▲ Sources" : "▼ Sources"}
                          </button>

                          {openSources[i] && (
                            <div className="mt-2 flex items-center gap-2 flex-wrap">
                              <button
                                className="px-2 py-1 text-xs rounded-md bg-white/5 border border-white/10 hover:bg-white/10"
                                onClick={() =>
                                  m.cites!.forEach((c) =>
                                    window.open(pdfUrl(c.doc_id, lang, dept), "_blank")
                                  )
                                }
                              >
                                Open all
                              </button>
                              {m.cites!.map((c, j) => {
                                const label = `${c.doc_id}`;
                                const href = pdfUrl(c.doc_id, lang, dept);
                                return (
                                  <span key={j} className="flex items-center gap-1">
                                    <a
                                      href={href}
                                      target="_blank"
                                      rel="noreferrer"
                                      className="px-2 py-1 text-xs rounded-full bg-violet-900/30 ring-1 ring-violet-400/30 hover:bg-violet-900/50"
                                    >
                                      {label}
                                    </a>
                                    <a
                                      href={href}
                                      download
                                      className="px-2 py-1 text-xs rounded-md bg-white/5 border border-white/10 hover:bg-white/10"
                                    >
                                      Download
                                    </a>
                                  </span>
                                );
                              })}
                            </div>
                          )}
                        </div>
                      )}
                    </>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>

        <div className="mt-2 text-xs text-slate-400">
          Tip: set <code>VITE_DM_BACKEND</code> in your UI env to force a backend (e.g.,{" "}
          <code>http://34.131.12.84:9000</code>).
        </div>
      </div>

      {showSettings && <SettingsModal />}
    </div>
  );
}
