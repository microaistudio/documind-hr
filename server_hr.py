# Version: 3.3.11
# Path: server_hr.py
# Purpose: DocuMind-HR API — /health, /api/search_text (keyword+synonyms), /api/search (weighted fusion + ±1 de-dup + optional rerank), /api/answer (stitched citations + highlighting + optional HTML)
# Changelog 3.3.11:
#   - /api/answer: add `format=text|html` (default text). When html, include `answer_html` with <mark> highlights.

import os
import time
import re
from typing import Optional, List, Dict, Tuple
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv
from pydantic import BaseModel

# helpers
from src.utils.synonyms import tokenize, build_where_and_params
from src.utils.embeddings import encode, to_pgvector

# --- ENV ---
load_dotenv(os.environ.get("ENV_FILE", ".env.hr"))
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "9001"))
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:5432/documind_hr")
IVFFLAT_PROBES = int(os.getenv("IVFFLAT_PROBES", "10"))

# Weighted fusion (semantic vs keyword)
try:
    _alpha = float(os.getenv("FUSION_ALPHA", "0.8"))
except Exception:
    _alpha = 0.8
FUSION_ALPHA = min(1.0, max(0.0, _alpha))

# Optional reranker (cross-encoder)
RERANK = os.getenv("RERANK", "0").strip() in ("1", "true", "yes", "on")
RERANK_TOP = int(os.getenv("RERANK_TOP", "10"))
RERANK_MODEL = os.getenv("RERANK_MODEL", "cross-encoder/ms-marco-MiniLM-L-6-v2")

# Prewarm toggles
PREWARM = os.getenv("PREWARM", "0").strip() in ("1", "true", "yes", "on")
PREWARM_RERANK = os.getenv("PREWARM_RERANK", "0").strip() in ("1", "true", "yes", "on")

# --- APP ---
app = FastAPI(title="DocuMind-HR", version="3.3.11")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

# --- DB helpers ---
def db_row(sql: str, params=None):
    with psycopg2.connect(DATABASE_URL) as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, params or ())
            return cur.fetchone()

def db_rows(sql: str, params=None):
    with psycopg2.connect(DATABASE_URL) as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, params or ())
            return cur.fetchall()

# --- Health ---
@app.get("/health")
def health():
    ext_vec = db_row("SELECT extname FROM pg_extension WHERE extname='vector' LIMIT 1;")
    ext_trg = db_row("SELECT extname FROM pg_extension WHERE extname='pg_trgm' LIMIT 1;")
    docs = db_row("SELECT COUNT(*) AS n FROM documents;")["n"]
    chks = db_row("SELECT COUNT(*) AS n FROM chunks;")["n"]
    enc  = db_row("SHOW SERVER_ENCODING;")["server_encoding"]
    return {
        "ok": True, "service": "DocuMind-HR", "host": HOST, "port": PORT,
        "pgvector": bool(ext_vec), "pg_trgm": bool(ext_trg), "encoding": enc,
        "documents": docs, "chunks": chks
    }

# --- Models ---
class TextHit(BaseModel):
    doc_id: str
    title: Optional[str]
    dept: str
    lang: str
    chunk_index: int
    score: float
    text: str

class SearchHit(TextHit):
    source: str  # "semantic" | "keyword" | "fallback" | "hybrid"

# --- Keyword search (ILIKE + trigram) with EN↔HI synonyms ---
@app.get("/api/search_text", response_model=List[TextHit])
def api_search_text(
    q: str = Query(..., min_length=1),
    dept: Optional[str] = Query(None),
    lang: Optional[str] = Query(None),
    limit: int = Query(5, ge=1, le=20),
    expand: int = Query(1, ge=0, le=1),
):
    t0 = time.perf_counter()

    filters: List[str] = []
    params: List[str] = []
    if expand:
        tokens = tokenize(q)
        syn_where, syn_params = build_where_and_params("c.text", tokens)
        filters.append(f"({syn_where})")
        params.extend(syn_params)
    else:
        filters.append("(c.text ILIKE %s OR similarity(c.text, %s) > 0.1)")
        params.extend([f"%{q}%", q])

    if dept:
        filters.append("d.dept = %s"); params.append(dept.strip().lower())
    if lang:
        filters.append("d.lang = %s"); params.append(lang.strip().lower())

    where = "WHERE " + " AND ".join(filters) if filters else ""
    sql = f"""
        SELECT d.doc_id, d.title, d.dept, d.lang, c.chunk_index,
               GREATEST(similarity(c.text, %s), 0) AS score,
               SUBSTRING(c.text FOR 700) AS text
        FROM chunks c
        JOIN documents d ON d.id = c.document_id
        {where}
        ORDER BY score DESC
        LIMIT %s;
    """
    rows = db_rows(sql, [q, *params, limit])
    out = [TextHit(
        doc_id=r["doc_id"], title=r["title"], dept=r["dept"], lang=r["lang"],
        chunk_index=r["chunk_index"], score=float(r["score"]), text=r["text"] or ""
    ) for r in rows]

    try:
        total_ms = int((time.perf_counter() - t0) * 1000)
        print(f"[search_text] q='{q[:80]}' dept={dept} lang={lang} expand={expand} "
              f"limit={limit} results={len(out)} total_ms={total_ms}")
    except Exception:
        pass

    return out

# --- Lazy reranker loader (fail-open) ---
_RERANKER = None
def _get_reranker():
    global _RERANKER
    if _RERANKER is not None:
        return _RERANKER
    try:
        from sentence_transformers import CrossEncoder  # type: ignore
        _RERANKER = CrossEncoder(RERANK_MODEL, max_length=512)
        return _RERANKER
    except Exception as e:
        print(f"[WARN] reranker unavailable ({e}); skipping.")
        _RERANKER = False
        return None

def _maybe_rerank(q: str, cands: List['SearchHit'], top_m: int) -> Tuple[List['SearchHit'], int, int]:
    if not RERANK or top_m <= 0 or not cands:
        return cands, 0, 0
    model = _get_reranker()
    if not model:
        return cands, 0, 0
    m = min(top_m, len(cands))
    pairs = [(q, (c.text or "")) for c in cands[:m]]
    t0 = time.perf_counter()
    try:
        scores = model.predict(pairs, batch_size=32, convert_to_tensor=False)
        ranked = sorted(zip(cands[:m], scores), key=lambda x: float(x[1]), reverse=True)
        reranked = [h for (h, s) in ranked]
        for i, (_, s) in enumerate(ranked):
            try:
                reranked[i].score = float(s)
            except Exception:
                pass
        out = reranked + cands[m:]
        ms = int((time.perf_counter() - t0) * 1000)
        return out, m, ms
    except Exception as e:
        print(f"[WARN] rerank failed: {e}")
        return cands, 0, 0

# --- Semantic search + weighted fusion + de-dup (+ optional rerank) ---
@app.get("/api/search", response_model=List[SearchHit])
def api_search(
    q: str = Query(..., min_length=1, description="Natural language query"),
    dept: Optional[str] = Query(None),
    lang: Optional[str] = Query(None),
    k: int = Query(8, ge=1, le=50, description="semantic candidates"),
    n: int = Query(3, ge=1, le=10, description="final results")
):
    T0 = time.perf_counter()
    enc_ms = sem_ms = kw_ms = 0
    rerank_ms = 0
    rerank_used = 0

    where_parts = ["c.embedding IS NOT NULL"]
    where_params: List[str] = []
    if dept:
        where_parts.append("d.dept = %s"); where_params.append(dept.strip().lower())
    if lang:
        where_parts.append("d.lang = %s"); where_params.append(lang.strip().lower())
    where_sql = "WHERE " + " AND ".join(where_parts)

    sem_rows = []
    try:
        t_enc = time.perf_counter()
        qvec = encode(q)[0]
        enc_ms = int((time.perf_counter() - t_enc) * 1000)

        vstr = to_pgvector(qvec)
        with psycopg2.connect(DATABASE_URL) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                try:
                    cur.execute("SET LOCAL ivfflat.probes = %s;", [IVFFLAT_PROBES])
                except Exception:
                    pass
                t_sem = time.perf_counter()
                cur.execute(f"""
                    SELECT d.doc_id, d.title, d.dept, d.lang, c.chunk_index,
                           (1 - (c.embedding <=> %s::vector)) AS score,
                           SUBSTRING(c.text FOR 700) AS text
                    FROM chunks c
                    JOIN documents d ON d.id = c.document_id
                    {where_sql}
                    ORDER BY c.embedding <=> %s::vector
                    LIMIT %s;
                """, [vstr, *where_params, vstr, k])
                sem_rows = cur.fetchall()
                sem_ms = int((time.perf_counter() - t_sem) * 1000)
    except Exception as e:
        print(f"[WARN] semantic search failed: {e}")

    sem_hits = [SearchHit(
        doc_id=r["doc_id"], title=r["title"], dept=r["dept"], lang=r["lang"],
        chunk_index=r["chunk_index"], score=float(r["score"]), text=r["text"] or "",
        source="semantic"
    ) for r in sem_rows]

    t_kw = time.perf_counter()
    kw_base = api_search_text(q=q, dept=dept, lang=lang, limit=k, expand=1)
    kw_ms = int((time.perf_counter() - t_kw) * 1000)
    kw_hits = [SearchHit(**h.dict(), source="keyword") for h in kw_base]

    key = lambda h: (h.doc_id, h.chunk_index)
    merged: Dict[Tuple[str,int], Dict] = {}

    def clamp01(x: float) -> float:
        return 0.0 if x is None else max(0.0, min(1.0, x))

    for h in sem_hits:
        kkey = key(h)
        s_norm = clamp01((h.score + 1.0) / 2.0)
        merged[kkey] = {
            "doc_id": h.doc_id, "title": h.title, "dept": h.dept, "lang": h.lang,
            "chunk_index": h.chunk_index, "sem": s_norm, "kw": 0.0,
            "text": h.text, "source_sem": True, "source_kw": False
        }

    for h in kw_hits:
        kkey = key(h)
        kw_norm = clamp01(h.score)
        if kkey in merged:
            m = merged[kkey]
            m["kw"] = max(m["kw"], kw_norm)
            if not m["text"]:
                m["text"] = h.text
            m["source_kw"] = True
        else:
            merged[kkey] = {
                "doc_id": h.doc_id, "title": h.title, "dept": h.dept, "lang": h.lang,
                "chunk_index": h.chunk_index, "sem": 0.0, "kw": kw_norm,
                "text": h.text, "source_sem": False, "source_kw": True
            }

    alpha = FUSION_ALPHA
    cands: List[SearchHit] = []
    for m in merged.values():
        combined = alpha * m["sem"] + (1.0 - alpha) * m["kw"]
        source = "hybrid" if (m["source_sem"] and m["source_kw"]) else ("semantic" if m["source_sem"] else "keyword")
        cands.append(SearchHit(
            doc_id=m["doc_id"], title=m["title"], dept=m["dept"], lang=m["lang"],
            chunk_index=m["chunk_index"], score=float(combined), text=m["text"] or "",
            source=source
        ))

    cands.sort(key=lambda h: h.score, reverse=True)

    if RERANK and cands:
        cands, rerank_used, rerank_ms = _maybe_rerank(q, cands, RERANK_TOP)
    else:
        rerank_used = 0
        rerank_ms = 0

    fused: List[SearchHit] = []
    def near(a: SearchHit, b: SearchHit) -> bool:
        return (a.doc_id == b.doc_id) and (abs(a.chunk_index - b.chunk_index) <= 1)

    for cand in cands:
        if any(near(cand, kept) for kept in fused):
            continue
        fused.append(cand)
        if len(fused) >= n:
            break

    if len(fused) < n:
        for cand in cands:
            if cand in fused:
                continue
            fused.append(cand)
            if len(fused) >= n:
                break

    if not fused and kw_hits:
        fused = [SearchHit(**h.dict(), source="fallback") for h in kw_hits[:n]]

    try:
        total_ms = int((time.perf_counter() - T0) * 1000)
        print(
            f"[search] q='{q[:80]}' dept={dept} lang={lang} k={k} n={n} "
            f"alpha={FUSION_ALPHA:.2f} encode_ms={enc_ms} semantic_ms={sem_ms} keyword_ms={kw_ms} "
            f"rerank_used={rerank_used} rerank_ms={rerank_ms} total_ms={total_ms} "
            f"sem_k={len(sem_hits)} kw_k={len(kw_hits)} merged={len(cands)} fused_n={len(fused)}"
        )
    except Exception:
        pass

    return fused

# --- Helpers for answer assembly ---
def _neighbor_text(doc_id: str, center_idx: int, window: int = 1, cap: int = 2000) -> Optional[str]:
    if window <= 0:
        return None
    rows = db_rows("""
        SELECT c.chunk_index, c.text
        FROM chunks c
        JOIN documents d ON d.id = c.document_id
        WHERE d.doc_id = %s AND c.chunk_index BETWEEN %s AND %s
        ORDER BY c.chunk_index
    """, [doc_id, max(0, center_idx - window), center_idx + window])
    if not rows:
        return None
    joined = " ".join((r["text"] or "") for r in rows)
    joined = re.sub(r"\s+", " ", joined).strip()
    if cap and len(joined) > cap:
        joined = joined[:cap]
    return joined

def _terms_from_query(q: str) -> List[str]:
    try:
        toks = tokenize(q) or []
    except Exception:
        toks = []
    if not toks:
        toks = re.findall(r"\w{2,}", q, flags=re.UNICODE)
    seen = set()
    out = []
    for t in sorted(toks, key=lambda s: len(s), reverse=True):
        t = t.strip()
        if len(t) < 2:
            continue
        key = t.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(t)
    return out

def _highlight_terms_md(text: str, terms: List[str]) -> str:
    if not text or not terms:
        return text or ""
    out = text
    for term in terms:
        if not term:
            continue
        pat = re.escape(term)
        has_latin = any('a' <= c.lower() <= 'z' for c in term if c.isalpha())
        flags = re.IGNORECASE if has_latin else 0
        try:
            out = re.sub(pat, lambda m: f"**{m.group(0)}**", out, flags=flags)
        except Exception:
            out = out.replace(term, f"**{term}**")
    return out

def _escape_html(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;").replace("'", "&#39;")

def _highlight_terms_html(text: str, terms: List[str]) -> str:
    if not text:
        return ""
    escaped = _escape_html(text)
    if not terms:
        return escaped
    out = escaped
    for term in terms:
        if not term:
            continue
        pat = re.escape(_escape_html(term))
        has_latin = any('a' <= c.lower() <= 'z' for c in term if c.isalpha())
        flags = re.IGNORECASE if has_latin else 0
        try:
            out = re.sub(pat, lambda m: f"<mark>{m.group(0)}</mark>", out, flags=flags)
        except Exception:
            out = out.replace(_escape_html(term), f"<mark>{_escape_html(term)}</mark>")
    return out

# --- Answer with citations (no LLM) ---
@app.get("/api/answer")
def api_answer(
    q: str = Query(..., min_length=1, description="User query"),
    dept: Optional[str] = Query(None),
    lang: Optional[str] = Query(None),
    k: int = Query(12, ge=3, le=50, description="gather pool for search"),
    n: int = Query(3, ge=1, le=10, description="stitched snippet count"),
    max_chars: int = Query(700, ge=200, le=2000, description="answer character cap"),
    neighbor: int = Query(0, ge=0, le=2, description="stitch ±neighbor chunks for context (default 0)"),
    highlight: int = Query(1, ge=0, le=1, description="bold/mark query terms (default 1)"),
    format: str = Query("text", pattern="^(text|html)$", description="answer format: text (default) or html"),
):
    """
    Builds a short, readable answer from top-N /api/search hits, with [DOC-ID] tags.
    If neighbor>0, pulls ±neighbor chunks from the same doc for fuller context.
    If highlight=1, emphasizes matched query terms.
    If format=html, also returns `answer_html` with <mark> highlights (answer stays text-safe).
    """
    T0 = time.perf_counter()
    hits: List[SearchHit] = api_search(q=q, dept=dept, lang=lang, k=k, n=n)

    terms = _terms_from_query(q) if highlight else []

    used = []
    parts_text = []
    parts_html = []
    budget = max_chars
    for h in hits[:max(1, n)]:
        stitched = _neighbor_text(h.doc_id, h.chunk_index, window=neighbor) if neighbor > 0 else None
        raw = (stitched or h.text or "").strip()
        if not raw:
            continue

        txt = _highlight_terms_md(raw, terms) if terms else raw
        piece_text = f"[{h.doc_id}] {txt}"

        if format == "html":
            html_txt = _highlight_terms_html(raw, terms) if terms else _escape_html(raw)
            piece_html = f"[{_escape_html(h.doc_id)}] {html_txt}"
        else:
            piece_html = None

        # apply cap on text; html mirrors text cap length
        capped = piece_text
        if len(capped) > budget:
            capped = capped[:max(0, budget-1)].rstrip() + "…"
        parts_text.append(capped)

        if piece_html is not None:
            html_capped = piece_html
            if len(piece_text) > budget:  # mirror same truncation decision
                # naive mirror by length of text; safe for demo
                html_capped = html_capped[:max(0, budget-1)] + "…"
            parts_html.append(html_capped)

        used.append(h)
        budget -= len(capped) + 2
        if budget <= 0:
            break

    answer_text = "\n\n—\n\n".join(parts_text) if parts_text else ""
    answer_html = "<hr/>".join(parts_html) if (format == "html" and parts_html) else None

    try:
        total_ms = int((time.perf_counter() - T0) * 1000)
        print(f"[answer] q='{q[:80]}' dept={dept} lang={lang} k={k} n={n} neighbor={neighbor} highlight={highlight} format={format} max_chars={max_chars} total_ms={total_ms}")
    except Exception:
        pass

    resp = {
        "query": q,
        "dept": dept,
        "lang": lang,
        "answer": answer_text,
        "hits": [
            {
                "doc_id": h.doc_id,
                "title": h.title,
                "dept": h.dept,
                "lang": h.lang,
                "chunk_index": h.chunk_index,
                "source": h.source,
                "score": h.score,
            } for h in used
        ],
        "meta": {
            "k": k, "n": n, "max_chars": max_chars, "neighbor": neighbor, "highlight": highlight, "format": format,
        }
    }
    if answer_html is not None:
        resp["answer_html"] = answer_html
    return resp

# --- Startup prewarm (optional via env) ---
@app.on_event("startup")
def _startup_prewarm():
    if PREWARM:
        try:
            t0 = time.perf_counter()
            _ = encode("warmup")[0]
            ms = int((time.perf_counter() - t0) * 1000)
            print(f"[startup] encoder prewarm_ms={ms}")
        except Exception as e:
            print(f"[startup] encoder prewarm skipped: {e}")
    if PREWARM_RERANK and RERANK:
        try:
            t0 = time.perf_counter()
            model = _get_reranker()
            if model:
                _ = model.predict([("warmup", "warmup")], batch_size=1)
                ms = int((time.perf_counter() - t0) * 1000)
                print(f"[startup] reranker prewarm_ms={ms}")
        except Exception as e:
            print(f"[startup] reranker prewarm skipped: {e}")

# Allow "python server_hr.py" local runs
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server_hr:app", host=HOST, port=PORT, reload=True)
