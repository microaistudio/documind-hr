import { useState } from "react";
import { llmSave } from "../lib/api";

export default function RawSummarizePage() {
  const [text, setText] = useState("");
  const [style, setStyle] = useState("bullet");
  const [resp, setResp] = useState<any>(null);
  const [loading, setLoading] = useState(false);

  async function go() {
    setLoading(true);
    try {
      const r = await llmRawSave(text, "ui-raw", style);
      setResp(r);
    } catch (e: any) {
      setResp({ error: e?.message || "failed" });
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="p-4 max-w-3xl mx-auto">
      <h1 className="text-xl font-semibold mb-3">LLM Summarize (Raw Text)</h1>
      <div className="flex gap-2 mb-2">
        <select className="border rounded px-2 py-1 text-sm" value={style} onChange={e=>setStyle(e.target.value)}>
          <option value="bullet">Bullet</option>
          <option value="short">Short</option>
          <option value="detailed">Detailed</option>
        </select>
        <button onClick={go} disabled={loading || !text.trim()} className="px-3 py-1.5 rounded bg-black text-white text-sm disabled:opacity-40">
          {loading ? "Summarizing…" : "Summarize & Save"}
        </button>
      </div>
      <textarea
        className="w-full border rounded p-2 h-40"
        placeholder="Paste any text…"
        value={text}
        onChange={e=>setText(e.target.value)}
      />
      {resp && (
        <pre className="mt-3 p-3 bg-gray-50 border rounded whitespace-pre-wrap text-sm">
{JSON.stringify(resp, null, 2)}
        </pre>
      )}
    </div>
  );
}
