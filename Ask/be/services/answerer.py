# Path: Ask/be/services/answerer.py
# Product: DocuMind-HR (Ask DocuMind)
# Purpose: Grounded answerer with demo toggles and Hindi-safe LLM path.
# Version: 0.8.0 (2025-08-31)
#  - Forward lang to LLM (POST JSON), no implicit language gate.
#  - Return rich 'meta' for the UI (source, timings, tokens, k/evidence_k/passages).
#  - Keep local (fallback) bullets + WA post-processing.
#  - Respect ASK_* env knobs for generation controls.

from typing import List, Dict, Any
from collections import defaultdict
import os, time

try:
    # Expected to expose: async def call_llm(payload: Dict[str, Any]) -> Dict[str, Any]
    from Ask.be.services.llm_client import call_llm  # POST {"prompt": "...", "...": ...}
except Exception:
    call_llm = None  # type: ignore

# ======= Env toggles (demo-friendly) =======
ASK_USE_LLM = (os.getenv("ASK_USE_LLM", "true").lower() in ("1", "true", "yes"))
ASK_SHOW_SOURCES = (os.getenv("ASK_SHOW_SOURCES", "false").lower() in ("1", "true", "yes"))
ASK_PERCENT_CAP = int(os.getenv("ASK_PERCENT_CAP", "60"))

# Generation knobs (used only for LLM path; safe defaults)
ASK_MAX_TOKENS = int(os.getenv("ASK_MAX_TOKENS", "512"))
ASK_TEMPERATURE = float(os.getenv("ASK_TEMPERATURE", "0.2"))
ASK_TOP_P = float(os.getenv("ASK_TOP_P", "1.0"))
ASK_PRESENCE_PENALTY = float(os.getenv("ASK_PRESENCE_PENALTY", "0"))
ASK_FREQUENCY_PENALTY = float(os.getenv("ASK_FREQUENCY_PENALTY", "0"))
ASK_TIMEOUT_MS = int(os.getenv("ASK_TIMEOUT_MS", "60000"))

SYSTEM_EN = (
    "You are DocuMind Assistant.\n"
    "Answer ONLY using the evidence. Be extractive and concise.\n"
    "Format: 2–4 short bullets that directly answer the question.\n"
    "Do NOT include long citations inside bullets. If unsure, say so.\n"
    "Keep the whole reply short (aim < 420 characters)."
)

SYSTEM_HI = (
    "आप DocuMind सहायक हैं। केवल दिए गए प्रमाण से संक्षिप्त उत्तर दें।\n"
    "प्रारूप: 2–4 छोटे बुलेट जो सीधे प्रश्न का उत्तर दें।\n"
    "बुलेट में लम्बे उद्धरण न जोड़ें। यदि स्पष्ट न हो तो बताएं।\n"
    "कुल उत्तर संक्षिप्त रखें (लगभग 420 अक्षरों तक)।"
)

# ---------- helpers ----------
def _group_citations(passages: List[Dict[str, Any]], max_docs: int = 3, max_chunks_per_doc: int = 4):
    by_doc = defaultdict(list)
    for p in passages:
        by_doc[str(p.get("doc_id", "?"))].append(int(p.get("chunk", 0)))
    items = []
    for doc_id, chunks in by_doc.items():
        uniq = sorted({int(c) for c in chunks})[:max_chunks_per_doc]
        items.append({"doc_id": doc_id, "chunks": uniq})
        if len(items) >= max_docs:
            break
    return items

def _sources_line(passages: List[Dict[str, Any]]) -> str:
    grouped = _group_citations(passages)
    if not grouped:
        return ""
    parts = []
    for it in grouped:
        if it["chunks"]:
            parts.append(f"{it['doc_id']}#{','.join(str(n) for n in it['chunks'])}")
        else:
            parts.append(it["doc_id"])
    return "Sources: [" + "; ".join(parts) + "]"

def _confidence(passages: List[Dict[str, Any]]) -> float:
    k = min(len(passages), 10)
    return round(min(0.9, 0.5 + 0.04 * k), 2)

def _cap_passages(passages: List[Dict[str, Any]], percent_cap: int) -> List[Dict[str, Any]]:
    """Soft cap on evidence size by approximate characters."""
    budget = int(9000 * max(10, min(percent_cap, 100)) / 100)  # ~9k chars @100%
    used = 0
    kept = []
    for p in passages:
        t = str(p.get("text") or "")
        rem = budget - used
        if rem <= 0:
            break
        if len(t) > rem:
            q = dict(p)
            q["text"] = t[:max(200, rem)]
            kept.append(q)
            break
        kept.append(p)
        used += len(t)
    return kept

def _postprocess_wa(text: str, passages: List[Dict[str, Any]]) -> str:
    """Tighten for WA/web: remove inline 'Source:' lines and append compact Sources if enabled."""
    lines = [ln.rstrip() for ln in (text or "").splitlines()]
    cleaned = []
    for ln in lines:
        low = ln.lower().strip()
        if low.startswith("source:") or low.startswith("sources:"):
            continue
        cleaned.append(ln)
    body = "\n".join([ln for ln in cleaned if ln])[:420].rstrip()
    if ASK_SHOW_SOURCES:
        src = _sources_line(passages)
        if src:
            body = (body + ("\n" if body else "") + src).strip()
    return body[:480]

# ---------- prompt builder ----------
def _build_llm_payload(question: str, lang: str, passages: List[Dict[str, Any]], percent_cap: int) -> Dict[str, Any]:
    system = SYSTEM_HI if (lang or "en").lower() == "hi" else SYSTEM_EN
    capped = _cap_passages(passages, percent_cap)
    header = f"{system}\n\nQUESTION:\n{question}\n\nEVIDENCE:\n"
    blocks = []
    for p in capped:
        blocks.append(f"[{p.get('doc_id')}#{p.get('chunk')}]\n{p.get('text','')}\n")
    guide = "\nWrite the final answer now as 2–4 short bullets. No long citations in bullets."
    prompt = header + "\n".join(blocks) + guide
    # Forward commonly supported controls to the gateway (keep payload simple/portable)
    return {
        "prompt": prompt,
        "lang": (lang or "en").lower(),  # some gateways honor this; harmless otherwise
        "max_tokens": ASK_MAX_TOKENS,
        "temperature": ASK_TEMPERATURE,
        "top_p": ASK_TOP_P,
        "presence_penalty": ASK_PRESENCE_PENALTY,
        "frequency_penalty": ASK_FREQUENCY_PENALTY,
        "timeout_ms": ASK_TIMEOUT_MS,
    }

# ---------- local extractive fallback (no-LLM mode) ----------
def _fix_leading_trunc(s: str) -> str:
    """
    Some upstream cleanup can drop the very first character of a line.
    Restore the most common cases without changing meaning.
    """
    if not s:
        return s
    x = s.lstrip()
    lowers = x.lower()
    fixes = [
        ("nd ", "and "),
        ("r ", "or "),
        ("pplying ", "applying "),
        ("griculture ", "agriculture "),
        ("nterest ", "interest "),
        ("eneficiary ", "beneficiary "),
        ("cheme ", "Scheme "),
        ("oans ", "loans "),
    ]
    for bad, good in fixes:
        if lowers.startswith(bad):
            x = good + x[len(bad):]
            return x
    return x

def _local_extractive(passages: List[Dict[str, Any]], lang: str) -> str:
    bullets = []
    for p in passages[:4]:
        t = (p.get("text") or "").strip()
        if not t:
            continue
        cut = t.split(". ")[0]
        if len(cut) > 220:
            cut = cut[:220] + "..."
        cut = _fix_leading_trunc(cut)
        bullets.append("• " + cut)
        if len(bullets) >= 4:
            break
    if not bullets:
        return "No clear answer found in the documents." if (lang or "en").lower() != "hi" else "दस्तावेज़ों में स्पष्ट उत्तर नहीं मिला।"
    return "\n".join(bullets)

# ---------- public entry ----------
async def generate_answer(
    question: str,
    lang: str,
    passages: List[Dict[str, Any]],
    percent_cap: int = ASK_PERCENT_CAP,
) -> Dict[str, Any]:
    """
    Returns:
      {
        "answer": str,
        "citations": [ {doc_id, chunks[]} ],
        "confidence": float,
        "grounded": bool,
        "meta": {
          "source": "llm" | "local",
          "took_ms": int,
          "llm_elapsed_ms": int | None,
          "k": int,                 # total candidate passages
          "evidence_k": int,        # actually forwarded/capped passages
          "passages": int,          # same as evidence_k for UI convenience
          "used_tokens": int | None,
          "max_tokens": int
        }
      }
    """
    start = time.perf_counter()
    lang = (lang or "en").lower()
    k_total = len(passages)
    capped = _cap_passages(passages, percent_cap)
    evidence_k = len(capped)

    # Guard: no passages at all
    if not passages:
        took_ms = int((time.perf_counter() - start) * 1000)
        msg = "No clear answer found in the documents." if lang != "hi" else "दस्तावेज़ों में स्पष्ट उत्तर नहीं मिला।"
        return {
            "answer": msg,
            "citations": [],
            "confidence": 0.0,
            "grounded": False,
            "meta": {
                "source": "local",
                "took_ms": took_ms,
                "llm_elapsed_ms": None,
                "k": 0,
                "evidence_k": 0,
                "passages": 0,
                "used_tokens": None,
                "max_tokens": ASK_MAX_TOKENS,
            },
        }

    # No-LLM path (toggle or missing client)
    if (not ASK_USE_LLM) or (call_llm is None):
        ans = _postprocess_wa(_local_extractive(capped, lang), capped)
        took_ms = int((time.perf_counter() - start) * 1000)
        return {
            "answer": ans,
            "citations": _group_citations(capped),
            "confidence": _confidence(capped),
            "grounded": True,
            "meta": {
                "source": "local",
                "took_ms": took_ms,
                "llm_elapsed_ms": None,
                "k": k_total,
                "evidence_k": evidence_k,
                "passages": evidence_k,
                "used_tokens": None,
                "max_tokens": ASK_MAX_TOKENS,
            },
        }

    # LLM path
    llm_elapsed = None
    used_tokens = None
    try:
        payload = _build_llm_payload(question, lang, capped, percent_cap)
        t0 = time.perf_counter()
        llm = await call_llm(payload)
        llm_elapsed = int((time.perf_counter() - t0) * 1000)

        # Accept multiple shapes
        reply = ""
        if isinstance(llm, dict):
            reply = (
                llm.get("answer")
                or llm.get("reply")
                or llm.get("response")
                or llm.get("text")
                or ""
            )
            # token accounting if provided by gateway
            usage = llm.get("usage") if isinstance(llm.get("usage"), dict) else None
            if usage:
                used_tokens = usage.get("total_tokens") or (
                    (usage.get("prompt_tokens") or 0) + (usage.get("completion_tokens") or 0)
                )
        else:
            reply = str(llm or "")

        if not isinstance(reply, str) or not reply.strip():
            raise RuntimeError("empty llm reply")

        ans = _postprocess_wa(reply.strip(), capped)
        took_ms = int((time.perf_counter() - start) * 1000)
        return {
            "answer": ans,
            "citations": _group_citations(capped),
            "confidence": _confidence(capped),
            "grounded": True,
            "meta": {
                "source": "llm",
                "took_ms": took_ms,
                "llm_elapsed_ms": llm_elapsed,
                "k": k_total,
                "evidence_k": evidence_k,
                "passages": evidence_k,
                "used_tokens": used_tokens,
                "max_tokens": ASK_MAX_TOKENS,
            },
        }

    except Exception:
        # Fallback to local extractive bullets
        ans = _postprocess_wa(_local_extractive(capped, lang), capped)
        took_ms = int((time.perf_counter() - start) * 1000)
        return {
            "answer": ans,
            "citations": _group_citations(capped),
            "confidence": _confidence(capped),
            "grounded": True,
            "meta": {
                "source": "local",  # explicit fallback marker
                "took_ms": took_ms,
                "llm_elapsed_ms": llm_elapsed,
                "k": k_total,
                "evidence_k": evidence_k,
                "passages": evidence_k,
                "used_tokens": used_tokens,
                "max_tokens": ASK_MAX_TOKENS,
            },
        }
