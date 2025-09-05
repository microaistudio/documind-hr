# Path: Ask/be/services/whatsapp.py
# Product: DocuMind-HR (Ask DocuMind)
# Purpose: Compose concise WhatsApp-ready replies with source tags
# Version: 0.4.0 (2025-08-29)

from typing import Dict, Any, List

def format_wa_reply(
    answer: str,
    citations: List[Dict[str, Any]],
    max_len: int = 600,
    max_sources: int = 3,
    max_chunks_per_source: int = 4,
) -> str:
    """
    Returns a compact text block:
      <answer (trimmed)>
      Source: [DOC#c1,c2]
      Source: [DOC#c3]
    """
    header = (answer or "").strip()
    if len(header) > max_len:
        header = header[: max_len - 1] + "…"

    lines = [header]
    for c in citations[:max_sources]:
        doc = str(c.get("doc_id", "?"))
        chunks = c.get("chunks") or []
        chunks = [str(int(x)) for x in chunks[:max_chunks_per_source] if isinstance(x, (int, str))]
        tag = f"[{doc}#{','.join(chunks)}]" if chunks else f"[{doc}]"
        lines.append(f"Source: {tag}")

    return "\n".join(lines)
