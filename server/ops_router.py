# Path: server/ops_router.py
# Product: HKRNL-KB-WA-FrontEnd / DocuMind-HR
# Purpose: Ops + Data APIs used by OpsDashboard.tsx
# Version: 1.8.2 (2025-09-04)
#   * Re-enabled LLM in Ask path: _gateway_ask_core() now calls get_llm().chat(...) instead of _model_stub(...)
#   * Honors lang ("hi"/"en") and optional timeout_ms & max_tokens in AskRequest (default timeout 60s)
#   * Falls back to local stub only if LLM client is unavailable or returns an error
#   * No other behavior changed for unrelated endpoints

from __future__ import annotations

import os
import math
import datetime as dt
import subprocess
from typing import Any, Dict, List, Optional, Tuple

import psycopg2
from psycopg2.extras import RealDictCursor
from fastapi import APIRouter, HTTPException, Query, Body, Request
from fastapi.responses import Response, RedirectResponse

# NEW: models / rate limit / audit
from pydantic import BaseModel, Field
from collections import deque
import time, json

# Optional (for /api/search proxy patterns)
try:
    import requests  # type: ignore
except Exception:  # pragma: no cover
    requests = None

# LLM client import (support both layouts)
try:
    from services.llm_client import get_llm  # type: ignore
except Exception:  # pragma: no cover
    try:
        from llm_client import get_llm  # type: ignore
    except Exception:
        get_llm = None  # type: ignore

# ---------------------------------------------------------------------------
# ENV / CONFIG
# ---------------------------------------------------------------------------

DSN = (
    os.getenv("DOCUMIND_HR_DSN")
    or os.getenv("DATABASE_URL")
    or "postgresql://postgres:postgres@127.0.0.1:5432/documind_hr"
)

SEARCH_BASE = os.environ.get("SEARCH_BASE", "").rstrip("/")
FILES_DIR   = os.environ.get("FILES_DIR") or os.environ.get("DOCUMIND_FILES_DIR") or ""

MAX_Q_LEN = 512  # /passages & /gateway/ask guardrail
FUSION_ALPHA = float(os.getenv("FUSION_ALPHA", "0.55"))
IVFFLAT_LISTS  = int(os.getenv("IVFFLAT_LISTS",  "200"))
IVFFLAT_PROBES = int(os.getenv("IVFFLAT_PROBES", "15"))

# /gateway/ask rate limit + audit log (in-memory + file)
RATE_WINDOW_SEC = int(os.getenv("GATEWAY_ASK_RATE_WINDOW_SEC", "60"))
RATE_MAX_PER_WINDOW = int(os.getenv("GATEWAY_ASK_RATE_MAX", "20"))
_AUDIT_LOG_PATH = os.environ.get("GATEWAY_ASK_LOG", "gateway_ask.log")
_rate_bucket: Dict[str, deque] = {}

# Global Ask fan-out knobs (safe, small defaults)
FANOUT_MAX_DOCS   = int(os.getenv("GATEWAY_FANOUT_DOCS", "60"))   # scan up to N docs
FANOUT_TAKE_PERDOC = int(os.getenv("GATEWAY_TAKE_PERDOC", "3"))   # take up to M passages per doc

# Ask defaults (safe caps)
ASK_DEFAULT_TIMEOUT_MS = int(os.getenv("GATEWAY_ASK_TIMEOUT_MS", "60000"))  # 60s default
ASK_DEFAULT_MAX_TOKENS = int(os.getenv("GATEWAY_ASK_MAX_TOKENS", "768"))

router = APIRouter()

# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def db():
    return psycopg2.connect(DSN)

_cache: Dict[str, Any] = {}
TABLES = {"documents": "documents", "chunks": "chunks", "ocr_pages": "ocr_pages"}

def table_exists(name: str) -> bool:
    key = f"t:{name}"
    if key in _cache:
        return _cache[key]
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name=%s)",
            (name,),
        )
        ok = bool(cur.fetchone()[0])
        _cache[key] = ok
        return ok

def column_exists(table: str, col: str) -> bool:
    key = f"c:{table}:{col}"
    if key in _cache:
        return _cache[key]
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT EXISTS (
              SELECT 1 FROM information_schema.columns
              WHERE table_name = %s AND column_name = %s
            )
            """,
            (table, col),
        )
        ok = bool(cur.fetchone()[0])
        _cache[key] = ok
        return ok

def pick_col(table: str, candidates: List[str], fallback: Optional[str] = None) -> Optional[str]:
    for c in candidates:
        if column_exists(table, c):
            return c
    return fallback

def get_document_cols() -> Dict[str, Optional[str]]:
    t = TABLES["documents"]
    return {
        "doc_id":      pick_col(t, ["doc_id", "id_external", "document_id", "docid", "id_str"], "doc_id"),
        "title":       pick_col(t, ["title", "name", "doc_title"], None),
        "dept":        pick_col(t, ["dept", "department", "dept_name"], None),
        "lang":        pick_col(t, ["lang", "language"], None),
        "type":        pick_col(t, ["type", "doc_type", "category"], None),
        "created_at":  pick_col(t, ["created_at", "created", "created_on", "timestamp", "ts"], None),
        "path":        pick_col(t, ["path", "file_path", "filepath", "pdf_path", "url"], None),
        "pages":       pick_col(t, ["pages", "page_count", "num_pages"], None),
        "sem_summary": pick_col(t, ["sem_summary", "semantic_summary"], None),
        "llm_summary": pick_col(t, ["llm_summary", "summary_llm", "model_summary"], None),
    }

def get_chunk_cols() -> Tuple[str, Optional[str], str, Optional[str]]:
    # returns (doc_col, idx_col, text_col, embed_col)
    t = TABLES["chunks"]
    doc_col   = pick_col(t, ["doc_id", "document_id", "doc", "docid", "source_doc_id"], "doc_id")
    idx_col   = pick_col(t, ["chunk_index", "index", "idx", "sequence", "chunk_no", "position"], None)
    text_col  = pick_col(t, ["text", "chunk_text", "content", "body", "text_content"], "text")
    embed_col = pick_col(t, ["embedding", "vector", "embed"], None)  # pgvector column if present
    return doc_col, idx_col, text_col, embed_col

def get_ocr_cols() -> Dict[str, Optional[str]]:
    t = TABLES["ocr_pages"]
    return {
        "doc_id": pick_col(t, ["doc_id", "document_id", "docid"], "document_id"),
        "page":   pick_col(t, ["page", "page_no", "pageno", "page_index"], "page_no"),
        "text":   pick_col(t, ["text", "content", "body"], "text"),
    }

# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def _rel_to_files(path: str) -> str:
    """Return a path relative to FILES_DIR so /files/<rel> will serve it."""
    candidates = []
    if FILES_DIR:
        candidates.append(FILES_DIR)
    candidates += ["/data", "data", os.path.join(os.getcwd(), "data")]

    path = path.strip()
    for base in candidates:
        if base and path.startswith(base.rstrip("/") + "/"):
            rel = os.path.relpath(path, base).replace("\\", "/")
            return rel.lstrip("/")
    return path.lstrip("/")

def _ensure_pdf_path(path_or_dir: str, title: Optional[str], doc_id: str) -> str:
    p = path_or_dir.rstrip("/")
    if p.lower().endswith(".pdf"):
        return p
    if title and title.lower().endswith(".pdf"):
        name = title
    elif title:
        name = f"{title}.pdf"
    else:
        name = f"{doc_id}.pdf"
    return f"{p}/{name}"

# ---------------------------------------------------------------------------
# Proxy helper
# ---------------------------------------------------------------------------

def _proxy_get(path: str, params: dict):
    if not SEARCH_BASE:
        raise HTTPException(404, detail="SEARCH_BASE not configured")
    if requests is None:
        raise HTTPException(500, detail="requests package not installed (pip install requests)")
    url = f"{SEARCH_BASE}{path}"
    r = requests.get(url, params=params, timeout=60)
    return r.status_code, r.headers.get("content-type", "application/json"), r.text

# ---------------------------------------------------------------------------
# /api/stats
# ---------------------------------------------------------------------------

@router.get("/api/stats")
def api_stats(request: Request) -> Dict[str, Any]:
    docs = chunks = embeds = ocr_pages = 0
    features = {"encoding": True, "pgvector": True, "pg_trgm": True}

    if table_exists(TABLES["documents"]):
        with db() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM {TABLES['documents']}")
            docs = cur.fetchone()[0]

    if table_exists(TABLES["chunks"]):
        with db() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM {TABLES['chunks']}")
            chunks = cur.fetchone()[0]
            _, _, _, embed_col = get_chunk_cols()
            if embed_col:
                cur.execute(f"SELECT COUNT(*) FROM {TABLES['chunks']} WHERE {embed_col} IS NOT NULL")
                embeds = cur.fetchone()[0]
            else:
                embeds = chunks

    if table_exists(TABLES["ocr_pages"]):
        with db() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM {TABLES['ocr_pages']}")
            ocr_pages = cur.fetchone()[0]

    env = {
        "fusion_alpha": FUSION_ALPHA,
        "ivfflat_lists": IVFFLAT_LISTS,
        "ivfflat_probes": IVFFLAT_PROBES,
    }

    # rolling latency from main.py middleware (if present)
    avg_routes_ms: Dict[str, float] = {}
    total_avg = 0.0
    lat = getattr(request.app.state, "_latency", None)
    if isinstance(lat, dict) and lat:
        for path, dq in lat.items():
            try:
                if dq:
                    avg_routes_ms[path] = round(sum(dq)/len(dq), 2)
            except Exception:
                continue
        if avg_routes_ms:
            total_avg = round(sum(avg_routes_ms.values())/len(avg_routes_ms), 2)

    avg_ms = {"encode": 0, "semantic": 0, "keyword": 0, "rerank": 0, "total": total_avg}

    return {
        "uptime_ms": int(dt.timedelta(days=1).total_seconds() * 1000),
        "counts": {
            "documents":     docs,
            "chunks":        chunks,
            "embeddings":    embeds,
            "ocr_pages":     ocr_pages,
            "sem_summaries": 0,
            "llm_summaries": 0,
        },
        "features": features,
        "env": env,
        "avg_ms": avg_ms,
        "avg_routes_ms": avg_routes_ms,
        "errors_24h": 0,
    }

# ---------------------------------------------------------------------------
# /api/docs (list)
# ---------------------------------------------------------------------------

@router.get("/api/docs")
def list_docs(
    q: str = Query(""),
    dept: str = Query(""),
    lang: str = Query(""),
    type: str = Query(""),
    from_: str = Query("", alias="from"),
    to: str = Query(""),
    limit: int = 10,
    page: int = 1,
) -> Dict[str, Any]:
    if not table_exists(TABLES["documents"]):
        return {"total": 0, "page": 1, "limit": limit, "items": []}

    dcols = get_document_cols()

    where, params = [], []

    def push(cond: Optional[str], val: Any):
        if cond is not None:
            where.append(cond)
            params.append(val)

    if q and dcols["title"]:
        push(f"{dcols['title']} ILIKE %s", f"%{q}%")
    if dept and dcols["dept"]:
        push(f"{dcols['dept']} = %s", dept)
    if lang and dcols["lang"]:
        push(f"{dcols['lang']} = %s", lang)
    if type and dcols["type"]:
        push(f"{dcols['type']} = %s", type)
    if from_ and dcols["created_at"]:
        push(f"{dcols['created_at']} >= %s", from_)
    if to and dcols["created_at"]:
        push(f"{dcols['created_at']} < %s", to)

    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(f"SELECT COUNT(*) AS c FROM {TABLES['documents']}{where_sql}", params)
        total = int(cur.fetchone()["c"]) if cur.rowcount is not None else 0

        offset = max(0, (page - 1) * max(1, limit))

        select_cols = [f"{dcols['doc_id']} AS doc_id"]
        for alias in ["title", "dept", "lang", "type", "created_at", "path", "pages", "sem_summary", "llm_summary"]:
            col = dcols.get(alias)
            if col:
                select_cols.append(f"{col} AS {alias}")
        col_sql = ", ".join(select_cols)

        with db().cursor(cursor_factory=RealDictCursor) as cur2:
            cur2.execute(
                f"SELECT {col_sql} FROM {TABLES['documents']}{where_sql} ORDER BY {dcols['doc_id']} LIMIT %s OFFSET %s",
                params + [limit, offset],
            )
            rows = cur2.fetchall()

    # preload OCR counts
    ocr_by_doc: Dict[str, int] = {}
    if table_exists(TABLES["ocr_pages"]) and rows:
        o = get_ocr_cols()
        ids = [str(r.get("doc_id")) for r in rows]
        with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                f"""
                SELECT {o['doc_id']}::text AS doc_id, COUNT(*) AS n
                FROM {TABLES['ocr_pages']}
                WHERE {o['doc_id']}::text = ANY(%s)
                GROUP BY {o['doc_id']}
                """,
                (ids,),
            )
            for r in cur.fetchall():
                ocr_by_doc[str(r["doc_id"])] = int(r["n"])

    # preload chunk/embed counts
    counts_by_doc: Dict[str, Dict[str, int]] = {}
    if table_exists(TABLES["chunks"]) and rows:
        c_doc, c_idx, c_text, c_embed = get_chunk_cols()
        d = get_document_cols()
        ids = [str(r.get("doc_id")) for r in rows]
        with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            if c_embed:
                cur.execute(
                    f"""
                    SELECT d.{d['doc_id']}::text AS doc_id,
                           COUNT(*) AS chunk_count,
                           SUM(CASE WHEN {c_embed} IS NOT NULL THEN 1 ELSE 0 END) AS embed_count
                    FROM {TABLES['chunks']} c
                    JOIN {TABLES['documents']} d
                      ON ({c_doc} = d.id OR {c_doc}::text = d.{d['doc_id']}::text)
                    WHERE d.{d['doc_id']}::text = ANY(%s)
                    GROUP BY d.{d['doc_id']}
                    """,
                    (ids,),
                )
            else:
                cur.execute(
                    f"""
                    SELECT d.{d['doc_id']}::text AS doc_id,
                           COUNT(*) AS chunk_count,
                           COUNT(*) AS embed_count
                    FROM {TABLES['chunks']} c
                    JOIN {TABLES['documents']} d
                      ON ({c_doc} = d.id OR {c_doc}::text = d.{d['doc_id']}::text)
                    WHERE d.{d['doc_id']}::text = ANY(%s)
                    GROUP BY d.{d['doc_id']}
                    """,
                    (ids,),
                )
            for r in cur.fetchall():
                counts_by_doc[str(r["doc_id"])] = {
                    "chunk_count": int(r["chunk_count"]),
                    "embed_count": int(r["embed_count"]),
                }

    # build items
    items: List[Dict[str, Any]] = []
    for r in rows:
        doc_id = str(r.get("doc_id"))
        title  = r.get("title") or doc_id
        dept_v = r.get("dept") or ""
        lang_v = r.get("lang") or ""
        type_v = r.get("type") or ""
        created_at_v = r.get("created_at")
        pages  = r.get("pages")
        semsum = r.get("sem_summary")
        llmsum = r.get("llm_summary")

        ocr_count = ocr_by_doc.get(doc_id, 0)
        chunk_stats = counts_by_doc.get(doc_id, {"chunk_count": 0, "embed_count": 0})
        c_count = chunk_stats["chunk_count"]
        e_count = chunk_stats["embed_count"]

        items.append({
            "doc_id": doc_id,
            "title": title,
            "dept": dept_v,
            "lang": lang_v,
            "type": type_v,
            "created_at": created_at_v,
            "stages": {
                "pdf":   {"state": "done", "pages": pages} if pages is not None else {"state": "done"},
                "ocr":   {"state": "done" if ocr_count > 0 else "pending"},
                "text":  {"state": "done" if (ocr_count > 0 or c_count > 0) else "pending"},
                "chunks":{"state": "done" if c_count > 0 else "none", "count": c_count},
                "embeds":{"state": "done" if (c_count > 0 and e_count >= c_count) else ("pending" if c_count > 0 else "none"), "count": e_count},
                "sem_summary": {"state": "db" if semsum else "none"},
                "llm_summary": {"state": "db" if llmsum else "none"},
            },
        })

    return {"total": total, "page": page, "limit": limit, "items": items}

# ---------------------------------------------------------------------------
# /api/docs/{id}/ocr
# ---------------------------------------------------------------------------

@router.get("/api/docs/{doc_id}/ocr")
def get_ocr(doc_id: str):
    text, pages = "", 0

    if table_exists(TABLES["ocr_pages"]):
        o = get_ocr_cols()
        with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                f"SELECT {o['page']} AS page, {o['text']} AS text "
                f"FROM {TABLES['ocr_pages']} WHERE {o['doc_id'] }::text = %s "
                f"ORDER BY {o['page']}",
                (str(doc_id),),
            )
            rows = cur.fetchall()
            if rows:
                pages = len(rows)
                text = "\n\n".join([r.get("text") or "" for r in rows])

    if not text and table_exists(TABLES["chunks"]):
        c_doc, c_idx, c_text, _ = get_chunk_cols()
        order_sql = f"ORDER BY {c_idx} NULLS FIRST" if c_idx else "ORDER BY 1"
        with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                f"SELECT {c_text} AS text FROM {TABLES['chunks']} "
                f"WHERE {c_doc}::text = %s {order_sql} LIMIT 200",
                (str(doc_id),),
            )
            rows = cur.fetchall()
            text = "\n\n".join([r.get("text") or "" for r in rows])

    return {"text": text, "pages": pages or None, "chars": len(text or "")}

# =============================================================================
# Helpers for chunks: external doc_id (TEXT) → documents.id (UUID)
# =============================================================================

def _get_internal_uuid_for_doc(doc_id: str) -> Optional[str]:
    if not table_exists(TABLES["documents"]):
        return None
    with db() as conn, conn.cursor() as cur:
        try:
            cur.execute("SELECT id FROM documents WHERE doc_id = %s LIMIT 1;", (doc_id,))
            row = cur.fetchone()
            return row[0] if row else None
        except Exception:
            return None

def _count_chunks_for_doc(doc_id: str) -> int:
    if not table_exists(TABLES["chunks"]):
        return 0
    c_doc, _, _, _ = get_chunk_cols()
    with db() as conn, conn.cursor() as cur:
        doc_uuid = _get_internal_uuid_for_doc(doc_id)
        if doc_uuid:
            cur.execute(f"SELECT COUNT(*) FROM {TABLES['chunks']} WHERE {c_doc} = %s;", (doc_uuid,))
            n = cur.fetchone()[0]
            if n:
                return n
        cur.execute(f"SELECT COUNT(*) FROM {TABLES['chunks']} WHERE {c_doc}::text = %s;", (str(doc_id),))
        return cur.fetchone()[0]

# =============================================================================
# /api/docs/{id}/meta
# =============================================================================

@router.get("/api/docs/{doc_id}/meta")
def doc_meta(doc_id: str):
    chunks = _count_chunks_for_doc(doc_id)
    embeds = 0
    ocrp   = 0

    if table_exists(TABLES["chunks"]):
        c_doc, _, _, c_embed = get_chunk_cols()
        with db() as conn, conn.cursor() as cur:
            doc_uuid = _get_internal_uuid_for_doc(doc_id)
            if c_embed:
                if doc_uuid:
                    cur.execute(f"SELECT COUNT(*) FROM {TABLES['chunks']} WHERE {c_doc} = %s AND {c_embed} IS NOT NULL;", (doc_uuid,))
                    embeds = cur.fetchone()[0]
                if embeds == 0:
                    cur.execute(f"SELECT COUNT(*) FROM {TABLES['chunks']} WHERE {c_doc}::text = %s AND {c_embed} IS NOT NULL;", (str(doc_id),))
                    embeds = cur.fetchone()[0]
            else:
                embeds = chunks

    if table_exists(TABLES["ocr_pages"]):
        o = get_ocr_cols()
        with db() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM {TABLES['ocr_pages']} WHERE {o['doc_id']}::text = %s;", (str(doc_id),))
            ocrp = cur.fetchone()[0]

    return {"doc_id": doc_id, "chunks": int(chunks), "embeds": int(embeds), "ocr_pages": int(ocrp)}

# =============================================================================
# /api/docs/{id}/passages — supports optional q for ranking (pg_trgm)
# =============================================================================

@router.get("/api/docs/{doc_id}/passages")
def get_doc_passages(
    doc_id: str,
    limit: int = Query(10, ge=1, le=200, description="Max passages to return"),
    q: str = Query("", description="Optional query to rank by pg_trgm similarity"),
    offset: int = Query(0, ge=0, description="Offset for pagination"),
):
    """
    Returns chunk previews.
    When q != "", includes `score` and orders by similarity DESC then chunk_index.
    Otherwise, orders by chunk_index and `score` is null.
    Contract (list): [{chunk_index, score|null, preview, chars}]
    """
    if q and len(q) > MAX_Q_LEN:
        raise HTTPException(status_code=400, detail=f"q too long (max {MAX_Q_LEN})")

    if not table_exists(TABLES["chunks"]):
        return []

    c_doc, c_idx, c_text, _ = get_chunk_cols()
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        doc_uuid = _get_internal_uuid_for_doc(doc_id)

        if q:
            # Ranked by pg_trgm similarity(text, q)
            if doc_uuid:
                cur.execute(
                    f"""
                    SELECT
                      { (f"{c_idx} AS chunk_index," if c_idx else "NULL::int AS chunk_index,") }
                      similarity({c_text}, %s) AS score,
                      LEFT({c_text}, 320) AS preview,
                      CHAR_LENGTH({c_text}) AS chars
                    FROM {TABLES['chunks']}
                    WHERE {c_doc} = %s
                    ORDER BY score DESC, {c_idx if c_idx else '1'}
                    LIMIT %s OFFSET %s;
                    """,
                    (q, doc_uuid, limit, offset),
                )
                rows = cur.fetchall()
                if rows:
                    return rows

            cur.execute(
                f"""
                SELECT
                  { (f"{c_idx} AS chunk_index," if c_idx else "NULL::int AS chunk_index,") }
                  similarity({c_text}, %s) AS score,
                  LEFT({c_text}, 320) AS preview,
                  CHAR_LENGTH({c_text}) AS chars
                FROM {TABLES['chunks']}
                WHERE {c_doc}::text = %s
                ORDER BY score DESC, {c_idx if c_idx else '1'}
                LIMIT %s OFFSET %s;
                """,
                (q, str(doc_id), limit, offset),
            )
            return cur.fetchall()

        # Unranked
        order_sql = f"ORDER BY {c_idx} NULLS FIRST" if c_idx else "ORDER BY 1"
        select_sql = (
            (f"{c_idx} AS chunk_index, " if c_idx else "NULL::int AS chunk_index, ")
            + "NULL::float8 AS score, "
            + f"LEFT({c_text}, 320) AS preview, CHAR_LENGTH({c_text}) AS chars"
        )
        if doc_uuid:
            cur.execute(
                f"SELECT {select_sql} FROM {TABLES['chunks']} WHERE {c_doc} = %s {order_sql} LIMIT %s OFFSET %s;",
                (doc_uuid, limit, offset),
            )
            rows = cur.fetchall()
            if rows:
                return rows
        cur.execute(
            f"SELECT {select_sql} FROM {TABLES['chunks']} WHERE {c_doc}::text = %s {order_sql} LIMIT %s OFFSET %s;",
            (str(doc_id), limit, offset),
        )
        return cur.fetchall()

# =============================================================================
# /api/docs/{doc_id}/chunk/{chunk_index}
# =============================================================================

@router.get("/api/docs/{doc_id}/chunk/{chunk_index}")
def get_one_chunk(doc_id: str, chunk_index: int):
    if not table_exists(TABLES["chunks"]):
        raise HTTPException(404, "chunks table missing")
    c_doc, c_idx, c_text, c_embed = get_chunk_cols()
    if not c_idx:
        raise HTTPException(400, "chunk_index column missing in chunks schema")

    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        doc_uuid = _get_internal_uuid_for_doc(doc_id)
        if doc_uuid:
            cur.execute(
                f"""
                SELECT {c_idx} AS chunk_index,
                       CHAR_LENGTH({c_text}) AS chars,
                       ({c_embed} IS NOT NULL) AS has_embed,
                       {c_text} AS text
                FROM {TABLES['chunks']}
                WHERE {c_doc} = %s AND {c_idx} = %s
                LIMIT 1;
                """,
                (doc_uuid, chunk_index),
            )
            row = cur.fetchone()
            if row:
                return row
        cur.execute(
            f"""
            SELECT {c_idx} AS chunk_index,
                   CHAR_LENGTH({c_text}) AS chars,
                   ({c_embed} IS NOT NULL) AS has_embed,
                   {c_text} AS text
            FROM {TABLES['chunks']}
            WHERE {c_doc}::text = %s AND {c_idx} = %s
            LIMIT 1;
            """,
            (str(doc_id), chunk_index),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "chunk not found")
        return row

# ---------------------------------------------------------------------------
# /api/docs/{id}/summary  (semantic stitch; optional save)
# ---------------------------------------------------------------------------

@router.get("/api/docs/{doc_id}/summary")
def semantic_summary(doc_id: str, mode: str = "semantic", k: int = 5, probes: int = 11, save: bool = False):
    if mode != "semantic":
        raise HTTPException(400, "Only mode=semantic is supported here")

    if not table_exists(TABLES["chunks"]):
        return {"source": "generated", "text": "", "k": k, "probes": probes}

    _c_doc, c_idx, c_text, _ = get_chunk_cols()
    order_sql = f"ORDER BY {c_idx} NULLS FIRST" if c_idx else "ORDER BY 1"
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        doc_uuid = _get_internal_uuid_for_doc(doc_id)
        if doc_uuid:
            cur.execute(
                f"SELECT {c_text} AS text FROM {TABLES['chunks']} WHERE {_c_doc} = %s {order_sql} LIMIT %s;",
                (doc_uuid, k),
            )
            rows = cur.fetchall()
        else:
            rows = []
        if not rows:
            cur.execute(
                f"SELECT {c_text} AS text FROM {TABLES['chunks']} WHERE {_c_doc}::text = %s {order_sql} LIMIT %s;",
                (str(doc_id), k),
            )
            rows = cur.fetchall()

    text = "\n\n".join([r.get("text") or "" for r in rows]) if rows else ""
    summary = text[:4000]
    source = "generated"

    dcols = get_document_cols()
    if save and summary and dcols["sem_summary"]:
        with db() as conn, conn.cursor() as cur:
            cur.execute(
                f"UPDATE {TABLES['documents']} SET {dcols['sem_summary']} = %s "
                f"WHERE {dcols['doc_id']}::text = %s",
                (summary, str(doc_id)),
            )
            conn.commit()
        source = "db"

    return {"source": source, "text": summary, "k": k, "probes": probes}

# ---------------------------------------------------------------------------
# NEW: LLM preview_v2 (chunks-only, UI-controlled timeout)
# ---------------------------------------------------------------------------

@router.post("/api/docs/{doc_id}/llm_summarize/preview_v2")
async def llm_preview_v2(doc_id: str, request: Request):
    """
    Safer LLM preview that *always* uses DB chunks (never raw OCR),
    honors UI overrides (timeout_ms, max_tokens, topk/percent_cap, groundedness),
    and returns useful meta + headers for the SummarizeLLM modal.
    """
    if get_llm is None:
        raise HTTPException(500, "LLM client not available")

    t0 = time.time()
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    style = (payload.get("style") or "bullet").strip()
    lang  = (str(payload.get("lang") or "en")).strip().lower()

    overrides = payload.get("overrides") or {}

    def _to_int(x, default=None):
        try:
            return int(x)
        except Exception:
            return default

    # Controls from UI (with safe defaults)
    topk = _to_int(overrides.get("topk") or overrides.get("top_k_chunks"), 6)
    if topk is None or topk <= 0:
        topk = 6
    topk = max(1, min(50, topk))

    percent_cap = _to_int(overrides.get("percent_cap"), 100)
    if percent_cap is None or percent_cap <= 0:
        percent_cap = 100
    percent_cap = max(1, min(100, percent_cap))

    max_tokens = _to_int(overrides.get("max_tokens"), 768)
    max_tokens = max(64, min(4096, max_tokens))

    timeout_ms = _to_int(overrides.get("timeout_ms"), 60000)  # DEFAULT 60s
    timeout_ms = max(5000, min(180000, timeout_ms))  # 5s..3m guardrail

    # Fetch chunks in natural order
    if not table_exists(TABLES["chunks"]):
        raise HTTPException(404, "chunks table not found")

    c_doc, c_idx, c_text, _ = get_chunk_cols()
    order_sql = f"ORDER BY {c_idx} NULLS FIRST" if c_idx else "ORDER BY 1"
    rows: list = []
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        doc_uuid = _get_internal_uuid_for_doc(doc_id)
        if doc_uuid:
            cur.execute(
                f"SELECT {c_text} AS text FROM {TABLES['chunks']} WHERE {c_doc} = %s {order_sql} LIMIT %s;",
                (doc_uuid, topk),
            )
            rows = cur.fetchall()
        if not rows:
            cur.execute(
                f"SELECT {c_text} AS text FROM {TABLES['chunks']} WHERE {c_doc}::text = %s {order_sql} LIMIT %s;",
                (str(doc_id), topk),
            )
            rows = cur.fetchall()

    used_k = len(rows)
    src = "\n\n".join([(r.get("text") or "").strip() for r in rows])

    # Apply percent cap (by characters)
    if percent_cap < 100:
        cap_chars = math.floor(len(src) * (percent_cap / 100.0))
        src = src[:cap_chars]
    src_chars = len(src)

    # Call LLM with per-request timeout
    try:
        summary = await get_llm().summarize(
            text=src,
            style=style,
            lang=lang,
            max_tokens=max_tokens,
            timeout_ms=timeout_ms,  # <-- UI-controlled (default 60s)
        )
        status = "ok"
    except Exception:
        # Graceful fallback (empty summary) but retain meta
        summary = ""
        status = "fallback"

    took_ms = int((time.time() - t0) * 1000)

    meta = {
        "source_effective": "chunks",
        "k_used": used_k,
        "src_chars": src_chars,
        "src_preview": src[:320],
        "lang": lang,  # <-- expose effective language back to UI
        "overrides": {
            "topk": topk,
            "percent_cap": percent_cap,
            "max_tokens": max_tokens,
            "timeout_ms": timeout_ms,
        },
        "status": status,
        "took_ms": took_ms,
    }

    body = {"summary": summary, "meta": meta}
    content = json.dumps(body, ensure_ascii=False)

    headers = {
        "X-Response-Time-ms": str(took_ms),
        "X-LLM-Source": "chunks",
        "X-LLM-Status": status,
        "X-LLM-TopK-Used": str(used_k),
        "X-LLM-PercentCap-Used": str(percent_cap),
        "X-LLM-Overrides": json.dumps(meta["overrides"]),
        "X-LLM-Lang": lang,  # <-- header for quick verification in DevTools
    }

    return Response(content=content, media_type="application/json", headers=headers)

# ---------------------------------------------------------------------------
# ==== SEMANTIC & HYBRID SEARCH ACROSS DOCS
# ---------------------------------------------------------------------------

def _vec_literal(vec: List[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in vec) + "]"

def _encode_query(request: Request, text: str) -> Optional[List[float]]:
    try:
        enc = getattr(request.app.state, "encode", None)
        if callable(enc):
            v = enc(text)
            if isinstance(v, (list, tuple)) and len(v) > 0:
                return list(map(float, v))
    except Exception:
        pass
    return None

def _doc_filters_sql(dcols: Dict[str, Optional[str]], dept: str, lang: str, params_out: List[Any]) -> str:
    where = []
    if dept and dcols["dept"]:
        where.append(f"d.{dcols['dept']} = %s")
        params_out.append(dept)
    if lang and dcols["lang"]:
        where.append(f"d.{dcols['lang']} = %s")
        params_out.append(lang)
    return (" AND " + " AND ".join(where)) if where else ""

@router.get("/api/search/semantic")
def search_semantic(request: Request,
                    q: str = Query(..., min_length=1),
                    dept: str = "", lang: str = "",
                    k: int = Query(50, ge=1, le=200),
                    offset: int = Query(0, ge=0)):
    if len(q) > MAX_Q_LEN:
        raise HTTPException(400, f"q too long (max {MAX_Q_LEN})")

    if not table_exists(TABLES["chunks"]):
        return []

    c_doc, c_idx, c_text, c_embed = get_chunk_cols()
    dcols = get_document_cols()

    # Try vector path
    qvec = None
    if c_embed:
        qvec = _encode_query(request, q)

    params_filters: List[Any] = []
    filters_sql = _doc_filters_sql(dcols, dept, lang, params_filters)

    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        if qvec and c_embed:
            try:
                cur.execute(f"SET LOCAL ivfflat.probes = {IVFFLAT_PROBES};")
            except Exception:
                pass
            vec = _vec_literal(qvec)
            cur.execute(
                f"""
                SELECT d.{dcols['doc_id']}::text AS doc_id,
                       {c_idx if c_idx else 'NULL'} AS chunk_index,
                       1 - ({c_embed} <=> %s::vector) AS score,
                       LEFT({c_text}, 320) AS preview,
                       CHAR_LENGTH({c_text}) AS chars
                FROM {TABLES['chunks']} c
                JOIN {TABLES['documents']} d
                  ON ({c_doc}::text = d.id::text OR {c_doc}::text = d.{dcols['doc_id']}::text)
                WHERE {c_embed} IS NOT NULL {filters_sql}
                ORDER BY {c_embed} <=> %s::vector
                LIMIT %s OFFSET %s;
                """,
                params_filters + [vec, vec, k, offset],
            )
            return cur.fetchall()

        # Fallback: keyword similarity when no vector path available
        cur.execute(
            f"""
            SELECT d.{dcols['doc_id']}::text AS doc_id,
                   {c_idx if c_idx else 'NULL'} AS chunk_index,
                   similarity({c_text}, %s) AS score,
                   LEFT({c_text}, 320) AS preview,
                   CHAR_LENGTH({c_text}) AS chars
            FROM {TABLES['chunks']}
            JOIN {TABLES['documents']} d
              ON ({c_doc}::text = d.id::text OR {c_doc}::text = d.{dcols['doc_id']}::text)
            WHERE 1=1 {filters_sql}
            ORDER BY score DESC
            LIMIT %s OFFSET %s;
            """,
            [q] + params_filters + [k, offset],
        )
        return cur.fetchall()

@router.get("/api/search/hybrid")
def search_hybrid(request: Request,
                  q: str = Query(..., min_length=1),
                  dept: str = "", lang: str = "",
                  k: int = Query(50, ge=1, le=200),
                  alpha: float = Query(FUSION_ALPHA, ge=0.0, le=1.0),
                  offset: int = Query(0, ge=0)):
    """score = alpha * sem_norm + (1-alpha) * kw_norm"""
    if len(q) > MAX_Q_LEN:
        raise HTTPException(400, f"q too long (max {MAX_Q_LEN})")

    # semantic pool
    sem_rows = search_semantic(request, q=q, dept=dept, lang=lang, k=min(2*k, 200), offset=offset)

    # keyword pool
    c_doc, c_idx, c_text, _ = get_chunk_cols()
    dcols = get_document_cols()
    params_filters: List[Any] = []
    filters_sql = _doc_filters_sql(dcols, dept, lang, params_filters)

    with db() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT d.{dcols['doc_id']}::text AS doc_id,
                   {c_idx if c_idx else 'NULL'} AS chunk_index,
                   similarity({c_text}, %s) AS score,
                   LEFT({c_text}, 320) AS preview,
                   CHAR_LENGTH({c_text}) AS chars
            FROM {TABLES['chunks']}
            JOIN {TABLES['documents']} d
              ON ({c_doc}::text = d.id::text OR {c_doc}::text = d.{dcols['doc_id']}::text)
            WHERE 1=1 {filters_sql}
            ORDER BY score DESC
            LIMIT %s OFFSET %s;
            """,
            [q] + params_filters + [min(2*k, 200), offset],
        )
        kw_rows = cur.fetchall()

    # Normalize & fuse
    def norm(scores: List[float]) -> Dict[int, float]:
        if not scores:
            return {}
        lo, hi = min(scores), max(scores)
        if hi <= lo:
            return {i: 0.0 for i in range(len(scores))}
        return {i: (scores[i]-lo)/(hi-lo) for i in range(len(scores))}

    pool: Dict[Tuple[str, Optional[int]], Dict[str, Any]] = {}
    sem_scores = norm([float(r["score"] or 0.0) for r in sem_rows])
    for i, r in enumerate(sem_rows):
        key = (str(r["doc_id"]), int(r["chunk_index"]) if r["chunk_index"] is not None else None)
        pool[key] = {
            "doc_id": key[0],
            "chunk_index": key[1],
            "sem": float(r["score"] or 0.0),
            "kw": 0.0,
            "preview": r.get("preview") or "",
            "chars": r.get("chars"),
            "sem_n": sem_scores.get(i, 0.0),
            "kw_n": 0.0,
        }

    kw_scores = norm([float(r["score"] or 0.0) for r in kw_rows])
    for i, r in enumerate(kw_rows):
        key = (str(r["doc_id"]), int(r["chunk_index"]) if r["chunk_index"] is not None else None)
        if key in pool:
            pool[key]["kw"] = float(r["score"] or 0.0)
            pool[key]["kw_n"] = kw_scores.get(i, 0.0)
            if not pool[key]["preview"]:
                pool[key]["preview"] = r.get("preview") or ""
            if not pool[key]["chars"]:
                pool[key]["chars"] = r.get("chars")
        else:
            pool[key] = {
                "doc_id": key[0], "chunk_index": key[1],
                "sem": 0.0, "kw": float(r["score"] or 0.0),
                "sem_n": 0.0, "kw_n": kw_scores.get(i, 0.0),
                "preview": r.get("preview") or "",
                "chars": r.get("chars"),
            }

    fused = []
    for v in pool.values():
        v["score"] = alpha * float(v.get("sem_n", 0.0)) + (1.0 - alpha) * float(v.get("kw_n", 0.0))
        fused.append(v)

    fused.sort(key=lambda x: x["score"], reverse=True)
    out = [
        {
            "doc_id": r["doc_id"],
            "chunk_index": r["chunk_index"],
            "score": round(float(r["score"]), 6),
            "preview": r.get("preview") or "",
            "chars": r.get("chars"),
        }
        for r in fused[:k]
    ]
    return out

# ---------------------------------------------------------------------------
# /api/docs/{doc_id}/pdf
# ---------------------------------------------------------------------------

@router.get("/api/docs/{doc_id}/pdf")
@router.head("/api/docs/{doc_id}/pdf")
def open_pdf(doc_id: str):
    dcols = get_document_cols()
    if not dcols["path"]:
        raise HTTPException(404, "No path column; cannot serve PDF")
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT {dcols['path']}, {dcols['title']}, {dcols['dept']}, {dcols['lang']} "
            f"FROM {TABLES['documents']} WHERE {dcols['doc_id']}::text = %s",
            (str(doc_id),),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "document not found")
        path, title, dept, lang = row

    if not path:
        # conventional fallback
        if title and str(title).lower().endswith(".pdf"):
            candidate = f"pdfs/{dept}/{lang}/{title}"
        elif title:
            candidate = f"pdfs/{dept}/{lang}/{title}.pdf"
        else:
            candidate = f"pdfs/{dept}/{lang}/{doc_id}.pdf"
        return RedirectResponse(url="/files/" + candidate.lstrip("/"), status_code=307)

    safe_path = _ensure_pdf_path(str(path), str(title) if title else None, str(doc_id))
    rel = _rel_to_files(safe_path)
    return RedirectResponse(url="/files/" + rel, status_code=307)

# ---------------------------------------------------------------------------
# /gateway/ask — deterministic cites (≤5), rate-limited, audited
# + /api/gateway/ask alias for proxies that only pass /api/*
# ---------------------------------------------------------------------------

class AskRequest(BaseModel):
    q: str = Field("", max_length=MAX_Q_LEN)
    dept: Optional[str] = None
    lang: Optional[str] = None
    k: int = 3
    neighbor: int = 0
    doc_id: Optional[str] = None
    # New, backward-compatible knobs:
    timeout_ms: Optional[int] = Field(None, ge=1000, le=180000)
    max_tokens: Optional[int] = Field(None, ge=32, le=4096)

class AskResponse(BaseModel):
    answer: str
    cites: List[Dict[str, Any]]

def _rate_check(ip: str) -> None:
    now = time.time()
    bucket = _rate_bucket.setdefault(ip, deque())
    while bucket and (now - bucket[0]) > RATE_WINDOW_SEC:
        bucket.popleft()
    if len(bucket) >= RATE_MAX_PER_WINDOW:
        raise HTTPException(status_code=429, detail="Too many requests")
    bucket.append(now)

def _audit_log(entry: Dict[str, Any]) -> None:
    try:
        line = json.dumps(entry, ensure_ascii=False)
        with open(_AUDIT_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        # non-fatal
        pass

def _retrieve_rows_for_doc(doc_id: str, q: str, probe: int) -> List[Dict[str, Any]]:
    """
    Fetch top passages for a doc. Ranked when q != "", otherwise by chunk order.
    Returns list of dicts with keys: chunk_index, preview, score?
    """
    if not table_exists(TABLES["chunks"]):
        return []

    c_doc, c_idx, c_text, _ = get_chunk_cols()
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        doc_uuid = _get_internal_uuid_for_doc(doc_id)

        if q:
            if doc_uuid:
                cur.execute(
                    f"""
                    SELECT {c_idx if c_idx else 'NULL'} AS chunk_index,
                           similarity({c_text}, %s) AS score,
                           LEFT({c_text}, 480) AS preview
                    FROM {TABLES['chunks']}
                    WHERE {c_doc} = %s
                    ORDER BY score DESC, {c_idx if c_idx else '1'}
                    LIMIT %s;
                    """,
                    (q, doc_uuid, probe),
                )
                rows = cur.fetchall()
                if rows:
                    return rows
            cur.execute(
                f"""
                SELECT {c_idx if c_idx else 'NULL'} AS chunk_index,
                       similarity({c_text}, %s) AS score,
                       LEFT({c_text}, 480) AS preview
                FROM {TABLES['chunks']}
                WHERE {c_doc}::text = %s
                ORDER BY score DESC, {c_idx if c_idx else '1'}
                LIMIT %s;
                """,
                (q, str(doc_id), probe),
            )
            return cur.fetchall()

        # q is empty → unranked by chunk order
        order_sql = f"ORDER BY {c_idx} NULLS FIRST" if c_idx else "ORDER BY 1"
        if doc_uuid:
            cur.execute(
                f"""
                SELECT {c_idx if c_idx else 'NULL'} AS chunk_index,
                       NULL::float8 AS score,
                       LEFT({c_text}, 480) AS preview
                FROM {TABLES['chunks']}
                WHERE {c_doc} = %s
                {order_sql}
                LIMIT %s;
                """,
                (doc_uuid, probe),
            )
            rows = cur.fetchall()
            if rows:
                return rows
        cur.execute(
            f"""
            SELECT {c_idx if c_idx else 'NULL'} AS chunk_index,
                   NULL::float8 AS score,
                   LEFT({c_text}, 480) AS preview
            FROM {TABLES['chunks']}
            WHERE {c_doc}::text = %s
            {order_sql}
            LIMIT %s;
            """,
            (str(doc_id), probe),
        )
        return cur.fetchall()

def _stitch(doc_id: str, rows: List[Dict[str, Any]], k: int) -> Tuple[str, List[Dict[str, Any]]]:
    take = min(5, max(1, k), len(rows))  # hard cap 5
    used = rows[:take]
    cites = [{"doc_id": doc_id, "chunk_index": r.get("chunk_index")} for r in used]
    stitched = "\n---\n".join([str(r.get("preview") or "") for r in used if r.get("preview")])
    return stitched, cites

def _stitch_multi(rows: List[Dict[str, Any]], k: int) -> Tuple[str, List[Dict[str, Any]]]:
    """rows: [{doc_id, chunk_index, preview, score}]"""
    take = min(5, max(1, k), len(rows))
    used = rows[:take]
    cites = [{"doc_id": r["doc_id"], "chunk_index": r["chunk_index"]} for r in used]
    stitched = "\n---\n".join([str(r.get("preview") or "") for r in used if r.get("preview")])
    return stitched, cites

def _model_stub(q: str, stitched: str) -> str:
    if not stitched.strip():
        return "No matches found for this query."
    body = stitched.strip().replace("\r", "")
    prefix = "Answer based on matched passages:\n\n"
    return (prefix + body)[:4000]

def _list_candidate_doc_ids(dept: Optional[str], lang: Optional[str], limit: int) -> List[str]:
    """Lightweight doc lister for global fan-out (dept/lang filters only)."""
    if not table_exists(TABLES["documents"]):
        return []
    d = get_document_cols()
    where, params = [], []
    if dept and d["dept"]:
        where.append(f"{d['dept']} = %s"); params.append(dept)
    if lang and d["lang"]:
        where.append(f"{d['lang']} = %s"); params.append(lang)
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT {d['doc_id']}::text FROM {TABLES['documents']}{where_sql} ORDER BY {d['doc_id']} LIMIT %s",
            params + [limit],
        )
        return [str(r[0]) for r in cur.fetchall()]

def _build_prompt(q: str, stitched: str, lang: Optional[str]) -> str:
    l = (lang or "en").strip().lower()
    if l.startswith("hi"):
        lang_hint = "Reply in Hindi (Devanagari script)."
    else:
        lang_hint = "Reply in English."
    return (
        f"{lang_hint}\n"
        "Answer the user's question using ONLY the information in the Context. "
        "If the Context does not contain the answer, say you don't have enough information.\n\n"
        f"Question: {q}\n\n"
        "Context:\n"
        f"{stitched}\n"
    )

async def _llm_or_stub(prompt: str, lang: Optional[str], timeout_ms: int, max_tokens: int, stitched: str) -> str:
    if get_llm is None:
        return _model_stub("", stitched)
    try:
        # Prefer simple prompt-only call; llm_client should map this to /chat.
        ans = await get_llm().chat(
            prompt=prompt,
            lang=(lang or "en"),
            timeout_ms=timeout_ms,
            max_tokens=max_tokens,
        )
        if not isinstance(ans, str) or not ans.strip():
            return _model_stub("", stitched)
        return ans.strip()
    except Exception:
        return _model_stub("", stitched)

# NOTE: core is async now to await LLM cleanly (endpoints updated accordingly)
async def _gateway_ask_core(request: Request, payload: AskRequest) -> AskResponse:
    ip = request.client.host if request.client else "unknown"
    _rate_check(ip)

    q = (payload.q or "")
    if len(q) > MAX_Q_LEN:
        raise HTTPException(400, f"q too long (max {MAX_Q_LEN})")
    k = max(1, min(10, int(payload.k or 3)))
    doc_id = (payload.doc_id or "").strip()
    lang = (payload.lang or "en").strip().lower()
    timeout_ms = int(payload.timeout_ms or ASK_DEFAULT_TIMEOUT_MS)
    max_tokens = int(payload.max_tokens or ASK_DEFAULT_MAX_TOKENS)

    t0 = time.time()

    # Doc-scoped retrieval
    if doc_id:
        probe = max(10, k * 5)
        rows = _retrieve_rows_for_doc(doc_id, q, probe)
        stitched, cites = _stitch(doc_id, rows, k)
        prompt = _build_prompt(q, stitched, lang)
        answer = await _llm_or_stub(prompt, lang, timeout_ms, max_tokens, stitched)
        elapsed_ms = int((time.time() - t0) * 1000)
        _audit_log({
            "timestamp": int(time.time() * 1000),
            "ip": ip, "q": q, "doc_id": doc_id, "dept": payload.dept, "lang": lang,
            "k": k, "neighbor": payload.neighbor, "cites": cites, "chars_in": len(q), "elapsed_ms": elapsed_ms,
            "timeout_ms": timeout_ms, "max_tokens": max_tokens,
        })
        return AskResponse(answer=answer, cites=cites)

    # Global Ask fan-out (dept/lang filters)
    cand_ids = _list_candidate_doc_ids(payload.dept, lang, FANOUT_MAX_DOCS)
    probe = max(10, k * 5)
    global_rows: List[Dict[str, Any]] = []
    per_take = max(1, min(FANOUT_TAKE_PERDOC, k))

    for did in cand_ids:
        rows = _retrieve_rows_for_doc(did, q, probe)
        if not rows:
            continue
        for r in rows[:per_take]:
            score = float(r.get("score") or 0.0)
            global_rows.append({
                "doc_id": did,
                "chunk_index": int(r.get("chunk_index") or 0),
                "preview": r.get("preview") or "",
                "score": score,
            })

    # Sort globally: score desc, then doc_id, then chunk_index
    global_rows.sort(key=lambda r: (-float(r.get("score") or 0.0), str(r["doc_id"]), int(r["chunk_index"])))

    stitched, cites = _stitch_multi(global_rows, k)
    prompt = _build_prompt(q, stitched, lang)
    answer = await _llm_or_stub(prompt, lang, timeout_ms, max_tokens, stitched)

    elapsed_ms = int((time.time() - t0) * 1000)
    _audit_log({
        "timestamp": int(time.time() * 1000),
        "ip": ip, "q": q, "doc_id": None, "dept": payload.dept, "lang": lang,
        "k": k, "neighbor": payload.neighbor, "cites": cites, "chars_in": len(q), "elapsed_ms": elapsed_ms,
        "fanout_docs": len(cand_ids), "fanout_rows": len(global_rows),
        "timeout_ms": timeout_ms, "max_tokens": max_tokens,
    })

    return AskResponse(answer=answer, cites=cites)

@router.post("/gateway/ask", response_model=AskResponse)
async def gateway_ask(request: Request, payload: AskRequest = Body(...)):
    # Primary path (works when proxy forwards all paths)
    return await _gateway_ask_core(request, payload)

@router.post("/api/gateway/ask", response_model=AskResponse)
async def gateway_ask_alias(request: Request, payload: AskRequest = Body(...)):
    # Alias for proxies that only forward /api/*
    return await _gateway_ask_core(request, payload)

# ---------------------------------------------------------------------------
# Admin: DB init / indexes
# ---------------------------------------------------------------------------

@router.post("/api/admin/db_init")
def admin_db_init():
    with db() as conn, conn.cursor() as cur:
        # ocr_pages
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ocr_pages (
              document_id TEXT NOT NULL,
              page_no     INT  NOT NULL,
              text        TEXT,
              PRIMARY KEY (document_id, page_no)
            );
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS ocr_pages_doc_idx ON ocr_pages(document_id);")

        # pgvector + chunks.embedding
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
        cur.execute("""
            SELECT 1
            FROM information_schema.columns
            WHERE table_name='chunks' AND column_name='embedding'
        """)
        has_embed = cur.fetchone() is not None
        if not has_embed:
            cur.execute("ALTER TABLE chunks ADD COLUMN embedding vector(768);")
        conn.commit()
        return {
            "ok": True,
            "ocr_pages": "ready",
            "chunks.embedding": "exists" if has_embed else "added"
        }

@router.post("/api/admin/db_indexes")
def admin_db_indexes():
    """Recommended indexes incl. pg_trgm + ivfflat (vector)."""
    created: List[str] = []
    with db() as conn, conn.cursor() as cur:
        # extensions
        cur.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm;")
        created.append("EXTENSION pg_trgm")
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
        created.append("EXTENSION vector")

        c_doc, c_idx, c_text, c_embed = get_chunk_cols()

        if c_doc:
            cur.execute(f'CREATE INDEX IF NOT EXISTS idx_chunks_doc ON {TABLES["chunks"]}({c_doc});')
            created.append(f"INDEX idx_chunks_doc ON chunks({c_doc})")
        if c_doc and c_idx:
            cur.execute(f'CREATE INDEX IF NOT EXISTS idx_chunks_doc_idx ON {TABLES["chunks"]}({c_doc}, {c_idx});')
            created.append(f"INDEX idx_chunks_doc_idx ON chunks({c_doc}, {c_idx})")
        if c_text:
            cur.execute(f'CREATE INDEX IF NOT EXISTS idx_chunks_text_trgm ON {TABLES["chunks"]} USING gin ({c_text} gin_trgm_ops);')
            created.append(f"INDEX idx_chunks_text_trgm ON chunks USING gin ({c_text} gin_trgm_ops)")
        if c_embed:
            cur.execute(
                f'CREATE INDEX IF NOT EXISTS idx_chunks_embed_cos ON {TABLES["chunks"]} '
                f'USING ivfflat ({c_embed} vector_cosine_ops) WITH (lists={IVFFLAT_LISTS});'
            )
            created.append(f"INDEX idx_chunks_embed_cos (lists={IVFFLAT_LISTS})")

        conn.commit()
    return {"ok": True, "created": created}

# ---------------------------------------------------------------------------
# Save OCR pages (persist to ocr_pages)
# ---------------------------------------------------------------------------

@router.post("/api/docs/{doc_id}/ocr/save")
def save_ocr_pages(doc_id: str, payload: Dict[str, Any] = Body(...)):
    """
    Payload:
    {
      "pages": [{"page_no": 1, "text": "..."}, ...],
      "overwrite": true|false
    }
    """
    pages = payload.get("pages") or []
    overwrite = bool(payload.get("overwrite", True))
    if not isinstance(pages, list) or not pages:
        raise HTTPException(400, "pages[] required")

    with db() as conn, conn.cursor() as cur:
        if overwrite:
            cur.execute("DELETE FROM ocr_pages WHERE document_id = %s", (doc_id,))
        for p in pages:
            try:
                page_no = int(p.get("page_no", 0))
            except Exception:
                page_no = 0
            text = p.get("text") or ""
            if page_no <= 0:
                continue
            cur.execute(
                """
                INSERT INTO ocr_pages (document_id, page_no, text)
                VALUES (%s, %s, %s)
                ON CONFLICT (document_id, page_no) DO UPDATE SET text = EXCLUDED.text
                """,
                (doc_id, page_no, text),
            )
        conn.commit()
    return {"ok": True, "saved": len(pages)}

# ---------------------------------------------------------------------------
# Admin: call local ingest scripts (trusted network only)
# ---------------------------------------------------------------------------

def _run(cmd: List[str]) -> Dict[str, Any]:
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise HTTPException(500, f"Command failed: {' '.join(cmd)}\n{p.stderr}")
    return {"ok": True, "out": p.stdout}

@router.post("/api/admin/ingest/ocr")
def admin_ingest_ocr(doc_id: str):
    return _run(["python", "ingest_one.py", "--doc-id", doc_id, "--ocr"])

@router.post("/api/admin/ingest/chunk_embed")
def admin_ingest_chunk_embed(doc_id: str):
    return _run(["python", "ingest_one.py", "--doc-id", doc_id, "--chunk", "--embed"])

# ---------------------------------------------------------------------------
# Optional: proxy /api/search and /api/answer to your KB engine
# ---------------------------------------------------------------------------

@router.get("/api/search")
def proxy_search(
    q: str = "", dept: str = "", lang: str = "", n: int = 10, k: int = 60,
    neighbor: int = 1, highlight: int = 1, format: str = "text",
):
    code, ctype, body = _proxy_get("/api/search", {
        "q": q, "dept": dept, "lang": lang, "n": n, "k": k,
        "neighbor": neighbor, "highlight": highlight, "format": format
    })
    return Response(content=body, media_type=ctype, status_code=code)

@router.get("/api/answer")
def proxy_answer(
    q: str, dept: str = "", lang: str = "", neighbor: int = 1,
    highlight: int = 1, format: str = "html",
):
    code, ctype, body = _proxy_get("/api/answer", {
        "q": q, "dept": dept, "lang": lang, "neighbor": neighbor,
        "highlight": highlight, "format": format
    })
    return Response(content=body, media_type=ctype, status_code=code)
