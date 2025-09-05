# Path: Ask/be/wa_router.py
# WhatsApp webhook -> retrieval -> engine (LLM or Local) with strict fallback
# Engine selector: "llm <q>" forces LLM; "local <q>" forces Local; otherwise auto (LLM→fallback)
# Per-message overrides: t=<tokens>, s=<seconds>, k=<topk>, "fast"
# Version: 1.1.0

import os, hmac, base64, hashlib, logging, time, re, inspect
from html import escape as _xml_escape
from typing import Dict, Any, Optional, List, Tuple

try:
    import anyio  # for thread offload if llm client is sync
except Exception:
    anyio = None

from fastapi import APIRouter, Request, Response
from fastapi.responses import PlainTextResponse

# ---- project services (same ones Ask uses) ----
from Ask.be.services.retriever import retrieve_hybrid
from Ask.be.services.answerer import generate_answer
from Ask.be.services.whatsapp import format_wa_reply
try:
    from Ask.be.services.llm_client import chat as _llm_chat  # async in most builds
except Exception:
    _llm_chat = None

log = logging.getLogger("wa")
router = APIRouter(prefix="/api/wa", tags=["whatsapp"])

# =========================
#        CONFIG
# =========================

WA_DEFAULT_LANG = os.getenv("WA_DEFAULT_LANG", "en")
WA_DEFAULT_DEPT = os.getenv("WA_DEFAULT_DEPT")  # e.g., "animal")

# Retrieval defaults (can be overridden per message with k=)
WA_TOPK = int(os.getenv("WA_TOPK", "6"))
WA_PERCENT_CAP = int(os.getenv("WA_PERCENT_CAP", "60"))

# WhatsApp-focused LLM defaults (fast)
WA_LLM_TIMEOUT_MS = int(os.getenv("WA_LLM_TIMEOUT_MS", "15000"))  # default 15s for WA
WA_LLM_MAX_TOKENS = int(os.getenv("WA_LLM_MAX_TOKENS", "36"))    # short replies by default

# Optional "fast" preset via keyword "fast"
WA_LLM_FAST_TOKENS = int(os.getenv("WA_LLM_FAST_TOKENS", "32"))
WA_LLM_FAST_TIMEOUT_MS = int(os.getenv("WA_LLM_FAST_TIMEOUT_MS", "12000"))

# Upper hard cap to avoid huge prompts
WA_LLM_PROMPT_MAX_BYTES = int(os.getenv("WA_LLM_PROMPT_MAX_BYTES", os.getenv("LLM_PROMPT_MAX_BYTES", "12000")))

# Meta / Twilio webhook verification (optional)
WA_VERIFY_TOKEN = os.getenv("WA_VERIFY_TOKEN")
WA_TWILIO_AUTH_TOKEN = os.getenv("WA_TWILIO_AUTH_TOKEN")
WA_PUBLIC_URL = os.getenv("WA_PUBLIC_URL")  # e.g., https://api.example.com

# =========================
#     HELPERS / UTILS
# =========================

def _render_twiml(message: str) -> str:
    msg = _xml_escape(message or "")
    return f'<?xml version="1.0" encoding="UTF-8"?><Response><Message>{msg}</Message></Response>'

@router.get("/health")
async def health() -> Dict[str, str]:
    return {"status": "ok", "component": "wa"}

@router.get("/webhook")
async def webhook_verify(request: Request):
    params = dict(request.query_params)
    if params.get("hub.mode") == "subscribe" and WA_VERIFY_TOKEN and params.get("hub.verify_token") == WA_VERIFY_TOKEN:
        return PlainTextResponse(params.get("hub.challenge", ""), media_type="text/plain")
    return PlainTextResponse("Forbidden", status_code=403, media_type="text/plain")

def _verify_twilio_signature(form_dict: Dict[str, Any], signature: str, public_url: str, auth_token: str) -> bool:
    if not (signature and public_url and auth_token):
        return False
    try:
        items = sorted((str(k), str(v)) for k, v in form_dict.items())
        payload = public_url + "".join(k + v for k, v in items)
        mac = hmac.new(auth_token.encode("utf-8"), payload.encode("utf-8"), hashlib.sha1)
        expected = base64.b64encode(mac.digest()).decode("utf-8")
        return hmac.compare_digest(expected, signature)
    except Exception as e:
        log.warning("WA Twilio signature verify error: %s", e)
        return False

# -------- prompt building (byte-capped, lang-aware) --------
def _truncate_utf8_to_bytes(s: str, max_bytes: int) -> str:
    return s.encode("utf-8")[: max(0, max_bytes)].decode("utf-8", errors="ignore")

def _build_prompt(question: str, lang: str, passages: List[Dict[str, Any]], percent_cap: int, max_bytes: int) -> str:
    header = (
        "You are DocuMind. Answer clearly and concisely using ONLY the evidence below.\n"
        "Do NOT include literal tags like [DOC#…] in your answer; citations are attached separately.\n\n"
        "EVIDENCE:\n"
    )
    qline = f"\n\nQUESTION: {question}\n" + ("Answer in Hindi." if (lang or "en").lower() == "hi" else "Answer in English.")
    total_chars = sum(len(p.get("text", "")) for p in passages) or 1
    pct_chars = int(total_chars * (max(10, min(100, percent_cap)) / 100.0))
    used_bytes = len(header.encode("utf-8"))
    budget = max_bytes - len(qline.encode("utf-8"))
    pieces: List[str] = [header]
    used_chars = 0
    if budget <= 0:
        return _truncate_utf8_to_bytes(header + qline, max_bytes)
    for p in passages:
        t = p.get("text", "") or ""
        if not t:
            continue
        used_chars += len(t)
        if used_chars > pct_chars and pct_chars > 0:
            break
        line = f"[{p.get('doc_id')}#{p.get('chunk', 0)}] {t.strip()}\n"
        lb = len(line.encode("utf-8"))
        if used_bytes + lb > budget:
            remain = budget - used_bytes
            if remain > 0:
                pieces.append(_truncate_utf8_to_bytes(line, remain))
            break
        pieces.append(line); used_bytes += lb
    return _truncate_utf8_to_bytes("".join(pieces) + qline, max_bytes)

# -------- output cleanup --------
_DOC_TAG_CLOSED_RE = re.compile(r"\s*\[(?:DOC|DOC#(?:chunks?|chunk)?)\s*[:\-]?\s*.*?\]\s*", re.I | re.S)
_DOC_TAG_HALFOPEN_RE = re.compile(r"\s*\[(?:DOC|DOC#(?:chunks?|chunk)?)\s*[:\-]?[^\]\n]*$", re.I | re.M)
_SYS_ECHO_RE = re.compile(r"^\s*You are DocuMind\.[^\n]*$", re.I | re.M)
_LANG_ECHO_RE = re.compile(r"^\s*Answer in (?:English|Hindi)\.[^\n]*$", re.I | re.M)
def _sanitize(text: str) -> str:
    t = text or ""
    t = _DOC_TAG_CLOSED_RE.sub(" ", t)
    t = _DOC_TAG_HALFOPEN_RE.sub(" ", t)
    t = _SYS_ECHO_RE.sub("", t)
    t = _LANG_ECHO_RE.sub("", t)
    t = re.sub(r"[ \t]{2,}", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()

# -------- language heuristic --------
_DEVANAGARI = re.compile(r"[\u0900-\u097F]")
def _auto_lang(text: str, current: str) -> str:
    return current or ("hi" if _DEVANAGARI.search(text) else "en")

# -------- command parsing (engine + overrides) --------
_ENGINE_RE = re.compile(r"^\s*(llm|local)\s*[:\-]?\s*(.*)$", re.I)
_LANG_INLINE_RE = re.compile(r"^\s*lang\s+(hi|en)\s*[:\-]?\s*(.*)$", re.I)
# flags anywhere near the front: t= tokens, s= seconds, k= topk, keyword "fast"
_FLAG_RE = re.compile(r"\b(t|tok|tokens|max)\s*=\s*(\d{1,4})\b|\b(s|sec|seconds)\s*=\s*(\d{1,4})\b|\b(k)\s*=\s*(\d{1,2})\b|\bfast\b", re.I)

def _parse_engine_and_overrides(text: str) -> Tuple[str, str, Dict[str, int], bool]:
    """
    Returns (engine, question, overrides, fast_flag)
      engine ∈ {'auto','llm','local'}
      overrides may contain: {'t': int tokens, 's': int seconds, 'k': int topk}
      fast_flag True if keyword 'fast' present
    Strips recognized flags from the question.
    """
    engine = "auto"
    t = text.strip()

    # engine
    m = _ENGINE_RE.match(t)
    if m:
        engine = m.group(1).lower()
        t = m.group(2).strip()

    # inline lang (optional)
    mlang = _LANG_INLINE_RE.match(t)
    lang_override = None
    if mlang:
        lang_override = mlang.group(1).lower()
        t = (mlang.group(2) or "").strip()

    # flags
    overrides: Dict[str, int] = {}
    fast = False
    def _sub_flags(m):
        nonlocal overrides, fast
        if m.group(1):  # t/tok/tokens/max
            overrides["t"] = int(m.group(2))
        elif m.group(3):  # s/sec/seconds
            overrides["s"] = int(m.group(4))
        elif m.group(5):  # k
            overrides["k"] = int(m.group(6))
        else:
            fast = True
        return ""  # remove the flag text

    # Only strip flags from the beginning chunk (first ~60 chars) to avoid nuking legit content later
    head, sep, tail = t[:60], t[60:61], t[61:]
    head_clean = _FLAG_RE.sub(_sub_flags, head)
    question = (head_clean + sep + tail).strip()

    return engine, question, overrides, fast, lang_override

# =========================
#         ROUTE
# =========================

@router.post("/webhook")
async def webhook(request: Request) -> Response:
    lang = WA_DEFAULT_LANG
    dept: Optional[str] = WA_DEFAULT_DEPT
    text: Optional[str] = None
    sender = "unknown"

    ctype = (request.headers.get("content-type") or "").lower()
    try:
        if "application/x-www-form-urlencoded" in ctype:
            form = await request.form()
            form_dict = {k: form.get(k) for k in form.keys()}
            if WA_TWILIO_AUTH_TOKEN and WA_PUBLIC_URL:
                sig = request.headers.get("X-Twilio-Signature") or request.headers.get("x-twilio-signature")
                if not _verify_twilio_signature(form_dict, sig or "", f"{WA_PUBLIC_URL}/api/wa/webhook", WA_TWILIO_AUTH_TOKEN):
                    return PlainTextResponse(_render_twiml("Unauthorized request."), status_code=401, media_type="application/xml")
            text = (form_dict.get("Body") or "").strip()
            lang = (form_dict.get("Lang") or lang).strip() or lang
            dept = form_dict.get("Dept") or dept
            sender = form_dict.get("From") or form_dict.get("WaId") or "unknown"
        else:
            data = await request.json()
            text = str(data.get("message", "")).strip()
            lang = str(data.get("lang") or lang).strip() or lang
            dept = data.get("dept") or dept
            sender = data.get("from") or "unknown"
    except Exception as e:
        log.warning("WA webhook: parse error: %s", e)
        text = None

    if not text:
        help_msg = (
            "Send a question (e.g., 'benefits subsidy').\n"
            "Engine: 'llm <q>' for LLM, 'local <q>' for Local. Default is auto (LLM→fallback).\n"
            "Overrides: t=<tokens> s=<seconds> k=<topk> or 'fast'. Example: 'llm t=32 s=12 benefits?'\n"
            "Language auto-detected; inline 'lang hi: <q>' or 'lang en: <q>' to force."
        )
        return PlainTextResponse(_render_twiml(help_msg), media_type="application/xml")

    engine, question, overrides, fast, lang_inline = _parse_engine_and_overrides(text)
    if lang_inline:
        lang = lang_inline

    if not lang or lang.lower() not in ("en", "hi"):
        lang = _auto_lang(question or text, lang or "")

    # Per-message knobs with WA defaults
    tok = max(8, overrides.get("t", WA_LLM_FAST_TOKENS if fast else WA_LLM_MAX_TOKENS))
    tmo_ms = max(3000, (overrides.get("s", WA_LLM_FAST_TIMEOUT_MS // 1000 if fast else WA_LLM_TIMEOUT_MS // 1000)) * 1000)
    k = min(12, max(1, overrides.get("k", WA_TOPK)))

    # ---------- retrieval ----------
    passages = await retrieve_hybrid(q=question, lang=lang, dept=dept, topk=k)
    fwd = passages[: min(k, len(passages))]
    if not fwd:
        return PlainTextResponse(_render_twiml("No evidence found in the documents."), media_type="application/xml")

    marker = "🟣 LLM"
    answer_text = ""
    cites = [{"doc_id": p.get("doc_id", ""), "chunk": p.get("chunk", 0)} for p in fwd]

    # ---------- engine select ----------
    if engine == "local":
        marker = "🟪 Local"
        if inspect.iscoroutinefunction(generate_answer):
            g = await generate_answer(question=question, lang=lang, passages=fwd, percent_cap=WA_PERCENT_CAP)
        else:
            g = generate_answer(question=question, lang=lang, passages=fwd, percent_cap=WA_PERCENT_CAP)
        answer_text = g.get("answer", "Local summary.")
    else:
        # engine == "llm" or auto
        prompt = _build_prompt(question, lang, fwd, WA_PERCENT_CAP, max_bytes=max(1024, WA_LLM_PROMPT_MAX_BYTES))
        try:
            if _llm_chat is None:
                raise RuntimeError("LLM client unavailable")

            if inspect.iscoroutinefunction(_llm_chat):
                out = await _llm_chat(
                    prompt=prompt,
                    max_tokens=int(tok),
                    timeout=float(tmo_ms) / 1000.0,
                    lang=(lang or "en").lower(),
                )
            else:
                def _call_sync():
                    return _llm_chat(
                        prompt=prompt,
                        max_tokens=int(tok),
                        timeout=float(tmo_ms) / 1000.0,
                        lang=(lang or "en").lower(),
                    )
                out = await anyio.to_thread.run_sync(_call_sync) if anyio else _call_sync()

            if isinstance(out, dict) and (out.get("error") or out.get("status") == "error"):
                raise RuntimeError(str(out.get("error") or "LLM error"))

            answer_text = (out.get("reply") or out.get("response") or out.get("text") or "").strip() if isinstance(out, dict) else str(out).strip()
            if not answer_text:
                raise RuntimeError("Empty LLM reply")

        except Exception as e:
            # strict fallback — never echo raw error
            log.error("WA LLM error: %s — fallback to Local", e)
            marker = "🟪 Local ⏱"
            if inspect.iscoroutinefunction(generate_answer):
                g = await generate_answer(question=question, lang=lang, passages=fwd, percent_cap=WA_PERCENT_CAP)
            else:
                g = generate_answer(question=question, lang=lang, passages=fwd, percent_cap=WA_PERCENT_CAP)
            answer_text = g.get("answer", "Local summary.")

    answer_text = _sanitize(answer_text)
    body = format_wa_reply(answer_text, cites)
    reply = f"{marker}  (t={tok}, s={int(tmo_ms/1000)}s)\n{body}"
    return PlainTextResponse(_render_twiml(reply), media_type="application/xml")
