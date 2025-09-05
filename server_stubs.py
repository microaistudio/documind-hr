# server_stubs.py
from fastapi import FastAPI, Body
app = FastAPI()

@app.get("/api/stats")
def stats():
    return {
        "uptime_ms": 1234567,
        "counts": {"documents": 34, "chunks": 72, "embeddings": 72, "sem_summaries": 20, "llm_summaries": 0},
        "features": {"encoding": True, "pgvector": True, "pg_trgm": True},
        "env": {"fusion_alpha": 0.55, "rerank": 1, "rerank_top": 25},
        "avg_ms": {"encode": 9, "semantic": 18, "keyword": 7, "rerank": 22, "total": 63},
        "errors_24h": 0,
    }

@app.get("/api/docs")
def docs(q: str = "", dept: str = "", lang: str = "", type: str = "", from_: str = "", to: str = "", limit: int = 10, page: int = 1):
    return {
        "total": 1, "page": page, "limit": limit,
        "items": [{
            "doc_id": "TEST-20250824-0021", "title": "ANTYODAYA-SARAL",
            "dept": "ANIMAL HUSBANDRY", "lang": "en", "type": "Acts and Rules",
            "stages": {
                "pdf": {"state": "done", "pages": 12},
                "ocr": {"state": "done", "ms": 1320, "chars": 25260},
                "text": {"state": "done", "chars": 25260},
                "chunks": {"state": "done", "count": 7},
                "embeds": {"state": "done", "count": 7, "model": "mpnet-base-v2"},
                "sem_summary": {"state": "db", "chars": 1400},
                "llm_summary": {"state": "none"},
            }
        }]
    }

@app.get("/api/docs/{doc_id}/ocr")
def ocr(doc_id: str):
    return {"text": "Sample OCR text…", "pages": 12, "chars": 25260}

@app.get("/api/docs/{doc_id}/summary")
def summary(doc_id: str, mode: str = "semantic", k: int = 5, probes: int = 11, save: bool = False):
    return {"source": "db" if save else "generated", "text": f"[{mode}] K={k} probes={probes} summary for {doc_id}", "k": k, "probes": probes}

@app.post("/api/docs/{doc_id}/llm_summarize/preview")
def llm_preview(doc_id: str, payload: dict = Body(...)):
    return {"text": f\"LLM preview for {doc_id} (model={payload.get('model')}, k={payload.get('k')})\", "tokens_in": 1200, "tokens_out": 350, "preview_id": "prev_123"}

@app.post("/api/docs/{doc_id}/llm_summarize/save")
def llm_save(doc_id: str, payload: dict = Body(...)):
    return {"ok": True}
