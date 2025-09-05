# Path: services/llm_client.py
# Purpose: Unified LLM client; tolerant to JSON or text/plain; supports overrides + language
# Version: 0.2.1 (2025-08-29)

import os, httpx
from typing import List, Dict, Optional, Any
from functools import lru_cache

def _extract_text_from_json(j: Any) -> str:
    """
    Try a variety of common shapes:
      - OpenAI: {"choices":[{"message":{"content":"..."}}], "usage":{...}}
      - Simple: {"reply":"..."}, {"response":"..."}, {"text":"..."}, {"result":"..."},
                {"content":"..."}, {"output":"..."}
      - Raw string: "..."
    """
    if j is None:
        return ""
    if isinstance(j, str):
        return j.strip()
    if isinstance(j, dict):
        # OpenAI-ish
        try:
            ch = j.get("choices")
            if isinstance(ch, list) and ch:
                msg = ch[0].get("message") if isinstance(ch[0], dict) else None
                if isinstance(msg, dict):
                    content = msg.get("content")
                    if isinstance(content, str):
                        return content.strip()
        except Exception:
            pass
        # Simple keys
        for k in ("reply", "response", "text", "result", "content", "output"):
            v = j.get(k)
            if isinstance(v, str):
                return v.strip()
    # Unknown shape
    return ""

class LLMClient:
    """
    Modes:
      - kind=openai  -> /v1/chat/completions (POST JSON)
      - kind=chatui  -> simple /chat endpoint, GET or POST based on env

    Env (chatui):
      LLM_HTTP_BASE, LLM_CHAT_PATH, LLM_CHAT_METHOD, LLM_CHAT_QUERY_PARAM
    Env (openai):
      LLM_API_BASE, LLM_API_PATH (/v1/chat/completions), LLM_API_KEY
    """
    def __init__(self):
        self.kind = os.getenv("LLM_KIND", "chatui").lower()

        # Accept both env styles (compat with your setup).
        self.base = (os.getenv("LLM_API_BASE") or os.getenv("LLM_HTTP_BASE") or "").rstrip("/")
        self.path = os.getenv("LLM_API_PATH") or os.getenv("LLM_CHAT_PATH") or "/chat"
        self.model = os.getenv("LLM_MODEL", "mistral-7b")
        self.temp = float(os.getenv("LLM_TEMPERATURE", "0.2"))
        self.max_tokens = int(os.getenv("LLM_MAX_TOKENS", "768"))
        self.timeout_ms = int(os.getenv("LLM_TIMEOUT_MS", os.getenv("LLM_DEADLINE_MS","120000")))
        self.api_key = os.getenv("LLM_API_KEY", "")
        self.chat_method = (os.getenv("LLM_CHAT_METHOD", "POST") or "POST").upper()
        self.query_key = os.getenv("LLM_CHAT_QUERY_PARAM", "q")

        if not self.base:
            raise RuntimeError("LLM base URL not set (LLM_HTTP_BASE or LLM_API_BASE)")

        if self.kind == "openai" and not self.path:
            self.path = "/v1/chat/completions"
        elif self.kind == "chatui" and not self.path:
            self.path = "/chat"

        self.url = f"{self.base}{self.path}"
        self.headers = {"Content-Type": "application/json"}
        if self.api_key:
            self.headers["Authorization"] = f"Bearer {self.api_key}"

        # httpx client (timeout per-request can override)
        self.client = httpx.AsyncClient(timeout=self.timeout_ms / 1000)
        self.last_usage: Optional[Dict[str, Any]] = None

    async def chat(
        self,
        messages: List[Dict[str, str]],
        *,
        temperature: Optional[float]=None,
        max_tokens: Optional[int]=None,
        top_p: Optional[float]=None,
        timeout_ms: Optional[int]=None,
    ) -> str:
        self.last_usage = None
        temperature = self._pick(temperature, self.temp)
        max_tokens = self._pick(max_tokens, self.max_tokens)
        top_p = self._pick(top_p, 1.0)
        timeout = (timeout_ms or self.timeout_ms) / 1000

        if self.kind == "openai":
            payload = {
                "model": self.model,
                "messages": messages,
                "temperature": temperature,
                "top_p": top_p,
                "max_tokens": max_tokens,
            }
            r = await self.client.post(self.url, headers={**self.headers}, json=payload, timeout=timeout)
            r.raise_for_status()
            try:
                j = r.json()
            except Exception:
                # openai should always be json; if not, treat as text fallback
                return (r.text or "").strip()
            self.last_usage = j.get("usage")
            txt = _extract_text_from_json(j)
            return txt

        # chatui: build a single prompt from messages (last user)
        prompt = ""
        for m in messages[::-1]:
            if m.get("role") == "user":
                prompt = m.get("content","")
                break

        method = self.chat_method if self.chat_method in ("GET","POST") else "POST"
        if method == "GET":
            # Not great for long/Unicode prompts, but supported if your gateway expects it.
            params = { self.query_key: prompt }
            r = await self.client.get(self.url, headers={**self.headers}, params=params, timeout=timeout)
        else:
            # POST JSON – safer for Hindi/long prompts
            payload = { self.query_key: prompt, "prompt": prompt }
            r = await self.client.post(self.url, headers={**self.headers}, json=payload, timeout=timeout)

        r.raise_for_status()

        # Try JSON first; if it fails, use raw text
        txt: str = ""
        try:
            j = r.json()
            txt = _extract_text_from_json(j)
        except Exception:
            txt = (r.text or "").strip()

        return txt

    async def summarize(
        self,
        text: str,
        style: str = "bullet",
        *,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        presence_penalty: Optional[float] = None,   # kept for API compat
        frequency_penalty: Optional[float] = None,  # kept for API compat
        timeout_ms: Optional[int] = None,
        lang: Optional[str] = None,
    ) -> str:
        # Minimal, model-agnostic instruction. Keeps backward compatibility.
        lang_hint = ""
        if lang and str(lang).lower().startswith("hi"):
            lang_hint = "\nReturn the summary in Hindi (हिन्दी)."

        prompt = (
            "Summarize the following text.\n"
            f"Style: {style}. Keep it concise and factual.{lang_hint}\n\n"
            f"TEXT:\n{text}\n\nSummary:"
        )
        return await self.chat(
            [{"role": "user", "content": prompt}],
            temperature=temperature,
            max_tokens=max_tokens,
            top_p=top_p,
            timeout_ms=timeout_ms,
        )

    @staticmethod
    def _pick(v: Optional[Any], default: Any) -> Any:
        return default if v is None else v

@lru_cache(maxsize=1)
def get_llm() -> LLMClient:
    return LLMClient()
