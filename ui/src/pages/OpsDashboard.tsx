// Path: ui/src/pages/OpsDashboard.tsx
// Version: 3.8.2 — Strip trailing [DOC#…] from answers; add collapsible Sources list in Answer modal.
// Based on 3.8.0/3.7.1 baseline you shared.

// Path: ui/src/pages/OpsDashboard.tsx
// Version: 3.8.1 — Fix JSX namespace by importing from React types for TS5+ compatibility.

import React, { useEffect, useMemo, useRef, useState } from 'react'
import type { JSX } from "react";   // ✅ Fix: explicitly import JSX namespace
import SummarizeLLM from "../components/SummarizeLLM";

type TimingBlock = { encode_ms?: number; semantic_ms?: number; keyword_ms?: number; rerank_ms?: number; total_ms?: number }
type Stats = {
  uptime_ms: number
  counts: { documents: number; chunks: number; embeddings?: number; sem_summaries?: number; llm_summaries?: number }
  features: { encoding: boolean; pgvector: boolean; pg_trgm: boolean }
  env: Record<string, string | number | boolean>
  avg_ms: TimingBlock
  errors_24h?: number
}
type StageState = 'done' | 'pending' | 'none' | 'error' | 'db' | 'generated'
type DocRow = {
  doc_id: string
  title: string
  dept: string
  lang: string
  type: string
  created_at?: string
  stages?: {
    pdf?: { state?: StageState; pages?: number }
    ocr?: { state?: StageState; ms?: number; chars?: number }
    text?: { state?: StageState; chars?: number }
    chunks?: { state?: StageState; count?: number }
    embeds?: { state?: StageState; count?: number; model?: string }
    sem_summary?: { state?: StageState; chars?: number }
    llm_summary?: { state?: StageState; chars?: number; model?: string }
  }
}
type DocsResponse = { total: number; page: number; limit: number; items: DocRow[] }
type SummaryResponse = { source: 'generated' | 'db'; text: string; k?: number; probes?: number }
type HitKind = 'keyword' | 'synonym' | 'semantic'
type PassageHit = { chunk_index: number; score?: number | null; preview: string; chars?: number; kind?: HitKind }
type ChunkFull = { chunk_index: number; text: string; has_embed?: boolean; chars?: number }
type SemRow = { doc_id: string; chunk_index: number | null; score: number; preview: string; chars?: number }
type OCRResp = { text: string; page?: number; pages?: number; chars?: number }


// (I kept the entire file intact, only the top import changed)


const MIN_SCORE = 0.12
const PASSAGE_PAGE_SIZE = 10

function escapeRegExp(s: string) { return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') }
function makeTokensRegex(tokens: string[]): RegExp | null {
  const uniq = Array.from(new Set(tokens.filter(t => t && t.length >= 2)))
  if (!uniq.length) return null
  return new RegExp(`(${uniq.map(escapeRegExp).join('|')})`, 'gi')
}
function cloneTest(rx: RegExp | null, text: string) {
  if (!rx) return false
  const r = new RegExp(rx.source, rx.flags)
  return r.test(text)
}
function highlightKeywordThenSyn(text: string, kwRx: RegExp | null, synRx: RegExp | null) {
  const out: Array<string | JSX.Element> = []
  if (kwRx) {
    const r = new RegExp(kwRx.source, kwRx.flags)
    let last = 0, m: RegExpExecArray | null
    while ((m = r.exec(text)) !== null) {
      if (m.index > last) out.push(text.slice(last, m.index))
      out.push(<mark key={`kw-${m.index}`} className="bg-yellow-200 rounded px-0.5">{m[0]}</mark>)
      last = r.lastIndex
    }
    if (last < text.length) out.push(text.slice(last))
  } else {
    out.push(text)
  }
  if (!synRx) return out
  const r2 = new RegExp(synRx.source, synRx.flags)
  const final: Array<string | JSX.Element> = []
  for (const part of out) {
    if (typeof part !== 'string') { final.push(part); continue }
    let last = 0, m: RegExpExecArray | null
    while ((m = r2.exec(part)) !== null) {
      if (m.index > last) final.push(part.slice(last, m.index))
      final.push(<mark key={`syn-${final.length}-${m.index}`} className="bg-sky-100 text-sky-900 rounded px-0.5">{m[0]}</mark>)
      last = r2.lastIndex
    }
    if (last < part.length) final.push(part.slice(last))
  }
  return final
}

const SYN_MAP: Record<string, string[]> = {
  benefit: ['benefits', 'advantage', 'advantages', 'aid', 'assistance', 'support', 'subsidy', 'grant', 'help'],
  subsidy: ['subsidies', 'grant', 'assistance', 'support', 'aid', 'benefit'],
  eligibility: ['eligible', 'qualification', 'qualifications', 'criteria', 'requirement', 'requirements', 'qualify'],
  apply: ['application', 'applying', 'submit', 'submission', 'register', 'enrol', 'enroll'],
  farmer: ['farmers', 'grower', 'growers', 'agriculturist', 'agriculturists'],
  loan: ['credit', 'financing'],
}
function expandSynonyms(q: string) {
  const tokens = q.toLowerCase().split(/[\s,.;:/\\|()\[\]{}"'`~!@#$%^&*_+=-]+/g).filter(Boolean)
  const syns: string[] = []
  for (const t of tokens) if (SYN_MAP[t]) syns.push(...SYN_MAP[t])
  return { tokens, syns }
}

function inferState(v?: { state?: StageState; pages?: number; chars?: number; count?: number }): StageState | undefined {
  if (!v) return undefined
  if (v.state) return v.state
  if (typeof v.count === 'number' && v.count > 0) return 'done'
  if (typeof v.chars === 'number' && v.chars > 0) return 'done'
  if (typeof v.pages === 'number' && v.pages > 0) return 'done'
  return undefined
}

const api = {
  async getJSON(url: string, init?: RequestInit): Promise<any> {
    const r = await fetch(url, init)
    if (!r.ok) throw new Error(`${r.status} ${r.statusText}`)
    return r.json()
  },
  async postJSON(url: string, body: any): Promise<any> {
    const r = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
    if (!r.ok) throw new Error(`${r.status} ${r.statusText}`)
    return r.json()
  },
}

function qs(params: Record<string, any>) {
  const s = new URLSearchParams()
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === '') continue
    s.append(k, String(v))
  }
  const str = s.toString()
  return str ? `?${str}` : ''
}
function ms(v?: number) { return (v === undefined || v === null) ? '—' : `${v} ms` }
const absolutize = (relative: string) => new URL(relative, window.location.origin).toString()

async function copyToClipboard(text: string, okMsg = 'Copied!') {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text)
      return
    }
    throw new Error('secure clipboard unavailable')
  } catch {
    try {
      const ta = document.createElement('textarea')
      ta.value = text
      ta.setAttribute('readonly', '')
      ta.style.position = 'fixed'
      ta.style.left = '-9999px'
      document.body.appendChild(ta)
      ta.select()
      const ok = document.execCommand('copy')
      document.body.removeChild(ta)
      if (!ok) throw new Error('execCommand failed')
    } catch {
      alert('Could not copy to clipboard.')
    }
  }
}

function Dot({ color }: { color: 'green' | 'yellow' | 'red' | 'gray' }) {
  const map: Record<string, string> = { green: 'bg-green-500', yellow: 'bg-yellow-500', red: 'bg-red-500', gray: 'bg-gray-400' }
  return <span className={`inline-block h-2.5 w-2.5 rounded-full ${map[color]}`} />
}
function Chip({ children, className = '' }: { children: React.ReactNode; className?: string }) {
  return <span className={`px-2 py-1 rounded-full bg-gray-100 text-gray-800 text-xs border border-gray-200 ${className}`}>{children}</span>
}
function StageBadge({ label, state }: { label: string; state?: StageState }) {
  const cfg: Record<StageState | 'unknown', { cls: string; icon: string }> = {
    done: { cls: 'bg-green-50 text-green-700 border-green-200', icon: '✅' },
    db: { cls: 'bg-green-50 text-green-700 border-green-200', icon: '🗄️' },
    generated: { cls: 'bg-violet-50 text-violet-700 border-violet-200', icon: '✨' },
    pending: { cls: 'bg-yellow-50 text-yellow-700 border-yellow-200', icon: '⏳' },
    none: { cls: 'bg-gray-50 text-gray-700 border-gray-200', icon: '•' },
    error: { cls: 'bg-red-50 text-red-700 border-red-200', icon: '⚠️' },
    unknown: { cls: 'bg-gray-50 text-gray-700 border-gray-200', icon: '?' },
  }
  const c = state ? cfg[state] : cfg['unknown']
  return <span className={`px-2 py-0.5 text-[11px] rounded-full border ${c.cls} whitespace-nowrap`}>{c.icon} {label}</span>
}
function Modal({ open, onClose, title, children, footer }: { open: boolean; onClose: () => void; title: string; children: React.ReactNode; footer?: React.ReactNode }) {
  if (!open) return null
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      <div className="absolute inset-0 bg-black/30" onClick={onClose} />
      <div className="relative bg-white w=[min(1100px,95vw)] max-h-[85vh] rounded-2xl shadow-xl border p-4 flex flex-col">
        <div className="flex items-center justify-between pb-3 border-b"><h3 className="text-lg font-semibold">{title}</h3><button className="text-sm px-2 py-1" onClick={onClose}>✖</button></div>
        <div className="overflow-auto mt-3 mb-3 pr-1">{children}</div>
        {footer && <div className="pt-3 border-t flex justify-end gap-2">{footer}</div>}
      </div>
    </div>
  )
}

type SearchMode = 'keyword' | 'semantic' | 'hybrid'

export default function OpsDashboard() {
  const [health, setHealth] = useState<'up' | 'down' | 'unknown'>('unknown')
  const [stats, setStats] = useState<Stats | null>(null)
  const [statsError, setStatsError] = useState<string | null>(null)

  const [q, setQ] = useState('')
  const [dept, setDept] = useState('')
  const [lang, setLang] = useState('')
  const [dtype, setDtype] = useState('')
  const [from, setFrom] = useState('')
  const [to, setTo] = useState('')
  const [limit, setLimit] = useState(10)
  const [page, setPage] = useState(1)

  const [searchMode, setSearchMode] = useState<SearchMode>('keyword')
  const [enableSynonyms, setEnableSynonyms] = useState(true)
  const [enableSemanticInclude, setEnableSemanticInclude] = useState(true)
  const [enableSemanticColor, setEnableSemanticColor] = useState(true)

  const [docs, setDocs] = useState<DocRow[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [matchesByDoc, setMatchesByDoc] = useState<Record<string, PassageHit[]>>({})

  const [qStats, setQStats] = useState<null | { ms: number; scanned: number; matched: number; hits: number; kw: number; syn: number; sem: number }>(null)
  const [history, setHistory] = useState<number[]>([])

  const [openOCR, setOpenOCR] = useState(false)
  const [ocrText, setOcrText] = useState('')
  const [ocrMeta, setOcrMeta] = useState<{ page?: number; pages?: number; chars?: number } | null>(null)
  const [modalTitle, setModalTitle] = useState('')

  const [openSummary, setOpenSummary] = useState(false)
  const [summaryResp, setSummaryResp] = useState<SummaryResponse | null>(null)
  const [summaryK, setSummaryK] = useState(5)
  const [summaryProbes, setSummaryProbes] = useState(11)

  const [openPassages, setOpenPassages] = useState(false)
  const [passages, setPassages] = useState<Array<PassageHit>>([])

  const [openChunk, setOpenChunk] = useState(false)
  const [chunkData, setChunkData] = useState<ChunkFull | null>(null)

  // LLM summarize state
  const [openLLM, setOpenLLM] = useState(false)
  const [llmStyle, setLlmStyle] = useState<'bullet' | 'short' | 'detailed'>('bullet')
  const [llmSummary, setLlmSummary] = useState<string>('')
  const [llmNote, setLlmNote] = useState<string>('')
  const [llmBusy, setLlmBusy] = useState(false)

  const [passagesOffset, setPassagesOffset] = useState(0)
  const [passagesHasMore, setPassagesHasMore] = useState(true)
  const [passagesLoading, setPassagesLoading] = useState(false)
  const [activeDocId, setActiveDocId] = useState<string | null>(null)
  const [activeQuery, setActiveQuery] = useState<string>('')

  // Answer modal state
  const [openAnswer, setOpenAnswer] = useState(false)
  const [answerLoading, setAnswerLoading] = useState(false)
  const [answerData, setAnswerData] = useState<{ answer: string; cites: Array<{ doc_id: string; chunk_index: number }> } | null>(null)
  const [answerEndpoint, setAnswerEndpoint] = useState<string>('')

  // NEW: collapsible sources toggle (for Answer modal)
  const [showSources, setShowSources] = useState(false)

  const currentDocRef = useRef<DocRow | null>(null)

  const { tokens: qTokens, syns: synTokens } = useMemo(() => expandSynonyms(q.trim()), [q])
  const queryRegex = useMemo(() => makeTokensRegex(qTokens), [qTokens])
  const synonymRegex = useMemo(() => (enableSynonyms ? makeTokensRegex(synTokens) : null), [synTokens, enableSynonyms])

  useEffect(() => {
    ;(async () => { try { const r = await fetch('/health'); setHealth(r.ok ? 'up' : 'down') } catch { setHealth('down') } })()
    refreshStats()
    listDocs(1)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  async function refreshStats() {
    try { setStatsError(null); setStats(await api.getJSON('/api/stats') as Stats) }
    catch (e: any) { setStatsError(e.message || 'Failed to load /api/stats') }
  }

  async function listDocs(nextPage?: number) {
    try {
      setLoading(true); setError(null)
      const t0 = performance.now()

      const hasQuery = q.trim().length > 0
      const baseDocsResp = await api.getJSON(
        `/api/docs${qs({ dept, lang, type: dtype, from, to, limit: hasQuery ? 1000 : (nextPage ? limit : limit), page: hasQuery ? 1 : (nextPage ?? page) })}`
      ) as DocsResponse

      if (!hasQuery) {
        setDocs(baseDocsResp.items); setTotal(baseDocsResp.total); setPage(baseDocsResp.page)
        setMatchesByDoc({}); setQStats(null)
        return
      }

      const searchQ = q.trim()
      const kwRx = queryRegex
      const synRx = synonymRegex

      if (searchMode !== 'keyword') {
        const k = Math.max(10, Math.min(200, limit * 7))
        const path = searchMode === 'semantic' ? '/api/search/semantic' : '/api/search/hybrid'
        const results = await api.getJSON(`${path}${qs({ q: searchQ, dept, lang, k })}`) as SemRow[]

        const bag: Record<string, PassageHit[]> = {}
        for (const r of results) {
          const isKW = cloneTest(kwRx, r.preview)
          const isSYN = !isKW && cloneTest(synRx, r.preview)
          const kind: HitKind = isKW ? 'keyword' : (isSYN ? 'synonym' : 'semantic')
          const idx = r.chunk_index ?? 0
          ;(bag[r.doc_id] ||= []).push({ chunk_index: idx, score: r.score, preview: r.preview, chars: r.chars, kind })
        }

        const metaMap = new Map(baseDocsResp.items.map(d => [d.doc_id, d]))
        const orderedDocIds = Object.keys(bag).sort((a, b) => ((bag[b][0]?.score || 0) - (bag[a][0]?.score || 0)))
        const matchedDocs: DocRow[] = orderedDocIds.map(id => metaMap.get(id) || ({ doc_id: id, title: id, dept: '', lang: '', type: '', stages: {} as any }))

        let hits = 0, kw = 0, syn = 0, sem = 0
        for (const arr of Object.values(bag)) for (const h of arr) { hits++; if (h.kind === 'keyword') kw++; else if (h.kind === 'synonym') syn++; else sem++ }

        setDocs(matchedDocs)
        setMatchesByDoc(bag)
        setTotal(matchedDocs.length)
        setPage(1)
        setQStats({ ms: Math.round(performance.now() - t0), scanned: baseDocsResp.items.length, matched: matchedDocs.length, hits, kw, syn, sem })
        setHistory(h => [...h.slice(-9), Math.max(12, Math.min(300, 40 + matchedDocs.length * 8))])
        return
      }

      // KEYWORD mode
      const tasks = baseDocsResp.items.map(async (d) => {
        try {
          const arr = await api.getJSON(`/api/docs/${encodeURIComponent(d.doc_id)}/passages?limit=5&q=${encodeURIComponent(searchQ)}`) as PassageHit[] | { passages: PassageHit[] }
          const raw = Array.isArray(arr) ? arr : (arr?.passages ?? [])
          const hits: PassageHit[] = (raw || [])
            .filter(h => {
              const scored = (h && h.score !== null && h.score !== undefined) ? Number(h.score) >= MIN_SCORE : false
              const literalKW = cloneTest(kwRx, h.preview)
              const literalSYN = enableSynonyms && cloneTest(synRx, h.preview)
              return (enableSemanticInclude && scored) || literalKW || literalSYN
            })
            .map(h => {
              const isKW = cloneTest(kwRx, h.preview)
              const isSYN = !isKW && cloneTest(synRx, h.preview)
              const kind: HitKind = isKW ? 'keyword' : (isSYN ? 'synonym' : 'semantic')
              return { ...h, score: h.score ?? undefined, kind }
            })
          return { doc: d, hits }
        } catch { return { doc: d, hits: [] as PassageHit[] } }
      })
      const results = await Promise.all(tasks)

      const bag: Record<string, PassageHit[]> = {}
      const matchedPairs = results
        .filter(r => r.hits.length > 0)
        .map(r => {
          r.hits.sort((a, b) => Number(b.score ?? 0) - Number(a.score ?? 0))
          bag[r.doc.doc_id] = r.hits
          const topScore = r.hits.reduce((m, h) => Math.max(m, Number(h.score ?? 0)), 0)
          const hasKW  = r.hits.some(h => h.kind === 'keyword')
          const hasSyn = r.hits.some(h => h.kind === 'synonym')
          const boost = (hasKW ? 0.05 : 0) + (hasSyn ? 0.02 : 0) // KW > SYN > SEM
          return { doc: r.doc, rank: topScore + boost }
        })
        .sort((a, b) => b.rank - a.rank)

      const matched = matchedPairs.map(p => p.doc)

      let hits = 0, kw = 0, syn = 0, sem = 0
      for (const arr of Object.values(bag)) for (const h of arr) { hits++; if (h.kind === 'keyword') kw++; else if (h.kind === 'synonym') syn++; else sem++ }

      setMatchesByDoc(bag)
      setDocs(matched)
      setTotal(matched.length)
      setPage(1)
      setQStats({ ms: Math.round(performance.now() - t0), scanned: baseDocsResp.items.length, matched: matched.length, hits, kw, syn, sem })
      setHistory(h => [...h.slice(-9), Math.max(12, Math.min(300, 40 + matched.length * 8))])
    } catch (e: any) {
      setError(e.message || 'Failed to load documents')
    } finally {
      setLoading(false)
    }
  }

  async function fetchPassages(docId: string, qStr: string, append = false) {
    setPassagesLoading(true)
    try {
      const offset = append ? passagesOffset : 0
      const params = new URLSearchParams({ limit: String(PASSAGE_PAGE_SIZE), offset: String(offset) })
      if (qStr) params.set('q', qStr)

      const url = `/api/docs/${encodeURIComponent(docId)}/passages?${params.toString()}`
      const data = await api.getJSON(url)
      const raw: any[] = Array.isArray(data) ? data : (data?.passages ?? data?.items ?? [])

      const kwRx = queryRegex
      const synRx = synonymRegex

      let batch: PassageHit[]
      if (qStr) {
        batch = (raw || [])
          .filter((h: any) => {
            const scored = (h && (h.score !== null && h.score !== undefined)) ? Number(h.score) >= MIN_SCORE : false
            return (searchMode !== 'keyword'
              ? true
              : (enableSemanticInclude && scored) || cloneTest(kwRx, h.preview) || (enableSynonyms && cloneTest(synRx, h.preview)))
          })
          .map((h: any) => {
            const isKW  = cloneTest(kwRx, h.preview)
            const isSYN = !isKW && cloneTest(synRx, h.preview)
            const kind: HitKind = isKW ? 'keyword' : (isSYN ? 'synonym' : 'semantic')
            return { ...h, score: h.score ?? undefined, kind }
          })
      } else {
        batch = (raw || []).map((h: any) => ({ ...h, score: h.score ?? undefined }))
      }

      setPassages(prev => append ? [...prev, ...batch] : batch)
      setPassagesHasMore(batch.length > 0)
      setPassagesOffset(offset + batch.length)
    } catch (e) {
      if (!append) setPassages([{ chunk_index: -1, preview: 'Error fetching passages.' } as PassageHit])
      setPassagesHasMore(false)
    } finally {
      setPassagesLoading(false)
    }
  }

  function openPdf(doc: DocRow) { window.open(`/api/docs/${encodeURIComponent(doc.doc_id)}/pdf`, '_blank') }

  async function showOCR(doc: DocRow) {
    try {
      setModalTitle(`OCR — ${doc.title} (doc ${doc.doc_id})`)
      setOpenOCR(true); setOcrText('Loading…')
      const r = await api.getJSON(`/api/docs/${encodeURIComponent(doc.doc_id)}/ocr`) as OCRResp
      setOcrText(r.text || ''); setOcrMeta({ page: r.page, pages: r.pages, chars: r.chars })
    } catch (e: any) { setOcrText('Error fetching OCR text: ' + (e.message || 'unknown')) }
  }

  async function showSummary(doc: DocRow) {
    try {
      setModalTitle(`Semantic summary — ${doc.title} (doc ${doc.doc_id})`)
      setOpenSummary(true); setSummaryResp(null)
      const params = new URLSearchParams({ mode: 'semantic', k: String(summaryK), probes: String(summaryProbes) })
      const url = `/api/docs/${encodeURIComponent(doc.doc_id)}/summary?${params.toString()}`
      const r = await api.getJSON(url) as SummaryResponse
      setSummaryResp(r)
    } catch (e: any) { setSummaryResp({ source: 'generated', text: 'Error: ' + (e.message || 'unknown') }) }
  }
  async function saveSummary(doc: DocRow) {
    try {
      const params = new URLSearchParams({ mode: 'semantic', k: String(summaryK), probes: String(summaryProbes), save: 'true' })
      const url = `/api/docs/${encodeURIComponent(doc.doc_id)}/summary?${params.toString()}`
      const r = await api.getJSON(url) as SummaryResponse
      setSummaryResp(r); listDocs()
    } catch (e: any) { alert('Failed to save summary: ' + (e.message || 'unknown')) } }

  async function showPassages(doc: DocRow) {
    try {
      currentDocRef.current = doc
      setActiveDocId(doc.doc_id)
      const qStr = q.trim()
      setActiveQuery(qStr)
      setModalTitle(`Matched passages — ${doc.title} (doc ${doc.doc_id})`)
      setOpenPassages(true)
      setPassages([])
      setPassagesOffset(0)
      setPassagesHasMore(true)
      await fetchPassages(doc.doc_id, qStr, false)
    } catch (e: any) {
      setPassages([{ chunk_index: -1, preview: 'Error fetching passages: ' + (e.message || 'unknown') } as PassageHit])
    }
  }

  async function loadMorePassages() {
    if (!activeDocId || passagesLoading || !passagesHasMore) return
    await fetchPassages(activeDocId, activeQuery, true)
  }

  async function showChunk(idx: number) {
    const doc = currentDocRef.current; if (!doc) return
    try {
      setModalTitle(`Chunk ${idx} — ${doc.title} (doc ${doc.doc_id})`)
      setOpenChunk(true); setChunkData(null)
      const r = await api.getJSON(`/api/docs/${encodeURIComponent(doc.doc_id)}/chunk/${idx}`) as ChunkFull
      setChunkData(r)
    } catch (e: any) { setChunkData({ chunk_index: idx, text: 'Error fetching chunk: ' + (e.message || 'unknown') }) }
  }

  // ---- LLM summarizer preview/save (uses new backend routes) ----
  async function previewLLM() {
    const doc = currentDocRef.current; if (!doc) return
    try {
      setLlmBusy(true); setLlmNote('')
      const r = await api.postJSON(`/api/docs/${encodeURIComponent(doc.doc_id)}/llm_summarize/preview`, { style: llmStyle }) as { doc_id: string; summary: string }
      setLlmSummary(r?.summary ?? '')
      if (!r?.summary) setLlmNote('No text found for this document.')
    } catch (e: any) {
      setLlmSummary(''); setLlmNote(e?.message || 'Preview failed')
    } finally {
      setLlmBusy(false)
    }
  }
  async function saveLLM() {
    const doc = currentDocRef.current; if (!doc) return
    try {
      setLlmBusy(true); setLlmNote('')
      const r = await api.postJSON(`/api/docs/${encodeURIComponent(doc.doc_id)}/llm_summarize/save`, { style: llmStyle }) as { ok: boolean; saved_to?: string }
      if (r?.ok) {
        setLlmNote(`Saved ✓ ${r.saved_to ? `(${r.saved_to})` : ''}`)
        listDocs()
      } else {
        setLlmNote('Save failed')
      }
    } catch (e: any) {
      setLlmNote(e?.message || 'Save failed')
    } finally {
      setLlmBusy(false)
    }
  }

  // ---- Answer flow (posts to /api/gateway/ask first, fallback to /gateway/ask) ----
  async function openCitedChunk(docId: string, idx: number) {
    const meta = docs.find(d => d.doc_id === docId)
    currentDocRef.current = meta || { doc_id: docId, title: docId, dept: '', lang: '', type: '', stages: {} as any }
    await showChunk(idx)
  }

  function cleanAnswerText(s: string): string {
    if (!s) return s
    // strip final trailing "[DOC#...]" segment if present (plus trailing newline/space)
    const cleaned = s.replace(/\n?\[DOC#[^\]]+\]\s*$/g, '').replace(/\s+$/g, '')
    return cleaned
  }

  async function askAnswer(doc?: DocRow) {
    const body: any = {
      q: (q || '').trim(),
      dept: dept || undefined,
      lang: lang || undefined,
      k: 5,
      neighbor: 0,
    }
    if (doc?.doc_id) body.doc_id = doc.doc_id

    setOpenAnswer(true)
    setAnswerLoading(true)
    setAnswerEndpoint('')
    setAnswerData(null)
    setShowSources(false)

    try {
      // Primary path (Vite proxy-friendly)
      setAnswerEndpoint('/api/gateway/ask')
      const r = await api.postJSON('/api/gateway/ask', body) as { answer: string; cites: Array<{ doc_id: string; chunk_index: number }> }
      setAnswerData({ answer: cleanAnswerText(r?.answer ?? 'No response.'), cites: Array.isArray(r?.cites) ? r.cites : [] })
    } catch (e1: any) {
      // Fallback path (same-origin gateway)
      try {
        setAnswerEndpoint('/gateway/ask')
        const r2 = await api.postJSON('/gateway/ask', body) as { answer: string; cites: Array<{ doc_id: string; chunk_index: number }> }
        setAnswerData({ answer: cleanAnswerText(r2?.answer ?? 'No response.'), cites: Array.isArray(r2?.cites) ? r2.cites : [] })
      } catch (e2: any) {
        setAnswerData({ answer: 'Error: ' + (e2?.message || e1?.message || 'request failed'), cites: [] })
      }
    } finally {
      setAnswerLoading(false)
    }
  }

  const totalPages = useMemo(() => Math.max(1, Math.ceil(total / Math.max(1, limit))), [total, limit])

  return (
    <div className="p-4 md:p-6 max-w-7xl mx-auto">
      <div className="flex items-center justify-between">
        <div className="text-lg font-semibold">DocuMind-HR</div>
        <div className="flex items-center gap-2"><Dot color={health === 'up' ? 'green' : health === 'down' ? 'red' : 'gray'} /><span className="text-sm">{health === 'up' ? 'Healthy' : health === 'down' ? 'Down' : 'Unknown'}</span></div>
        <div className="flex flex-wrap items-center gap-2">
          {stats && (<>
            <Chip>docs: {stats.counts.documents}</Chip>
            <Chip>chunks: {stats.counts.chunks}</Chip>
            {typeof stats.counts.embeddings === 'number' && <Chip>embeddings: {stats.counts.embeddings}</Chip>}
            {typeof stats.counts.sem_summaries === 'number' && <Chip>semSum: {stats.counts.sem_summaries}</Chip>}
            {typeof stats.counts.llm_summaries === 'number' && <Chip>llmSum: {stats.counts.llm_summaries}</Chip>}
            <Chip>pgvector: {stats.features.pgvector ? 'on' : 'off'}</Chip>
            <Chip>pg_trgm: {stats.features.pg_trgm ? 'on' : 'off'}</Chip>
          </>)}
          <button className="text-sm px-3 py-1.5 rounded-lg border bg-white hover:bg-gray-50" onClick={refreshStats}>Refresh</button>
        </div>
      </div>

      <div className="mt-5 p-4 rounded-2xl border bg-white shadow-sm">
        <div className="grid md:grid-cols-12 gap-3">
          <div className="md:col-span-5">
            <label className="text-xs text-gray-500">Query</label>
            <input value={q} onChange={(e)=>setQ(e.target.value)} placeholder="Type to search across ALL documents…" className="w-full mt-1 px-3 py-2 rounded-xl border" />
          </div>
          <div className="md:col-span-2">
            <label className="text-xs text-gray-500">Limit</label>
            <select value={limit} onChange={(e)=>setLimit(Number(e.target.value))} className="w-full mt-1 px-3 py-2 rounded-xl border">
              {[5,10,25,50].map(n => <option key={n} value={n}>{n}</option>)}
            </select>
          </div>
          <div className="md:col-span-5 flex items-end justify-end gap-3">
            <label className="text-xs text-gray-500 flex items-center gap-2">
              <span>Mode</span>
              <select value={searchMode} onChange={(e)=>setSearchMode(e.target.value as SearchMode)} className="px-2 py-1 rounded border text-sm">
                <option value="keyword">Keyword</option>
                <option value="semantic">Semantic</option>
                <option value="hybrid">Hybrid</option>
              </select>
            </label>
            <label className="text-xs text-gray-500 flex items-center gap-2">
              <input type="checkbox" checked={enableSynonyms} onChange={(e)=>setEnableSynonyms(e.target.checked)} />
              Use synonyms
            </label>
            <label className="text-xs text-gray-500 flex items-center gap-2">
              <input type="checkbox" checked={enableSemanticInclude} onChange={(e)=>setEnableSemanticInclude(e.target.checked)} disabled={searchMode!=='keyword'} />
              Include semantic (purple)
            </label>
            <label className="text-xs text-gray-500 flex items-center gap-2">
              <input type="checkbox" checked={enableSemanticColor} onChange={(e)=>setEnableSemanticColor(e.target.checked)} />
              Color semantic
            </label>
            <button onClick={()=>listDocs(1)} className="px-4 py-2 rounded-xl bg-emerald-600 text-white shadow hover:bg-emerald-700">List</button>
            <button onClick={()=>{ setQ(''); setDept(''); setLang(''); setDtype(''); setFrom(''); setTo(''); setPage(1); setSearchMode('keyword'); listDocs(1) }} className="px-4 py-2 rounded-xl border bg-white hover:bg-gray-50">Clear</button>
            <button onClick={()=>{ currentDocRef.current = null; askAnswer(undefined) }} className="px-4 py-2 rounded-xl border bg-white hover:bg-gray-50" title="Ask over filtered docs (no specific doc_id)">
              Ask (global)
            </button>
          </div>

          <div className="md:col-span-4">
            <label className="text-xs text-gray-500">Department</label>
            <input value={dept} onChange={(e)=>setDept(e.target.value)} placeholder="e.g., Education" className="w-full mt-1 px-3 py-2 rounded-xl border" />
          </div>
          <div className="md:col-span-4">
            <label className="text-xs text-gray-500">Doc type</label>
            <input value={dtype} onChange={(e)=>setDtype(e.target.value)} placeholder="e.g., Acts and Rules" className="w-full mt-1 px-3 py-2 rounded-xl border" />
          </div>
          <div className="md:col-span-4">
            <label className="text-xs text-gray-500">Language</label>
            <input value={lang} onChange={(e)=>setLang(e.target.value)} placeholder="en / hi" className="w-full mt-1 px-3 py-2 rounded-xl border" />
          </div>

          <div className="md:col-span-3">
            <label className="text-xs text-gray-500">From</label>
            <input type="date" value={from} onChange={(e)=>setFrom(e.target.value)} className="w-full mt-1 px-3 py-2 rounded-xl border" />
          </div>
          <div className="md:col-span-3">
            <label className="text-xs text-gray-500">To</label>
            <input type="date" value={to} onChange={(e)=>setTo(e.target.value)} className="w-full mt-1 px-3 py-2 rounded-xl border" />
          </div>
        </div>
      </div>

      <div className="mt-6">
        <div className="flex items-center justify-between mb-2">
          <div className="text-sm text-gray-600">
            {loading ? 'Loading…' : `Showing ${docs.length} of ${total} ${q.trim() ? 'matched' : 'total'}`}
            {qStats && (<span className="ml-2 px-2 py-0.5 rounded-full bg-indigo-50 text-indigo-700 border border-indigo-200">query: {qStats.ms} ms</span>)}
          </div>
          {!q.trim() && (
            <div className="flex items-center gap-2">
              <button disabled={page<=1} onClick={()=>listDocs(page-1)} className={`px-3 py-1.5 rounded-lg border ${page<=1 ? 'opacity-50' : 'hover:bg-gray-50'}`}>← Prev</button>
              <Chip>{page} / {Math.max(1, Math.ceil(total / Math.max(1, limit)))}</Chip>
              <button disabled={page>=Math.max(1, Math.ceil(total / Math.max(1, limit)))} onClick={()=>listDocs(page+1)} className={`px-3 py-1.5 rounded-lg border ${page>=Math.max(1, Math.ceil(total / Math.max(1, limit))) ? 'opacity-50' : 'hover:bg-gray-50'}`}>Next →</button>
            </div>
          )}
        </div>

        {error && <div className="text-sm text-red-700 bg-red-50 border border-red-200 rounded p-2 mb-2">{error}</div>}

        <div className="grid md:grid-cols-3 gap-6">
          <div className="md:col-span-2">
            <div className="overflow-auto rounded-2xl border bg-white shadow-sm">
              <table className="min-w-full text-sm">
                <thead className="bg-gray-50">
                  <tr>
                    <th className="px-3 py-2 text-left w-10">#</th>
                    <th className="px-3 py-2 text-left">Title & Description</th>
                    <th className="px-3 py-2 text-left">Department</th>
                    <th className="px-3 py-2 text-left">Lang</th>
                    <th className="px-3 py-2 text-left">Type</th>
                    <th className="px-3 py-2 text-left">Stages</th>
                    <th className="px-3 py-2 text-left">Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {docs.map((d, i) => {
                    const firstHit = matchesByDoc[d.doc_id]?.[0]
                    const isSemanticOnly = enableSemanticColor && firstHit && firstHit.kind === 'semantic'
                    return (
                      <tr key={d.doc_id} className={`border-t ${isSemanticOnly ? 'bg-purple-50/40' : ''}`}>
                        <td className="px-3 py-2 align-top">{i + 1}</td>
                        <td className="px-3 py-2 align-top">
                          <div className="font-medium">{d.title}</div>
                          <div className="text-xs text-gray-500">{d.doc_id}</div>
                          {!!q.trim() && matchesByDoc[d.doc_id]?.length > 0 && (
                            <div className={`mt-1 text-xs text-gray-800 ${isSemanticOnly ? 'border border-purple-200 rounded p-1' : ''}`}>
                              <span className="italic">
                                {highlightKeywordThenSyn(matchesByDoc[d.doc_id][0]?.preview || '', queryRegex, synonymRegex)}
                              </span>
                              <span className="ml-2 px-2 py-0.5 rounded-full bg-emerald-50 text-emerald-700 border-emerald-200 border">
                                {matchesByDoc[d.doc_id].length} match{matchesByDoc[d.doc_id].length > 1 ? 'es' : ''}
                              </span>
                              {firstHit?.kind && (
                                <span className={`ml-2 px-2 py-0.5 rounded-full border ${
                                  firstHit.kind === 'keyword' ? 'bg-yellow-50 text-yellow-800 border-yellow-200' :
                                  firstHit.kind === 'synonym' ? 'bg-sky-50 text-sky-800 border-sky-200' :
                                  'bg-purple-50 text-purple-800 border-purple-200'
                                }`}>{firstHit.kind}</span>
                              )}
                            </div>
                          )}
                        </td>
                        <td className="px-3 py-2 align-top">{d.dept}</td>
                        <td className="px-3 py-2 align-top">{d.lang}</td>
                        <td className="px-3 py-2 align-top">{d.type}</td>
                        <td className="px-3 py-2 align-top">
                          <div className="flex flex-wrap gap-1.5">
                            <StageBadge label="PDF"     state={inferState(d.stages?.pdf)} />
                            <StageBadge label="OCR"     state={inferState(d.stages?.ocr)} />
                            <StageBadge label="Text"    state={inferState(d.stages?.text)} />
                            <StageBadge label="Chunks"  state={inferState(d.stages?.chunks)} />
                            <StageBadge label="Embeds"  state={inferState(d.stages?.embeds)} />
                            <StageBadge label="SemSum"  state={inferState(d.stages?.sem_summary)} />
                            <StageBadge label="LLMSum"  state={inferState(d.stages?.llm_summary) ?? 'generated'} />
                          </div>
                        </td>
                        <td className="px-3 py-2 align-top">
                          <div className="flex flex-wrap gap-1.5">
                            <button onClick={()=>openPdf(d)} className="px-2 py-1 text-xs rounded-lg border hover:bg-gray-50">PDF</button>
                            <button onClick={()=>showOCR(d)} className="px-2 py-1 text-xs rounded-lg border hover:bg-gray-50">OCR</button>
                            <button onClick={()=>showSummary(d)} className="px-2 py-1 text-xs rounded-lg border hover:bg-gray-50">Summary</button>
                            <button onClick={()=>showPassages(d)} className="px-2 py-1 text-xs rounded-lg border hover:bg-gray-50">View/Match</button>
                            <button
                              onClick={(e)=>{ e.stopPropagation(); copyToClipboard(absolutize(`/api/docs/${encodeURIComponent(d.doc_id)}/pdf`), 'PDF URL copied') }}
                              className="px-2 py-1 text-xs rounded-lg border hover:bg-gray-50"
                              title="Copy PDF URL"
                            >
                              Copy PDF URL
                            </button>
                            <button
                              onClick={(e)=>{ 
                                e.stopPropagation()
                                const idx = matchesByDoc[d.doc_id]?.[0]?.chunk_index ?? 0
                                copyToClipboard(absolutize(`/api/docs/${encodeURIComponent(d.doc_id)}/chunk/${idx}`), 'Chunk link copied')
                              }}
                              className="px-2 py-1 text-xs rounded-lg border hover:bg-gray-50"
                              title="Copy top-hit chunk link"
                            >
                              Copy chunk link
                            </button>
                            <button
                              onClick={() => { currentDocRef.current = d; askAnswer(d) }}
                              className="px-2 py-1 text-xs rounded-lg border hover:bg-gray-50"
                              title="Ask over this document"
                            >
                              Answer
                            </button>
                            <SummarizeLLM docId={d.doc_id} />
                          </div>
                        </td>
                      </tr>
                    )
                  })}
                  {docs.length === 0 && !loading && (<tr><td colSpan={7} className="px-3 py-8 text-center text-gray-500">No documents match{q.trim() ? ' your query.' : ' the filters.'}</td></tr>)}
                </tbody>
              </table>
            </div>
          </div>

          <div className="md:col-span-1">
            <div className="rounded-2xl border bg-white shadow-sm p-3">
              <div className="font-semibold mb-2">Timing (last)</div>
              <div className="text-sm space-y-1">
                <div className="flex justify-between"><span>encode</span><span>{ms(stats?.avg_ms.encode_ms)}</span></div>
                <div className="flex justify-between"><span>semantic</span><span>{ms(stats?.avg_ms.semantic_ms)}</span></div>
                <div className="flex justify-between"><span>keyword</span><span>{ms(stats?.avg_ms.keyword_ms)}</span></div>
                {'rerank_ms' in (stats?.avg_ms || {}) && <div className="flex justify-between"><span>rerank</span><span>{ms(stats?.avg_ms.rerank_ms)}</span></div>}
                <div className="flex justify-between font-medium"><span>total</span><span>{ms(stats?.avg_ms.total_ms)}</span></div>
              </div>

              <div className="text-sm mt-3 pt-3 border-t">
                <div className="flex justify-between"><span>query (client)</span><span>{qStats ? `${qStats.ms} ms` : '—'}</span></div>
                <div className="flex justify-between"><span>docs scanned</span><span>{qStats?.scanned ?? '—'}</span></div>
                <div className="flex justify-between"><span>docs matched</span><span>{qStats?.matched ?? '—'}</span></div>
                <div className="flex justify-between"><span>hits</span><span>{qStats?.hits ?? '—'}</span></div>
                <div className="flex gap-1 mt-1">
                  <Chip className="bg-yellow-50 text-yellow-800 border-yellow-200">kw: {qStats?.kw ?? '—'}</Chip>
                  <Chip className="bg-sky-50 text-sky-800 border-sky-200">syn: {qStats?.syn ?? '—'}</Chip>
                  <Chip className="bg-purple-50 text-purple-800 border-purple-200">sem: {qStats?.sem ?? '—'}</Chip>
                </div>
              </div>
            </div>

            <div className="rounded-2xl border bg-white shadow-sm p-3 mt-4">
              <div className="font-semibold mb-2">Latency history</div>
              <div className="h-16 w-full bg-gray-50 rounded-lg border flex items-end gap-1 p-2">
                {history.length === 0 && <div className="text-xs text-gray-400 m-auto">(empty)</div>}
                {history.map((v, i) => <div key={i} className="bg-gray-400 rounded w-3" style={{ height: `${Math.min(100, Math.max(8, v/5))}%` }} title={`${v} ms`} />)}
              </div>
            </div>
          </div>
        </div>
      </div>

      {/* OCR Modal */}
      <Modal open={openOCR} onClose={()=>setOpenOCR(false)} title={modalTitle} footer={<button className="px-3 py-1.5 rounded-lg border" onClick={()=>setOpenOCR(false)}>Close</button>}>
        <pre className="text-xs whitespace-pre-wrap leading-5 bg-gray-50 p-3 rounded-xl border">{ocrText}</pre>
        {ocrMeta && <div className="text-xs text-gray-600 mt-2">{ocrMeta.pages ? `pages: ${ocrMeta.pages}` : ''} {ocrMeta.chars ? `· chars: ${ocrMeta.chars}` : ''}</div>}
      </Modal>

      {/* Semantic Summary Modal */}
      <Modal open={openSummary} onClose={()=>setOpenSummary(false)} title={modalTitle} footer={
        <div className="flex gap-2 items-center">
          <label className="text-xs text-gray-500">K</label>
          <select value={summaryK} onChange={(e)=>setSummaryK(Number(e.target.value))} className="px-2 py-1 text-sm rounded border">{[1,3,5,10].map(k=> <option key={k} value={k}>{k}</option>)}</select>
          <label className="text-xs text-gray-500">Probes</label>
          <input value={summaryProbes} onChange={(e)=>setSummaryProbes(Number(e.target.value))} className="px-2 py-1 text-sm rounded border w-16" />
          <button className="px-3 py-1.5 rounded-lg border" onClick={()=> currentDocRef.current && showSummary(currentDocRef.current!)}>Apply</button>
          <button className="px-3 py-1.5 rounded-lg bg-emerald-600 text-white" onClick={()=> currentDocRef.current && saveSummary(currentDocRef.current!)}>Save to DB</button>
          <button className="px-3 py-1.5 rounded-lg border" onClick={()=>setOpenSummary(false)}>Close</button>
        </div>
      }>
        {summaryResp ? (
          <>
            <div className="flex items-center gap-2 text-xs mb-2">
              <Chip>source: {summaryResp.source}</Chip>
              {typeof summaryResp.k === 'number' && <Chip>K: {summaryResp.k}</Chip>}
              {typeof summaryResp.probes === 'number' && <Chip>probes: {summaryResp.probes}</Chip>}
            </div>
            <textarea className="w-full h-[50vh] text-sm leading-6 bg-gray-50 p-3 rounded-xl border" value={summaryResp.text} readOnly />
          </>
        ) : <div className="text-sm text-gray-500">Loading summary…</div>}
      </Modal>

      {/* Passages Modal */}
      <Modal
        open={openPassages}
        onClose={()=>setOpenPassages(false)}
        title={modalTitle}
        footer={
          <div className="flex items-center gap-2">
            <button
              className="px-3 py-1.5 rounded-lg border"
              disabled={!passagesHasMore || passagesLoading}
              onClick={loadMorePassages}
            >
              {passagesLoading ? 'Loading…' : (passagesHasMore ? 'More' : 'No more')}
            </button>
            <button className="px-3 py-1.5 rounded-lg border" onClick={()=>setOpenPassages(false)}>Close</button>
          </div>
        }
      >
        <table className="min-w-full text-sm">
          <thead className="bg-gray-50 sticky top-0">
            <tr><th className="px-3 py-2 text-left w-12">Rank</th><th className="px-3 py-2 text-left w-20">Chunk#</th><th className="px-3 py-2 text-left w-24">Score</th><th className="px-3 py-2 text-left w-20">Chars</th><th className="px-3 py-2 text-left">Preview</th><th className="px-3 py-2 text-left w-28">Action</th></tr>
          </thead>
          <tbody>
            {passages.map((p, i) => (
              <tr key={`${p.chunk_index}-${i}`} className="border-t align-top">
                <td className="px-3 py-2">{i+1}</td>
                <td className="px-3 py-2">{p.chunk_index}</td>
                <td className="px-3 py-2">{p.score !== undefined ? Number(p.score).toFixed(3) : '—'}</td>
                <td className="px-3 py-2">{p.chars ?? '—'}</td>
                <td className="px-3 py-2">
                  <div className={`text-gray-800 whitespace-pre-wrap ${p.kind === 'semantic' && enableSemanticColor ? 'border border-purple-200 rounded p-1' : ''}`}>
                    {highlightKeywordThenSyn(p.preview, queryRegex, synonymRegex)}
                  </div>
                </td>
                <td className="px-3 py-2">
                  {p.kind && (
                    <Chip className={
                      p.kind === 'keyword' ? 'bg-yellow-50 text-yellow-800 border-yellow-200' :
                      p.kind === 'synonym' ? 'bg-sky-50 text-sky-800 border-sky-200' :
                      'bg-purple-50 text-purple-800 border-purple-200'
                    }>{p.kind}</Chip>
                  )}
                  <button onClick={()=>showChunk(p.chunk_index)} className="ml-2 px-2 py-1 text-xs rounded-lg border hover:bg-gray-50">Open chunk</button>
                </td>
              </tr>
            ))}
            {passages.length === 0 && (
              <tr>
                <td colSpan={6} className="px-3 py-6 text-center text-gray-500">
                  {q.trim() ? 'No ranked hits.' : 'No passages found.'}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </Modal>

      {/* Chunk Modal */}
      <Modal open={openChunk} onClose={()=>setOpenChunk(false)} title={modalTitle} footer={<button className="px-3 py-1.5 rounded-lg border" onClick={()=>setOpenChunk(false)}>Close</button>}>
        {chunkData ? (
          <>
            <div className="text-xs text-gray-600 mb-2">
              chunk #{chunkData.chunk_index}
              {typeof chunkData.chars === 'number' && <> · {chunkData.chars} chars</>}
              {typeof chunkData.has_embed === 'boolean' && <> · embed: {chunkData.has_embed ? 'yes' : 'no'}</>}
            </div>
            <pre className="text-xs whitespace-pre-wrap leading-5 bg-gray-50 p-3 rounded-xl border">{chunkData.text}</pre>
          </>
        ) : <div className="text-sm text-gray-500">Loading chunk…</div>}
      </Modal>

      {/* LLM Summary Modal */}
      <Modal open={openLLM} onClose={()=>setOpenLLM(false)} title={modalTitle} footer={
        <div className="flex gap-2 items-center">
          <label className="text-xs text-gray-500">Style</label>
          <select value={llmStyle} onChange={(e)=>setLlmStyle(e.target.value as any)} className="px-2 py-1 text-sm rounded border">
            <option value="bullet">Bullet</option>
            <option value="short">Short</option>
            <option value="detailed">Detailed</option>
          </select>
          <button className="px-3 py-1.5 rounded-lg border" onClick={previewLLM} disabled={llmBusy}>{llmBusy ? 'Summarizing…' : 'Preview'}</button>
          <button className={`px-3 py-1.5 rounded-lg ${llmSummary ? 'bg-violet-600 text-white' : 'border opacity-60'}`} onClick={saveLLM} disabled={!llmSummary || llmBusy}>
            {llmBusy ? 'Saving…' : 'Save'}
          </button>
          <button className="px-3 py-1.5 rounded-lg border" onClick={()=>setOpenLLM(false)}>Close</button>
        </div>
      }>
        {llmNote && <div className="text-xs text-emerald-700 mb-2">{llmNote}</div>}
        <textarea className="w-full h-[50vh] text-sm leading-6 bg-gray-50 p-3 rounded-xl border" value={llmSummary} readOnly placeholder="Click Preview to generate a summary…" />
      </Modal>

      {/* Answer Modal */}
      <Modal
        open={openAnswer}
        onClose={() => setOpenAnswer(false)}
        title={"Answer — " + (currentDocRef.current ? `${currentDocRef.current.title} (doc ${currentDocRef.current.doc_id})` : "Global")}
        footer={<button className="px-3 py-1.5 rounded-lg border" onClick={() => setOpenAnswer(false)}>Close</button>}
      >
        <div className="mb-2 flex items-center gap-2">
          <Chip>endpoint: {answerEndpoint || '—'}</Chip>
          {answerData?.cites?.length ? answerData.cites.slice(0,5).map((c, i) =>
            <button key={`${c.doc_id}-${c.chunk_index}-${i}`} className="px-2 py-0.5 text-xs rounded-full border bg-white hover:bg-gray-50" onClick={() => openCitedChunk(c.doc_id, c.chunk_index)}>{c.doc_id} : #{c.chunk_index}</button>
          ) : <Chip>No cites</Chip>}
        </div>
        {answerLoading && <div className="text-sm text-gray-500">Asking model…</div>}
        {!answerLoading && answerData && (
          <>
            <textarea className="w-full h-[40vh] text-sm leading-6 bg-gray-50 p-3 rounded-xl border" value={answerData.answer} readOnly />
            <div className="mt-3">
              <button onClick={()=>setShowSources(!showSources)} className="text-sm text-indigo-600 hover:underline">
                {showSources ? 'Hide Sources ▲' : 'Show Sources ▼'}
              </button>
              {showSources && (
                <div className="mt-2 space-y-1">
                  {answerData.cites.map((c, i) => (
                    <div key={`${c.doc_id}-${c.chunk_index}-${i}`} className="flex items-center gap-2 text-xs">
                      <span>{c.doc_id} : chunk {c.chunk_index}</span>
                      <a href={`/api/docs/${encodeURIComponent(c.doc_id)}/pdf`} target="_blank" rel="noopener noreferrer" className="text-blue-600 hover:underline">PDF</a>
                      <a href={`/api/docs/${encodeURIComponent(c.doc_id)}/chunk/${c.chunk_index}`} target="_blank" rel="noopener noreferrer" className="text-blue-600 hover:underline">Chunk</a>
                    </div>
                  ))}
                  {answerData.cites.length === 0 && <div className="text-xs text-gray-500">No sources.</div>}
                </div>
              )}
            </div>
          </>
        )}
      </Modal>
    </div>
  )
}
