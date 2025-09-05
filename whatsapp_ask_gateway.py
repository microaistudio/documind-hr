# Path: whatsapp_ask_gateway.py
# Version: 3.3.2-wa.ask (explicit LLM = push-only ack)
# WhatsApp → DocuMind Ask (local) with LLM worker follow-ups.

import os, time, asyncio
from typing import Dict
import httpx
from fastapi import FastAPI, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, JSONResponse
from html import escape as xml_escape

# === Env knobs ===
WA_BUDGET_MS       = int(os.getenv("WA_BUDGET_MS", "12000"))
WA_LLM_TIMEOUT_MS  = int(os.getenv("WA_LLM_TIMEOUT_MS", "7500"))
WA_REPLY_MS        = int(os.getenv("WA_REPLY_MS", "2000"))
WA_TWO_MESSAGE     = os.getenv("WA_TWO_MESSAGE", "1").lower() in {"1","true","yes","on"}
WA_MAX_TOKENS      = int(os.getenv("WA_MAX_TOKENS", "128"))
WA_PERCENT_CAP     = int(os.getenv("WA_PERCENT_CAP", "35"))
WA_LANG_HINT       = os.getenv("WA_LANG_HINT", "en").lower()
WA_LLM_BUSY_FALLBACK_PUSH = os.getenv("WA_LLM_BUSY_FALLBACK_PUSH", "1").lower() in {"1","true","yes","on"}

ASK_HTTP_BASE      = os.getenv("ASK_HTTP_BASE", "http://127.0.0.1:9000").rstrip("/")
ASK_ENDPOINT       = os.getenv("ASK_API_URL", f"{ASK_HTTP_BASE}/api/wa/webhook")

LLM_WORKER_BASE    = os.getenv("LLM_WORKER_BASE", "http://127.0.0.1:8011").rstrip("/")
LLM_ANSWER_URL     = f"{LLM_WORKER_BASE}/api/wa/answer"
LLM_PUSH_URL       = f"{LLM_WORKER_BASE}/api/wa/push"

FORWARD_TIMEOUT_S  = int(os.getenv("FORWARD_TIMEOUT_S", "12"))
TRIM_SOFT = 900

app = FastAPI(title="WhatsApp Ask Gateway")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

# === utils ===
def _twiml(text: str) -> Response:
    msg = (text or "…").strip()
    xml = f'<?xml version="1.0" encoding="UTF-8"?><Response><Message>{xml_escape(msg)}</Message></Response>'
    return Response(content=xml, media_type="application/xml; charset=utf-8")

def _trim(s: str, n: int = TRIM_SOFT) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[:n-1].rstrip() + "…"

def _normalize_wa(s: str) -> str:
    s = (s or "").strip().replace("whatsapp: ", "whatsapp:+")
    if s.startswith("whatsapp:"):
        left, right = s.split(":", 1); right = right.strip()
        if right and right[0].isdigit(): right = "+" + right
        s = f"{left}:{right}"
    return s

def _label(kind: str, ms: int) -> str:
    secs = max(1, int(round(ms/1000.0)))
    return f"🟥 Local ⏱ (≈{secs}s)" if kind.lower() == "local" else f"🟣 LLM ⏱ (≈{secs}s)"

# === remote calls ===
async def ask_forward_twiml(form: Dict[str, str]) -> str:
    try:
        async with httpx.AsyncClient(timeout=FORWARD_TIMEOUT_S) as cli:
            r = await cli.post(ASK_ENDPOINT, data=form)
            if r.status_code >= 400 or not (r.text or "").strip():
                return '<Response><Message>Local summary unavailable.</Message></Response>'
            return r.text
    except Exception as e:
        return f'<Response><Message>Local error: {e}</Message></Response>'

async def llm_push(to_wa: str, q: str) -> None:
    payload = {"to": _normalize_wa(to_wa), "q": q,
               "timeout_ms": WA_LLM_TIMEOUT_MS, "max_tokens": WA_MAX_TOKENS}
    try:
        async with httpx.AsyncClient(timeout=(WA_LLM_TIMEOUT_MS/1000.0)+2.0) as cli:
            r = await cli.post(LLM_PUSH_URL, json=payload)
            r.raise_for_status()
    except Exception:
        pass  # best-effort

# === health ===
@app.get("/twilio/health")
async def twilio_health() -> JSONResponse:
    return JSONResponse({
        "ok": True,
        "budget_ms": WA_BUDGET_MS,
        "llm_timeout_ms": WA_LLM_TIMEOUT_MS,
        "reply_ms": WA_REPLY_MS,
        "two_message": WA_TWO_MESSAGE,
        "ask_endpoint": ASK_ENDPOINT,
        "llm_worker_base": LLM_WORKER_BASE,
        "llm_busy_fallback_push": WA_LLM_BUSY_FALLBACK_PUSH,
    })

# === webhook ===
@app.post("/twilio/whatsapp")
async def twilio_whatsapp(
    Body: str = Form(""),
    From: str = Form(""),
    To: str = Form(""),
):
    t0 = time.time()
    body = (Body or "").strip()
    from_id = _normalize_wa(From)
    to_id = _normalize_wa(To)

    lower = body.lower()
    is_local = lower.startswith("local") or lower.endswith(" local") or lower == "local"
    is_more  = lower in {"more","details"} or lower.startswith("llm") or lower.startswith("llm:")

    # 1) Force local-only
    if is_local:
        xml = await ask_forward_twiml({"Body": body, "From": from_id, "To": to_id})
        return Response(content=xml, media_type="application/xml")

    # 2) Default: two-message (local now + worker push later)
    if WA_TWO_MESSAGE and not is_more:
        asyncio.create_task(llm_push(from_id, body))
        xml = await ask_forward_twiml({"Body": body, "From": from_id, "To": to_id})
        return Response(content=xml, media_type="application/xml")

    # 3) Explicit LLM — push-only ack (no single-shot call)
    q = body.replace("llm", "", 1).strip() if lower.startswith("llm") else body
    if WA_LLM_BUSY_FALLBACK_PUSH:
        asyncio.create_task(llm_push(from_id, q))
    ms = int((time.time() - t0) * 1000)
    header = _label("LLM", ms)
    # send a friendly ack instead of “busy”; the real answer will arrive via worker push
    return _twiml(_trim(f"{header}\nWorking on it… I’ll send details shortly.", TRIM_SOFT))
