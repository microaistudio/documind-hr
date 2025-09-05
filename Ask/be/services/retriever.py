# Path: Ask/be/services/retriever.py
# Product: DocuMind-HR (Ask DocuMind)
# Purpose: Robust retrieval via in-process ASGI or HTTP; tries /api/search, /api/search/hybrid, /api/search/semantic
# Version: 0.3.1 (2025-08-29)

from typing import List, Dict, Optional, Any, Tuple, Callable
import os, logging, httpx

log = logging.getLogger("ask.retriever")

_HTTP_BASE = os.getenv("DOCUMIND_HTTP_BASE", "http://127.0.0.1:9000")
_HTTP_TIMEOUT = float(os.getenv("DOCUMIND_HTTP_TIMEOUT", "30"))
_USE_ASGI = os.getenv("DOCUMIND_USE_ASGI", "1").strip() == "1"

# Try these paths/methods in order
_CANDIDATES: Tuple[Tuple[str, str], ...] = (
    ("/api/search/hybrid", "GET"),
    ("/api/search/hybrid", "POST"),
    ("/api/search/semantic", "GET"),
    ("/api/search/semantic", "POST"),
    ("/api/search", "GET"),
    ("/api/search", "POST"),
)

# Send multiple equivalent param names; backend can accept any
def _build_param_variants(q: str, dept: Optional[str], n: int) -> List[Dict[str, Any]]:
    base = {"q": q}
    if dept:
        base["dept"] = dept
    variants = []
    for kname in ("n", "k", "limit", "topk"):
        v = dict(base)
        v[kname] = n
        variants.append(v)
    return variants

def _pick(d: Dict[str, Any], names: Tuple[str, ...], default: Any = None) -> Any:
    for n in names:
        if n in d and d[n] is not None:
            return d[n]
    return default

def _norm(p: Dict[str, Any]) -> Dict[str, Any]:
    doc_id = _pick(p, ("doc_id","document_id","doc","id","docId"), "?")
    chunk  = _pick(p, ("chunk","chunk_index","index","part","section","page","chunkId","chunk_no"), 0)
    text   = _pick(p, ("text","snippet","passage","content","body","preview","text_snippet","chunk_text"), "")
    score  = _pick(p, ("score","similarity","cosine","rank","bm25","dense_score"), 0.0)
    try: chunk = int(chunk)
    except: chunk = 0
    try: score = float(score)
    except: score = 0.0
    return {"doc_id": str(doc_id), "chunk": chunk, "text": str(text), "score": score}

def _extract_list(data: Any) -> List[Dict[str, Any]]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in ("passages","results","items","hits","data"):
            v = data.get(k)
            if isinstance(v, list) and v:
                return v
        for outer in ("result","payload","response"):
            v = data.get(outer)
            if isinstance(v, dict):
                for k in ("passages","results","items","hits","data"):
                    v2 = v.get(k)
                    if isinstance(v2, list) and v2:
                        return v2
    return []

async def _try_with_client(do: Callable[[httpx.AsyncClient, str, str, Dict[str, Any]], Any]) -> Optional[Any]:
    # 1) ASGI fast path
    if _USE_ASGI:
        try:
            from httpx import ASGITransport  # type: ignore
            from main import app
            async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://asgi.local", timeout=_HTTP_TIMEOUT) as client:
                for path, method in _CANDIDATES:
                    for params in _PARAM_VARIANTS:
                        try:
                            resp = await do(client, path, method, params)
                            if resp is not None:
                                return resp
                        except Exception:
                            continue
        except Exception as e:
            log.info("ASGI loopback unavailable: %s; falling back to HTTP", e)

    # 2) HTTP fallback
    async with httpx.AsyncClient(base_url=_HTTP_BASE, timeout=_HTTP_TIMEOUT) as client:
        for path, method in _CANDIDATES:
            for params in _PARAM_VARIANTS:
                try:
                    resp = await do(client, path, method, params)
                    if resp is not None:
                        return resp
                except Exception:
                    continue
    return None

# Will be set inside retrieve_hybrid so variants depend on n/dept
_PARAM_VARIANTS: List[Dict[str, Any]] = []

async def retrieve_hybrid(q: str, lang: str = "en", dept: Optional[str] = None, topk: int = 12) -> List[Dict]:
    """
    Try multiple endpoints (/api/search/hybrid, /api/search/semantic, /api/search),
    multiple methods (GET/POST), and multiple param names (n/k/limit/topk).
    """
    n = max(1, min(topk, 50))
    global _PARAM_VARIANTS
    _PARAM_VARIANTS = _build_param_variants(q=q, dept=dept, n=n)

    async def _do(client: httpx.AsyncClient, path: str, method: str, params: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
        if method == "GET":
            r = await client.get(path, params=params)
        else:
            r = await client.post(path, json=params)
        if r.status_code != 200:
            return None
        try:
            data = r.json()
        except Exception:
            return None
        raw = _extract_list(data)
        return raw if raw else None

    raw = await _try_with_client(_do)
    if not raw:
        log.info("retrieve_hybrid: no results; tried %s", [p for p, _ in _CANDIDATES])
        return []

    return [_norm(p) for p in raw[:n]]
