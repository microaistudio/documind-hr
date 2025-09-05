// Path: ui/src/components/SemSummaryModal.tsx
// Purpose: “Semantic summary” modal with LIVE Apply (re-runs preview in-place).
// Version: 1.0.0

import React, { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

type Props = {
  docId: string;
  open: boolean;
  onClose: () => void;

  /** Optional: initial values when opening */
  defaultK?: number;        // e.g. 5
  defaultProbes?: number;   // e.g. 11

  /** Optional: override endpoints if your routes differ */
  previewPath?: string;     // default: `/api/docs/:docId/semsum/preview`
  savePath?: string;        // default: `/api/docs/:docId/semsum/save`
};

export default function SemSummaryModal({
  docId,
  open,
  onClose,
  defaultK = 5,
  defaultProbes = 11,
  previewPath,
  savePath,
}: Props) {
  const PREVIEW_URL = previewPath ?? `/api/docs/${docId}/semsum/preview`;
  const SAVE_URL = savePath ?? `/api/docs/${docId}/semsum/save`;

  const [k, setK] = useState<number>(defaultK);
  const [probes, setProbes] = useState<number>(defaultProbes);

  const [summary, setSummary] = useState<string>("");
  const [note, setNote] = useState<string>("");
  const [tookMs, setTookMs] = useState<number | null>(null);

  const [loading, setLoading] = useState(false);
  const abortRef = useRef<AbortController | null>(null);

  /** Core: (re)run preview with current K/Probes */
  async function preview() {
    setLoading(true);
    setNote("");

    const ctrl = new AbortController();
    abortRef.current = ctrl;

    try {
      const res = await fetch(PREVIEW_URL, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ k, probes }),
        signal: ctrl.signal,
      });

      setTookMs(Number(res.headers.get("X-Response-Time-ms") || "0"));

      const ctype = (res.headers.get("content-type") || "").toLowerCase();
      let data: any = null;
      if (ctype.includes("application/json")) {
        data = await res.json();
      } else {
        const txt = await res.text();
        try { data = JSON.parse(txt); } catch { data = { summary: txt }; }
      }

      setSummary(data?.summary ?? "");
      setNote(data?.note ?? "");
    } catch (e: any) {
      if (e?.name !== "AbortError") {
        setSummary("");
        setNote(e?.message || "Preview failed");
      }
    } finally {
      setLoading(false);
      abortRef.current = null;
    }
  }

  /** Load once every time the modal opens */
  useEffect(() => {
    if (!open) return;
    // reset state to incoming defaults each time it opens
    setK(defaultK);
    setProbes(defaultProbes);
    setSummary("");
    setNote("");
    setTookMs(null);
    preview();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, docId]);

  function onKeyDown(e: React.KeyboardEvent) {
    if (e.key === "Enter") {
      e.preventDefault();
      preview();
    }
  }

  async function onSave() {
    try {
      setLoading(true);
      const r = await fetch(SAVE_URL, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ k, probes, summary }),
      });
      const d = await r.json().catch(() => ({}));
      setNote(d?.ok ? "Saved ✓" : "Save failed");
    } finally {
      setLoading(false);
    }
  }

  if (!open) return null;

  /** Portal so this modal always floats above parent dialogs */
  return createPortal(
    <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-[1200]" onClick={onClose}>
      <div
        className="bg-white w-[760px] max-h-[85vh] overflow-y-auto rounded-xl shadow-lg p-4 relative"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between mb-2 sticky top-0 bg-white z-10 -mx-4 px-4 py-2 border-b">
          <h3 className="text-lg font-semibold">Semantic summary — {docId}</h3>
          <button className="text-gray-500 hover:text-black" onClick={onClose}>✕</button>
        </div>

        {/* Badges row */}
        <div className="flex flex-wrap items-center gap-2 text-sm mb-2">
          <span className="px-2 py-0.5 rounded-full border text-xs bg-slate-100 text-slate-800">source: generated</span>
          <span className="px-2 py-0.5 rounded-full border text-xs bg-slate-100 text-slate-800">k: {k}</span>
          <span className="px-2 py-0.5 rounded-full border text-xs bg-slate-100 text-slate-800">probes: {probes}</span>
          {typeof tookMs === "number" && <span className="text-slate-500">Took {tookMs} ms</span>}
          {note && <span className="text-slate-500">· {note}</span>}
        </div>

        {/* Summary text area */}
        <textarea
          className="w-full h-[48vh] border rounded p-3 font-mono text-[13px] leading-6"
          value={summary}
          onChange={(e) => setSummary(e.target.value)}
        />

        {/* Controls */}
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <label className="flex items-center gap-1 text-sm">
            K
            <select
              className="border rounded px-2 py-1"
              value={k}
              onChange={(e) => setK(Number(e.target.value))}
              onKeyDown={onKeyDown}
            >
              {[1, 3, 5, 10, 20, 50, 100].map((n) => (
                <option key={n} value={n}>{n}</option>
              ))}
            </select>
          </label>

          <label className="flex items-center gap-1 text-sm">
            Probes
            <input
              type="number"
              className="border rounded px-2 py-1 w-20"
              value={probes}
              onChange={(e) => setProbes(Number(e.target.value))}
              onKeyDown={onKeyDown}
              min={1}
              max={100}
            />
          </label>

          <button
            onClick={preview}
            disabled={loading}
            className="ml-1 px-3 py-1.5 rounded border text-sm hover:bg-gray-50 disabled:opacity-40"
            title="Re-run with the current K and probes (stays open)"
          >
            {loading ? "Applying…" : "Apply"}
          </button>

          <button
            onClick={onSave}
            disabled={loading || !summary}
            className="px-3 py-1.5 rounded bg-emerald-600 text-white text-sm font-medium hover:bg-emerald-700 disabled:opacity-40"
          >
            {loading ? "Saving…" : "Save to DB"}
          </button>

          <div className="grow" />
          <button onClick={onClose} className="px-3 py-1.5 rounded border text-sm">Close</button>
        </div>
      </div>
    </div>,
    document.body
  );
}
