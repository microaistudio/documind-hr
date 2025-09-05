// Path: ui/src/components/AnswerBody.tsx
// Purpose: Shared presentation for grounded answers + fallbacks across LLM modal and Ask page.
// Version: 1.2.0 — sticky chips bar support + safe z-index for popovers

import React, { useMemo, useState } from "react";

export function Badge({ children, tone }: { children: React.ReactNode; tone: "violet" | "purple" | "slate" }) {
  const cls =
    tone === "violet"
      ? "bg-violet-100 text-violet-900 border-violet-200"
      : tone === "purple"
      ? "bg-purple-100 text-purple-900 border-purple-200"
      : "bg-slate-100 text-slate-700 border-slate-200";
  return <span className={`px-2 py-0.5 rounded-full border text-xs ${cls}`}>{children}</span>;
}

export type SourceItem = { doc_id: string; chunk?: number | string; text?: string };

export type AnswerBodyProps = {
  title?: string;
  status?: "" | "ok" | "fallback";      // set "ok" for LLM; "fallback" for local
  sourceLabel?: string;                  // e.g., "ocr", "passages", "local", "grounded"
  tookMs?: number | null;
  note?: string;
  summary: string;
  kUsed?: number | null;
  percentCap?: number | null;
  tokensIn?: number | null;
  tokensOut?: number | null;
  overrides?: any;
  sources?: SourceItem[];
  chipsBar?: React.ReactNode;            // NEW: sticky chips/tabs row slot
};

export default function AnswerBody(props: AnswerBodyProps) {
  const {
    title,
    status,
    sourceLabel,
    tookMs,
    note,
    summary,
    kUsed,
    percentCap,
    tokensIn,
    tokensOut,
    overrides,
    sources = [],
    chipsBar,
  } = props;

  const [showSources, setShowSources] = useState(false);

  const tone: "violet" | "purple" | "slate" =
    status === "ok" ? "violet" : status === "fallback" ? "purple" : "slate";

  const meta = useMemo(() => {
    const bits: string[] = [];
    if (typeof tookMs === "number") bits.push(`Took ${tookMs} ms`);
    if (sourceLabel) bits.push(sourceLabel);
    if (typeof tokensIn === "number" || typeof tokensOut === "number") {
      bits.push(`tok in:${tokensIn ?? "—"} out:${tokensOut ?? "—"}`);
    }
    if (typeof kUsed === "number") bits.push(`k used: ${kUsed}`);
    if (typeof percentCap === "number") bits.push(`cap: ${percentCap}%`);
    return bits.join("  ·  ");
  }, [tookMs, sourceLabel, tokensIn, tokensOut, kUsed, percentCap]);

  return (
    <div className="relative">
      {/* Sticky chips header if provided */}
      {chipsBar && (
        <div className="sticky top-0 z-30 -mx-4 px-4 py-2 bg-white/95 backdrop-blur border-b">
          {chipsBar}
        </div>
      )}

      {/* Title row & meta */}
      {(title || status || sourceLabel || typeof tookMs === "number") && (
        <div className="mb-2 flex flex-wrap items-center gap-2 text-sm">
          {title && <div className="font-semibold">{title}</div>}
          {status && <Badge tone={tone}>{status === "ok" ? "LLM" : status === "fallback" ? "Local" : "—"}</Badge>}
          {meta && <span className="text-slate-500">{meta}</span>}
        </div>
      )}

      {/* Note */}
      {note && <div className="mb-2 text-xs text-slate-500">{note}</div>}

      {/* Summary */}
      <pre className="text-sm whitespace-pre-wrap border rounded p-3 bg-gray-50 max-h-[50vh] overflow-auto">
{summary || "No summary yet."}
      </pre>

      {/* Meta tail */}
      <div className="mt-2 text-xs text-slate-600 space-x-3">
        {typeof kUsed === "number" && <span>k used: {kUsed}</span>}
        {typeof percentCap === "number" && percentCap > 0 && <span>cap: {percentCap}%</span>}
        {typeof tokensIn === "number" && <span>tokens in: {tokensIn}</span>}
        {typeof tokensOut === "number" && <span>tokens out: {tokensOut}</span>}
        {overrides && (
          <span title={typeof overrides === "string" ? overrides : JSON.stringify(overrides)}>overrides ✓</span>
        )}
      </div>

      {/* Sources */}
      <div className="mt-3">
        <button onClick={() => setShowSources(v => !v)} className="text-sm text-violet-700 hover:underline">
          {showSources ? "Hide Sources ▲" : "Show Sources ▼"}
        </button>
        {showSources && (
          <div className="mt-2 space-y-2">
            {sources.map((s, i) => (
              <div key={i} className="text-xs border rounded p-2 bg-white relative z-[1200]">
                <div className="font-mono text-[11px] text-slate-500">
                  [{s.doc_id}{s.chunk !== undefined ? `#${s.chunk}` : ""}]
                </div>
                {s.text && <div className="mt-1 text-slate-800">{s.text}</div>}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
