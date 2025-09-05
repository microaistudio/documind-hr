# Path: Ask/be/services/llm_client.py
# Version: 3.5.0 (2025-08-31)
# Strict POST-JSON client with HARD BYTE CAP + AUTO-SHRINK retries on "context size" 400s.
# Adds optional length/format hint via env (LLM_STYLE, LLM_LENGTH_HINT).

from __future__ import annotations
import os
from typing import Any, Dict, Optional, Tuple
import httpx

# ---------- Env ----------
def _env(name: str, default: str) -> str:
    v = os.getenv(name)
    return v if (v is not None and str(v).strip() != "") else default

LLM_HTTP_BASE = _env("LLM_HTTP_BASE", "http://127.0.0.1:8000").rstrip("/")
LLM_CHAT_PATH = _env("LLM_CHAT_PATH", "/chat")
LLM_TIMEOUT_MS = int(_env("LLM_TIMEOUT_MS", "60000") or "60000")
LLM_CLIENT_DEBUG = _env("LLM_CLIENT_DEBUG", "0").lower() in ("1","true","yes","on")

# Hard caps (bytes). Tune in .env.hr
PROMPT_MAX_BYTES = int(_env("LLM_PROMPT_MAX_BYTES", "9000") or "9000")
PROMPT_TAIL_BYTES = int(_env("LLM_PROMPT_TAIL_BYTES", "2200") or "2200")
LLM_AUTOSHRINK = _env("LLM_AUTOSHRINK", "1").lower() in ("1","true","yes","on")

# NEW: length/format hints
LLM_STYLE = _env("LLM_STYLE", "").strip().lower()          # "bullets" | "bullet" | "paragraph" | ""
LLM_LENGTH_HINT = _env("LLM_LENGTH_HINT", "").strip()      # free text, e.g., "Write 6–10 bullets (~150–220 words)."

if LLM_CLIENT_DEBUG:
    print(f"[llm_client v3.5.0] base={LLM_HTTP_BASE} path={LLM_CHAT_PATH} cap={PROMPT_MAX_BYTES}B tail={PROMPT_TAIL_BYTES}B autoshrink={LLM_AUTOSHRINK}")
    if LLM_STYLE or LLM_LENGTH_HINT:
        print(f"[llm_client] hints: style={LLM_STYLE or '-'}; length_hint={'set' if LLM_LENGTH_HINT else 'none'}")

# ---------- Byte-safe truncation ----------
def _truncate_utf8_to_bytes(s: str, max_bytes: int) -> str:
    if max_bytes <= 0:
        return ""
    return s.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")

def _cap_prompt_bytes_keep_tail(prompt: str, max_bytes: int, tail_bytes: int) -> str:
    b = prompt.encode("utf-8")
    if len(b) <= max_bytes:
        return prompt
    tail = max(0, min(tail_bytes, max_bytes // 2))
    head = max_bytes - tail
    head_s = b[:head].decode("utf-8", errors="ignore")
    tail_s = b[-tail:].decode("utf-8", errors="ignore")
    capped = head_s + "\n…\n" + tail_s
    capped = _truncate_utf8_to_bytes(capped, max_bytes)
    if LLM_CLIENT_DEBUG:
        print(f"[llm_client] capped initial prompt: {len(b)}B -> {len(capped.encode('utf-8'))}B (head {head}B, tail {tail}B)")
    return capped

def _apply_hints(prompt: str, lang: Optional[str]) -> str:
    """Append small length/format guidance so answers aren't one-liners."""
    parts = []
    st = LLM_STYLE
    if st in ("bullets", "bullet", "list"):
        parts.append("Format: bullet points.")
    elif st in ("paragraph", "para", "paragraphs"):
        parts.append("Format: 2–3 short paragraphs.")
    if LLM_LENGTH_HINT:
        parts.append(LLM_LENGTH_HINT)
    if not parts:
        return prompt
    # keep it minimal; put after QUESTION so it stays in the preserved tail
    hint = " ".join(parts)
    # keep language directive implicit; your router already says 'Answer in Hindi/English.'
    return f"{prompt}\n\nLENGTH & FORMAT: {hint}"

# ---------- Response parsing ----------
def _extract_text_and_tokens(resp: httpx.Response) -> Tuple[str, Optional[int]]:
    text = ""
    used: Optional[int] = None
    data: Any = None
    try:
        data = resp.json()
    except Exception:
        data = None

    if isinstance(data, dict):
        text = (
            data.get("reply")
            or data.get("response")
            or data.get("text")
            or data.get("answer")
            or data.get("result")
            or data.get("completion")
            or ""
        )
        if not text and isinstance(data.get("data"), dict):
            dd = data["data"]
            text = dd.get("text") or dd.get("reply") or dd.get("response") or dd.get("answer") or ""
        if not text and isinstance(data.get("choices"), list) and data["choices"]:
            ch0 = data["choices"][0]
            text = ((ch0.get("message") or {}).get("content")) or ch0.get("text") or ""

        usage = data.get("usage")
        if isinstance(usage, dict):
            used = usage.get("total_tokens") or (
                (usage.get("prompt_tokens") or 0) + (usage.get("completion_tokens") or 0)
            )

    if not text:
        text = resp.text or ""

    if used is None:
        try:
            used = int(resp.headers.get("x-total-tokens") or 0) or None
        except Exception:
            used = None

    return (text or "").strip(), used

def _is_context_size_error(resp_text: str) -> bool:
    t = (resp_text or "").lower()
    return (
        "context size" in t
        or "max context" in t
        or "exceeds the available context" in t
        or "token limit" in t
        or "too many tokens" in t
    )

async def _post_prompt(prompt: str, timeout_s: float, lang: Optional[str]) -> httpx.Response:
    url = f"{LLM_HTTP_BASE}{LLM_CHAT_PATH}"
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "Accept": "application/json",
    }
    if (lang or "").lower() == "hi":
        headers["Accept-Language"] = "hi-IN,hi;q=0.9"
    payload: Dict[str, Any] = {"prompt": prompt}
    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_s)) as client:
        return await client.post(url, json=payload, headers=headers)

# ---------- Public API ----------
async def chat(
    *,
    prompt: str,
    max_tokens: int,                 # kept for signature compatibility; NOT sent
    timeout: float = 30.0,
    temperature: Optional[float] = None,       # ignored
    top_p: Optional[float] = None,             # ignored
    presence_penalty: Optional[float] = None,  # ignored
    frequency_penalty: Optional[float] = None, # ignored
    lang: Optional[str] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """
    Strict client to satisfy gateway needing only {'prompt': ...}, with:
      - length/format hint append
      - hard byte cap
      - auto-shrink retries on 400 context errors
    Returns {'reply': str, 'used_tokens': int|None}.
    """
    timeout_s = float(timeout if (timeout and timeout > 0) else (LLM_TIMEOUT_MS / 1000.0))

    # 0) Apply optional length/format hints
    hinted = _apply_hints(prompt, lang)

    # 1) Initial cap
    original_bytes = len(hinted.encode("utf-8"))
    capped = _cap_prompt_bytes_keep_tail(hinted, PROMPT_MAX_BYTES, PROMPT_TAIL_BYTES)
    capped_bytes = len(capped.encode("utf-8"))
    if LLM_CLIENT_DEBUG:
        print(f"[llm_client] prompt sizes: original={original_bytes}B, sending={capped_bytes}B (limit={PROMPT_MAX_BYTES}B)")

    # 2) Attempt + optional auto-shrink retries on 400 context errors
    shrink_factors = [1.0, 0.7, 0.5, 0.35, 0.25] if LLM_AUTOSHRINK else [1.0]
    last_err = None
    last_resp_text = ""

    for f in shrink_factors:
        if f < 1.0:
            cap = max(1500, int(PROMPT_MAX_BYTES * f))
            tail = max(600, min(PROMPT_TAIL_BYTES, int(cap * 0.35)))
            capped_try = _cap_prompt_bytes_keep_tail(hinted, cap, tail)
            if LLM_CLIENT_DEBUG:
                print(f"[llm_client] autoshrink try @{int(f*100)}% -> cap={cap}B tail={tail}B (send={len(capped_try.encode('utf-8'))}B)")
        else:
            capped_try = capped

        try:
            r = await _post_prompt(capped_try, timeout_s, lang)
            if LLM_CLIENT_DEBUG:
                print(f"[llm_client] POST JSON (prompt-only, {len(capped_try.encode('utf-8'))}B) -> {r.status_code}")
            r.raise_for_status()
            text, used = _extract_text_and_tokens(r)
            if not text and LLM_CLIENT_DEBUG:
                print(f"[llm_client] 200 but empty. Body head: {(r.text or '')[:240]}")
            return {"reply": text, "used_tokens": used}
        except httpx.HTTPStatusError as e:
            status = getattr(e.response, "status_code", 0)
            try:
                last_resp_text = e.response.text or ""
            except Exception:
                last_resp_text = ""
            last_err = f"HTTP {status}"
            if LLM_CLIENT_DEBUG:
                print(f"[llm_client] HTTP {status}: {e} | body: {last_resp_text[:240]}")
            if status == 400 and _is_context_size_error(last_resp_text) and f != shrink_factors[-1]:
                continue
            return {"reply": "", "used_tokens": None, "error": last_err}
        except Exception as e:
            last_err = str(e)
            if LLM_CLIENT_DEBUG:
                print(f"[llm_client] transport error: {e}")
            return {"reply": "", "used_tokens": None, "error": last_err}

    return {"reply": "", "used_tokens": None, "error": last_err or "context_too_large"}
