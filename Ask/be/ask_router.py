# Path: Ask/be/ask_router.py
# Purpose: Ask endpoints using the SAME client as Summarize (services/llm_client.py)
# Version: 0.7.0 (surgical)
#  - Uses services.llm_client.get_llm().chat(messages=...) to keep Summarize compatibility
#  - Aliases: /api/ask/answer and /ask/answer (and /health)
#  - 60s default timeout; honors req.timeout_ms; language hint preserved
#  - Local fallback only on real errors (never show raw "HTTP 404" as answer)

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field
from typing import List, Dict, Any, Optional
import time, os, re, logging

router = APIRouter()
logger = logging.getLogger("ask")

# ---- models -------------------------------------------------------------------

class AskRequest(BaseModel):
    q: str = Field(..., description="User question")
    lang: str = Field("en", description="Language code: en|hi")
    dept: Optional[str] = None
    topk: int = Field(12, ge=1, le=50)
    percent_cap: int = Field(60, ge=10, le=100)
    evidence_k: int = Field(8, ge=1, le=50)
    max_tokens: Optional[int] = None
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    presence_penalty: Optional[float] = None
    frequency_penalty: Optional[float] = None
    timeout_ms: Optional[int] = Field(None, ge=1000, le=600000)
    mode: Optional[str] = None  # "llm" | "local" | e.g. "LLM (longer)"

class Citation(BaseModel):
    doc_id: str
    chunks: List[int] = []

class AskResponse(BaseModel):
    answer: str
    citations: List[Citation]
    confidence: float
    grounded: bool
    meta: Dict[str, Any]

# ---- shared services ----------------------------------------------------------

try:
    # Use the SAME client Summarize uses
    from services.llm_client import get_llm  # <- canonical
except Exception:  # very defensive fallback if module layout differs
    from ..services.llm_client import get_llm  # type: ignore

try:
    from Ask.be.services.retriever import retrieve_hybrid as _retrieve_hybrid  # type: ignore
except Exception:
    async def _retrieve_hybrid(q: str, lang: str = "en", dept: Optional[str] = None, topk: int = 12):
        return []

try:
    from Ask.be.services.answerer import generate_answer as _generate_answer  # type: ignore
except Exception:
    async def _generate_answer(question: str, lang: str, passages: List[Dict[str, Any]], percent_cap: int = 60):
        return {"answer": "No sufficient evidence.", "citations": [], "confidence": 0.0, "grounded": False}

# ---- helpers ------------------------------------------------------------------

def _env_true(name: str, default=False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return bool(default)
    return raw.strip().lower() in ("1", "true", "yes", "y", "on")

def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return default

def _cap(n: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, n))

def _env_max_tokens(default_if_missing: int = 512) -> int:
    try:
        v = int(os.getenv("LLM_ASK_MAX_TOKENS", "") or os.getenv("LLM_MAX_TOKENS", "") or "")
        return v if v > 0 else default_if_missing
    except Exception:
        return default_if_missing

def _default_timeout_ms() -> int:
    try:
        return int(os.getenv("LLM_TIMEOUT_MS", "60000"))  # 60s default
    except Exception:
        return 60000

def _resolve_mode(req_mode: Optional[str], header_mode: Optional[str]) -> str:
    txt = (header_mode or req_mode or "").strip().lower()
    if txt:
        if "llm" in txt or txt in ("model", "remote", "gateway"):
            return "llm"
        if "local" in txt:
            return "local"
    # default to LLM (this matched your working setup)
    return "llm" if _env_true("ASK_USE_LLM", True) else "local"

def _prompt_max_bytes() -> int:
    return _env_int("LLM_PROMPT_MAX_BYTES", 12000)

def _truncate_utf8_to_bytes(s: str, max_bytes: int) -> str:
    b = s.encode("utf-8")[: max(0, max_bytes)]
    return b.decode("utf-8", errors="ignore")

def _build_grounded_prompt(question: str, lang: str, passages: List[Dict[str, Any]], percent_cap: int) -> Dict[str, Any]:
    header = (
        "You are DocuMind. Answer clearly and concisely using ONLY the evidence below.\n"
        "If the evidence does not contain the answer, say you don't have enough information.\n\n"
        "EVIDENCE:\n"
    )
    lang_line = "Answer in Hindi." if (lang or "en").lower().startswith("hi") else "Answer in English."
    qline = f"\n\nQUESTION: {question}\n{lang_line}"
    max_bytes = max(1024, _prompt_max_bytes())

    total_chars = sum(len(p.get("text", "")) for p in passages) or 1
    pct_chars = int(total_chars * (_cap(percent_cap, 10, 100) / 100.0))

    pieces: List[str] = [header]
    used_bytes = len(header.encode("utf-8"))
    q_bytes = len(qline.encode("utf-8"))
    budget = max_bytes - q_bytes
    if budget <= 0:
        prompt = _truncate_utf8_to_bytes(header + qline, max_bytes)
        return {"prompt": prompt, "bytes": len(prompt.encode("utf-8"))}

    used_chars = 0
    for p in passages:
        t = (p.get("text") or "").strip()
        if not t:
            continue
        used_chars += len(t)
        if used_chars > pct_chars and pct_chars > 0:
            break
        line = f"[{p.get('doc_id')}#{p.get('chunk', 0)}] {t}\n"
        lb = len(line.encode("utf-8"))
        if used_bytes + lb > budget:
            remain = budget - used_bytes
            if remain <= 0:
                break
            line = _truncate_utf8_to_bytes(line, remain)
            pieces.append(line)
            used_bytes += len(line.encode("utf-8"))
            break
        pieces.append(line)
        used_bytes += lb

    prompt_body = "".join(pieces) + qline
    prompt_safe = _truncate_utf8_to_bytes(prompt_body, max_bytes)
    return {"prompt": prompt_safe, "bytes": len(prompt_safe.encode("utf-8"))}

_DOC_TAG_CLOSED_RE = re.compile(r"\s*\[(?:DOC|DOC#(?:chunks?|chunk)?)\s*[:\-]?\s*.*?\]\s*", re.IGNORECASE | re.DOTALL)
_DOC_TAG_HALFOPEN_INLINE_RE = re.compile(r"\s*\[(?:DOC|DOC#(?:chunks?|chunk)?)\s*[:\-]?[^\]\n]*$", re.IGNORECASE | re.MULTILINE)
_SYS_ECHO_RE = re.compile(r"^\s*You are DocuMind\.[^\n]*$", re.IGNORECASE | re.MULTILINE)
_LANG_ECHO_RE = re.compile(r"^\s*Answer in (?:English|Hindi)\.[^\n]*$", re.IGNORECASE | re.MULTILINE)

def _sanitize(text: str, *, for_mode: str) -> str:
    if not text:
        return ""
    t = _DOC_TAG_CLOSED_RE.sub(" ", text)
    t = _DOC_TAG_HALFOPEN_INLINE_RE.sub(" ", t)
    t = _SYS_ECHO_RE.sub("", t)
    t = _LANG_ECHO_RE.sub("", t)
    t = re.sub(r"[ \t]{2,}", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    if for_mode == "local":
        lines = []
        for raw in t.splitlines():
            s = raw.strip()
            if not s:
                continue
            if s.startswith("•"):
                s = "• " + s.lstrip("•").lstrip()
            else:
                s = re.sub(r"^(?:[•\-\–\—\*]+|\(?\d{1,3}\)?[.)]?|\(?[a-zA-Z]\)?[.)]?)\s+", "• ", s)
            lines.append(s)
        t = "\n".join(lines)
    return t.strip()

# ---- endpoints ----------------------------------------------------------------

@router.get("/api/ask/health")
@router.get("/ask/health")
async def ask_health():
    return {
        "status": "ok",
        "component": "ask",
        "ask_default_mode": "llm" if _env_true("ASK_USE_LLM", True) else "local",
        "llm_http_base": os.getenv("LLM_HTTP_BASE", "") or os.getenv("LLM_API_BASE", ""),
        "llm_timeout_ms": os.getenv("LLM_TIMEOUT_MS", "60000"),
        "llm_prompt_max_bytes": os.getenv("LLM_PROMPT_MAX_BYTES", "12000"),
    }

async def _ask_answer_core(req: AskRequest, request: Request) -> AskResponse:
    t0 = time.time()
    header_mode = request.headers.get("X-Ask-Mode")
    mode = _resolve_mode(req.mode, header_mode)

    # 1) retrieve
    k = _cap(req.topk, 1, 50)
    passages = await _retrieve_hybrid(req.q, lang=req.lang, dept=req.dept, topk=k)
    evidence_k = _cap(req.evidence_k, 1, k)
    fwd = passages[:evidence_k]

    if not fwd:
        took_ms = int((time.time() - t0) * 1000)
        return AskResponse(
            answer="I couldn’t find enough evidence in the documents to answer.",
            citations=[],
            confidence=0.0,
            grounded=False,
            meta={"took_ms": took_ms, "k": k, "evidence_k": evidence_k, "passages": 0, "source": mode},
        )

    # 2) local path
    if mode == "local":
        g0 = time.time()
        g = await _generate_answer(question=req.q, lang=req.lang, passages=fwd, percent_cap=req.percent_cap)
        answer = _sanitize(g.get("answer", ""), for_mode="local")
        took_ms = int((time.time() - t0) * 1000)
        cites = [Citation(doc_id=p.get("doc_id", ""), chunks=[p.get("chunk", 0)]) for p in fwd]
        return AskResponse(
            answer=answer,
            citations=cites,
            confidence=float(g.get("confidence", 0.9)),
            grounded=bool(g.get("grounded", True)),
            meta={
                "took_ms": took_ms,
                "generator_ms": int((time.time() - g0) * 1000),
                "k": k,
                "evidence_k": evidence_k,
                "passages": len(fwd),
                "percent_cap": req.percent_cap,
                "source": "local",
                "max_tokens": req.max_tokens or _env_max_tokens(),
            },
        )

    # 3) LLM path (via the SAME client Summarize uses)
    built = _build_grounded_prompt(req.q, req.lang, fwd, req.percent_cap)
    prompt = built["prompt"]
    prompt_bytes = built["bytes"]

    final_max_tokens = req.max_tokens if (req.max_tokens and req.max_tokens > 0) else _env_max_tokens()
    timeout_ms = req.timeout_ms if (req.timeout_ms and req.timeout_ms > 0) else _default_timeout_ms()

    logger.info("[ASK] Calling LLM via services.llm_client (timeout_ms=%s, max_tokens=%s, prompt_bytes=%s)",
                timeout_ms, final_max_tokens, prompt_bytes)

    llm = get_llm()
    llm_ms = None
    used_tokens = None
    source_flag = "llm"

    try:
        l0 = time.time()
        # Build messages for the shared client
        lang_hint = "Answer in Hindi." if (req.lang or "en").lower().startswith("hi") else "Answer in English."
        system_msg = {"role": "system", "content": lang_hint}
        user_msg = {"role": "user", "content": prompt}
        text = await llm.chat(
            [system_msg, user_msg],
            temperature=req.temperature,
            top_p=req.top_p,
            max_tokens=final_max_tokens,
            timeout_ms=timeout_ms,
        )
        llm_ms = int((time.time() - l0) * 1000)
        used_tokens = (llm.last_usage or {}).get("total_tokens") if getattr(llm, "last_usage", None) else None
        if not isinstance(text, str) or not text.strip():
            raise RuntimeError("Empty LLM reply")
    except Exception as e:
        logger.error("LLM error (%s) — falling back to local", e)
        g = await _generate_answer(question=req.q, lang=req.lang, passages=fwd, percent_cap=req.percent_cap)
        text = g.get("answer", "I’m falling back to a local answer due to LLM unavailability.")
        source_flag = "local"

    text = _sanitize(text or "", for_mode=source_flag)
    cites = [Citation(doc_id=p.get("doc_id", ""), chunks=[p.get("chunk", 0)]) for p in fwd]
    took_ms = int((time.time() - t0) * 1000)

    return AskResponse(
        answer=text or "",
        citations=cites,
        confidence=0.9,
        grounded=True,
        meta={
            "took_ms": took_ms,
            "llm_elapsed_ms": llm_ms,
            "used_tokens": used_tokens,
            "k": k,
            "evidence_k": evidence_k,
            "passages": len(fwd),
            "percent_cap": req.percent_cap,
            "max_tokens": final_max_tokens,
            "timeout_ms": timeout_ms,
            "prompt_bytes": prompt_bytes,
            "prompt_max_bytes": _prompt_max_bytes(),
            "source": source_flag,
        },
    )

@router.post("/api/ask/answer", response_model=AskResponse)
@router.post("/ask/answer", response_model=AskResponse)
async def ask_answer(req: AskRequest, request: Request):
    return await _ask_answer_core(req, request)
