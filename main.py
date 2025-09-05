# Path: main.py
# Version: 0.5.7
# Diff vs 0.5.6:
# - Default LLM_HTTP_BASE now falls back to http://127.0.0.1:8000 if unset.
# - LLM_CHAT_METHOD default switched to POST (from GET).
# - LLM_CHAT_QUERY_PARAM default switched to "prompt" (from "q").
# - Added /api/llm/probe to verify effective settings at runtime.

import os, time, json
from collections import defaultdict, deque
from pathlib import Path
from typing import Optional, Any, List, Dict, Tuple
from urllib.parse import urlsplit

def _load_env():
    from dotenv import load_dotenv
    env_file = os.getenv("ENV_FILE", "")
    if env_file and Path(env_file).exists():
        load_dotenv(env_file, override=True); return
    for candidate in (".env.hr", ".env"):
        if Path(candidate).exists():
            load_dotenv(candidate, override=True); return
_load_env()

from fastapi import FastAPI, Response, Depends, Request, HTTPException, APIRouter
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from server.ops_router import router as ops_router
from services.llm_client import get_llm, LLMClient

import httpx
from httpx import ASGITransport

from Ask.be.ask_router import router as ask_router
from Ask.be.wa_router import router as wa_router

APP_TITLE = os.getenv("APP_TITLE", "DocuMind-HR Ops API")
APP_VERSION = "0.5.7"

def _env_first(*keys: str, default: Optional[str] = None) -> Optional[str]:
    for k in keys:
        v = os.getenv(k)
        if v is not None and str(v).strip() != "":
            return v
    return default

FILES_DIR = os.getenv("FILES_DIR", "data")
ASGI_LOOPBACK = _env_first("DOCUMIND_USE_ASGI", "ASGI_LOOPBACK", default="1").lower() in ("1","true","yes")
SELF_HTTP_BASE = _env_first("DOCUMIND_HTTP_BASE","BACKEND_BASE_HTTP","API_BASE_HTTP","SELF_HTTP_BASE") or "http://127.0.0.1:9000"
SELF_HTTP_TIMEOUT = float(_env_first("DOCUMIND_HTTP_TIMEOUT","SELF_HTTP_TIMEOUT", default="30"))
LLM_PASSAGES_LIMIT = int(_env_first("DOCUMIND_PASSAGES_LIMIT","LLM_PASSAGES_LIMIT", default="100"))

_llm_base = _env_first("LLM_HTTP_BASE","LLM_BASE_URL","MISTRAL_BASE_URL")
if not _llm_base:
    _llm_host = _env_first("LLM_HOST","LLM_HTTP_HOST")
    _llm_port = _env_first("LLM_PORT","LLM_HTTP_PORT")
    _llm_scheme = _env_first("LLM_SCHEME","LLM_HTTP_SCHEME", default="http")
    if _llm_host and _llm_port:
        _llm_base = f"{_llm_scheme}://{_llm_host}:{_llm_port}"
# NEW: safe fallback for local gateway
if not _llm_base:
    _llm_base = "http://127.0.0.1:8000"
os.environ.setdefault("LLM_HTTP_BASE", _llm_base)

# NEW DEFAULTS for your /chat gateway
os.environ.setdefault("LLM_CHAT_PATH", _env_first("LLM_CHAT_PATH", default="/chat"))
os.environ.setdefault("LLM_CHAT_METHOD", _env_first("LLM_CHAT_METHOD", default="POST"))
os.environ.setdefault("LLM_CHAT_QUERY_PARAM", _env_first("LLM_CHAT_QUERY_PARAM", default="prompt"))
os.environ.setdefault("LLM_MODEL", _env_first("LLM_MODEL","MISTRAL_MODEL", default="mistral-7b"))

app = FastAPI(title=APP_TITLE, version=APP_VERSION)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in _env_first("CORS_ORIGINS", default="*").split(",")] if _env_first("CORS_ORIGINS") else ["*"],
    allow_credentials=_env_first("CORS_ALLOW_CREDENTIALS", default="false").lower() in ("1","true","yes"),
    allow_methods=["GET","POST","OPTIONS"],
    allow_headers=["*"],
    expose_headers=[
        "content-type","x-response-time-ms","x-llm-status","x-llm-source",
        "x-passages-k","x-llm-topk-used","x-llm-percentcap-used","x-llm-overrides",
        "x-tokens-in","x-tokens-out","x-llm-model","x-llm-lang",
    ],
    max_age=3600,
)

app.include_router(ask_router)
app.include_router(wa_router)

@app.options("/{full_path:path}")
async def _cors_probe(full_path: str, request: Request):
    return Response(status_code=200)

app.state._latency = defaultdict(lambda: deque(maxlen=100))
@app.middleware("http")
async def timing_mw(request: Request, call_next):
    t0 = time.perf_counter()
    resp = None
    try:
        resp = await call_next(request); return resp
    finally:
        dt_ms = (time.perf_counter() - t0) * 1000.0
        try: app.state._latency[request.url.path].append(dt_ms)
        except: pass
        if resp is not None:
            try: resp.headers["X-Response-Time-ms"] = f"{dt_ms:.2f}"
            except: pass

@app.get("/health")
def health(): return {"ok": True}

@app.get("/healthz/files")
def healthz_files():
    root = Path(FILES_DIR); sample = root / "pdfs/animal/en/1.pdf"
    return {"FILES_DIR": str(root), "exists": root.is_dir(), "sample": str(sample), "sample_exists": sample.exists()}

if os.path.isdir(FILES_DIR):
    app.mount("/files", StaticFiles(directory=FILES_DIR), name="files")

from server.ops_router import router as ops_router
app.include_router(ops_router, prefix="")

@app.get("/")
def root():
    return {
        "service": APP_TITLE, "version": APP_VERSION,
        "endpoints": [
            "/health","/api/llm/ping","/api/llm/defaults","/api/llm/help",
            "/api/docs/{id}/llm_summarize/preview","/api/docs/{id}/llm_summarize/save"
        ],
    }

@app.get("/version")
def version(): return {"version": APP_VERSION}

# NEW: quick probe to confirm chat wiring at runtime
@app.get("/api/llm/probe")
def llm_probe():
    return {
        "LLM_HTTP_BASE": os.getenv("LLM_HTTP_BASE"),
        "LLM_CHAT_PATH": os.getenv("LLM_CHAT_PATH"),
        "LLM_CHAT_METHOD": os.getenv("LLM_CHAT_METHOD"),
        "LLM_CHAT_QUERY_PARAM": os.getenv("LLM_CHAT_QUERY_PARAM"),
    }

@app.get("/api/llm/ping")
async def llm_ping(llm: LLMClient = Depends(get_llm)):
    txt = await llm.chat([{"role":"user","content":"ping"}])
    return {"ok": True, "sample": txt[:120]}

@app.get("/api/llm/defaults")
def llm_defaults():
    def _get(name, dv):
        v = os.getenv(name)
        if v is None: return dv
        try: return type(dv)(v)
        except: return dv
    return {
        "model": _get("LLM_MODEL","mistral-7b"),
        "max_tokens": _get("LLM_MAX_TOKENS", 768),
        "temperature": _get("LLM_TEMPERATURE", 0.2),
        "top_p": _get("LLM_TOP_P", 1.0),
        "presence_penalty": _get("LLM_PRESENCE_PENALTY", 0.0),
        "frequency_penalty": _get("LLM_FREQUENCY_PENALTY", 0.0),
        "timeout_ms": _get("LLM_DEADLINE_MS", 30000),
        "topk": _get("DOCUMIND_PASSAGES_LIMIT", 24),
        "percent_cap": 100,
        "style": "bullet",
    }

@app.get("/api/llm/help")
def llm_help():
    return {
        "temperature": "Higher = more random; lower = more deterministic.",
        "top_p": "Nucleus sampling: sample from smallest prob. mass whose cumulative probability >= top_p (0-1).",
        "topk": "Retrieval: number of top-ranked snippets/chunks to assemble before summarizing.",
        "percent_cap": "Percent of assembled text to keep (roughly by characters)."
    }

class SummarizeIn(BaseModel):
    text: str
    style: Optional[str] = "bullet"

@app.post("/api/summarize")
async def api_summarize(payload: SummarizeIn, llm: LLMClient = Depends(get_llm)):
    summary = await llm.summarize(payload.text, payload.style or "bullet")
    return {"summary": summary}

def _safe_err_note(err: Exception) -> str:
    try:
        if isinstance(err, httpx.HTTPStatusError):
            req = err.request; resp = err.response
            p = urlsplit(str(req.url)); base = f"{p.scheme}://{p.netloc}{p.path}"
            return f"http {resp.status_code} on {req.method} {base}"
        if isinstance(err, httpx.RequestError):
            req = getattr(err, "request", None)
            if req is not None:
                p = urlsplit(str(req.url)); base = f"{p.scheme}://{p.netloc}{p.path}"
                return f"request error on {req.method} {base}"
        msg = str(err)
        if "http" in msg and "?" in msg:
            try:
                start = msg.index("http"); end_q = msg.index("?", start); after = msg.find(" ", end_q)
                msg = msg[:end_q] if after == -1 else msg[:end_q] + msg[after:]
            except Exception: pass
        return (msg[:177] + "...") if len(msg) > 180 else msg
    except Exception:
        return "error"

router_llm = APIRouter(prefix="/api/docs", tags=["llm"])

async def _asgi_get_json(path: str) -> Tuple[int, Any]:
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://asgi.local") as client:
        r = await client.get(path, timeout=SELF_HTTP_TIMEOUT)
        try: return r.status_code, r.json()
        except: return r.status_code, r.text

async def _http_get_json(path: str) -> Tuple[int, Any]:
    url = f"{SELF_HTTP_BASE}{path}"
    async with httpx.AsyncClient() as client:
        r = await client.get(url, timeout=SELF_HTTP_TIMEOUT)
        try: return r.status_code, r.json()
        except: return r.status_code, r.text

async def get_self_json(path: str) -> Any:
    if ASGI_LOOPBACK:
        try:
            status, payload = await _asgi_get_json(path)
            if status == 200: return payload
        except: pass
    status, payload = await _http_get_json(path)
    if status != 200: raise HTTPException(status_code=status, detail=f"Upstream failed for {path}")
    return payload

def _take_first_n(items: List[Any], n: int) -> List[Any]:
    n = max(0, int(n))
    if n <= 0: return []
    return list(items[:n])

async def collect_doc_text(doc_id: str, limit: int) -> Tuple[str, str, int, int]:
    try:
        data = await get_self_json(f"/api/docs/{doc_id}/passages?limit={limit}")
        passages: List[str] = []
        if isinstance(data, list):
            for it in data:
                if isinstance(it, dict) and "text" in it: passages.append(str(it.get("text") or ""))
        elif isinstance(data, dict):
            items = data.get("items") or data.get("passages") or []
            for it in items:
                if isinstance(it, dict) and "text" in it: passages.append(str(it.get("text") or ""))
        if passages:
            used = _take_first_n(passages, limit)
            return ("\n\n".join(used), "passages", len(passages), len(used))
    except Exception: pass
    try:
        data = await get_self_json(f"/api/docs/{doc_id}/text")
        t = ""
        if isinstance(data, dict): t = str(data.get("text") or data.get("content") or "")
        if t.strip(): return (t, "text", 0, 1)
    except Exception: pass
    try:
        data = await get_self_json(f"/api/docs/{doc_id}/ocr")
        t = ""
        if isinstance(data, dict): t = str(data.get("text") or "")
        if t.strip(): return (t, "ocr", 0, 1)
    except Exception: pass
    try:
        data = await get_self_json(f"/api/docs/{doc_id}/chunks?limit={limit}")
        chunks: List[str] = []
        if isinstance(data, list):
            for it in data:
                if isinstance(it, dict) and "text" in it: chunks.append(str(it.get("text") or ""))
        elif isinstance(data, dict):
            items = data.get("items") or data.get("chunks") or []
            for it in items:
                if isinstance(it, dict) and "text" in it: chunks.append(str(it.get("text") or ""))
        if chunks:
            used = _take_first_n(chunks, limit)
            return ("\n\n".join(used), "mixed", len(chunks), len(used))
    except Exception: pass
    return ("", "unknown", 0, 0)

def _toy_bullet_summarize(text: str, max_lines: int = 8) -> str:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines: return ""
    head = lines[: max_lines * 2]
    out = []
    for ln in head:
        if len(out) >= max_lines: break
        if len(ln) > 300: ln = ln[:297] + "..."
        out.append(f"• {ln}")
    return "\n".join(out)

@router_llm.post("/{doc_id}/llm_summarize/preview")
async def llm_preview(doc_id: str, body: Dict[str, Any], llm: LLMClient = Depends(get_llm)):
    t0 = time.perf_counter()
    try:
        style = str((body or {}).get("style") or "bullet").lower()
        overrides = (body or {}).get("overrides") or {}
        lang = (body or {}).get("lang") or "en"
        force_fb = bool((body or {}).get("force_fallback"))  # NEW

        env_defaults = {
            "max_tokens": int(os.getenv("LLM_MAX_TOKENS", "768")),
            "temperature": float(os.getenv("LLM_TEMPERATURE", "0.2")),
            "top_p": float(os.getenv("LLM_TOP_P", "1.0")),
            "presence_penalty": float(os.getenv("LLM_PRESENCE_PENALTY", "0.0")),
            "frequency_penalty": float(os.getenv("LLM_FREQUENCY_PENALTY", "0.0")),
            "timeout_ms": int(os.getenv("LLM_DEADLINE_MS", "30000")),
            "topk": int(os.getenv("DOCUMIND_PASSAGES_LIMIT", "24")),
            "percent_cap": 100,
        }
        eff = {**env_defaults, **{k: v for k, v in overrides.items() if v is not None}}

        limit = int(eff["topk"])
        text, source, k_reported, k_used = await collect_doc_text(doc_id, limit=limit)

        if not text.strip():
            took_ms = int((time.perf_counter() - t0) * 1000)
            headers = {
                "X-Response-Time-ms": str(took_ms),"X-LLM-Status": "fallback","X-LLM-Source": source,
                "X-Passages-K": str(k_reported),"X-LLM-TopK-Used": str(k_used),"X-LLM-PercentCap-Used": "0",
                "X-LLM-Overrides": json.dumps(eff, separators=(",", ":")),
                "X-Tokens-In": "0","X-Tokens-Out": "0","X-LLM-Model": os.getenv("LLM_MODEL","mistral-7b"),
                "X-LLM-Lang": str(lang),
            }
            print(f"[LLM] PREVIEW doc={doc_id} took={took_ms}ms status=fallback source={source} k=0/0 cap=0% tokens=0|0 lang={lang}")
            return JSONResponse({"doc_id": doc_id, "summary": "", "note": "no text found"}, headers=headers)

        pct_used = int(eff["percent_cap"])
        if isinstance(pct_used, int) and 10 <= pct_used <= 100 and pct_used < 100:
            clip_at = max(500, int(len(text) * (pct_used / 100.0)))
            text = text[:clip_at]

        usage_in = usage_out = None
        note = source
        status = "ok"

        # NEW: user-forced local fallback (used on Cancel)
        if force_fb:
            summary = _toy_bullet_summarize(text)
            note = f"{source}; user_cancel"
            status = "fallback"
        else:
            try:
                try:
                    summary = await llm.summarize(
                        text, style or "bullet",
                        max_tokens=eff["max_tokens"], temperature=eff["temperature"], top_p=eff["top_p"],
                        presence_penalty=eff["presence_penalty"], frequency_penalty=eff["frequency_penalty"],
                        timeout_ms=eff["timeout_ms"], lang=lang
                    )
                except TypeError:
                    summary = await llm.summarize(text, style or "bullet")

                if not summary or not summary.strip():
                    raise RuntimeError("empty summary")

                if hasattr(llm, "last_usage"):
                    usage_in  = (llm.last_usage or {}).get("prompt_tokens")
                    usage_out = (llm.last_usage or {}).get("completion_tokens")
            except Exception as e:
                summary = _toy_bullet_summarize(text)
                note = f"{source}; llm_fallback: {_safe_err_note(e)}"
                status = "fallback"

        took_ms = int((time.perf_counter() - t0) * 1000)
        headers = {
            "X-Response-Time-ms": str(took_ms),"X-LLM-Status": status,"X-LLM-Source": source,
            "X-Passages-K": str(k_reported),"X-LLM-TopK-Used": str(k_used),"X-LLM-PercentCap-Used": str(pct_used),
            "X-LLM-Overrides": json.dumps(eff, separators=(",", ":")),
            "X-Tokens-In": str(usage_in or 0),"X-Tokens-Out": str(usage_out or 0),
            "X-LLM-Model": os.getenv("LLM_MODEL","mistral-7b"),"X-LLM-Lang": str(lang),
        }
        print(f"[LLM] PREVIEW doc={doc_id} took={took_ms}ms status={status} source={source} k={k_used}/{limit} cap={pct_used}% tokens={(usage_in or 0)}|{(usage_out or 0)} lang={lang}")
        return JSONResponse({"doc_id": doc_id, "summary": summary, "note": note}, headers=headers)

    except Exception as e:
        took_ms = int((time.perf_counter() - t0) * 1000)
        headers = {
            "X-Response-Time-ms": str(took_ms),"X-LLM-Status": "fallback","X-LLM-Source": "unknown",
            "X-Passages-K": "0","X-LLM-TopK-Used": "0","X-LLM-PercentCap-Used": "0",
            "X-LLM-Overrides": "{}","X-Tokens-In": "0","X-Tokens-Out": "0",
            "X-LLM-Model": os.getenv("LLM_MODEL","mistral-7b"),"X-LLM-Lang": str((body or {}).get("lang") or "en"),
        }
        safe_note = _safe_err_note(e)
        print(f"[LLM] PREVIEW doc={doc_id} took={took_ms}ms status=error source=unknown k=0/0 cap=0% tokens=0|0 lang={(body or {}).get('lang') or 'en'} err={safe_note}")
        return JSONResponse({"doc_id": doc_id, "summary": "", "note": f"llm_fallback: {safe_note}"}, headers=headers)

@router_llm.post("/{doc_id}/llm_summarize/save")
async def llm_save(doc_id: str, body: Dict[str, Any], llm: LLMClient = Depends(get_llm)):
    style = str((body or {}).get("style") or "bullet").lower()
    overrides = (body or {}).get("overrides") or {}
    lang = (body or {}).get("lang") or "en"
    topk        = int(overrides.get("topk") or LLM_PASSAGES_LIMIT)
    percent_cap = overrides.get("percent_cap")

    text, source, k_reported, k_used = await collect_doc_text(doc_id, limit=topk)
    if not text.strip():
        return {"ok": False, "note": source or "no text found"}

    if isinstance(percent_cap, int) and 10 <= percent_cap <= 100 and percent_cap < 100:
        clip_at = max(500, int(len(text) * (percent_cap / 100.0)))
        text = text[:clip_at]

    try:
        summary = await llm.summarize(text, style or "bullet", lang=lang)
        if not summary or not summary.strip(): raise RuntimeError("empty summary")
        persisted = True; note = source
    except Exception as e:
        summary = _toy_bullet_summarize(text)
        persisted = False; note = f"{source}; llm_fallback: {_safe_err_note(e)}"

    target = f"doc:{doc_id}:summary:{style}"
    return {"ok": persisted, "saved_to": target, "note": note, "summary": summary, "k_used": k_used, "k_reported": k_reported}

app.include_router(router_llm)
