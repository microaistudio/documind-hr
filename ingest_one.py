# Version: 3.1.0
# Path: /srv/documind-hr/ingest_one.py
# Purpose: Insert ONE PDF into documind_hr (text-only, no embeddings). For Hindi/English smoke tests.

import os, io, uuid, hashlib, sys
from datetime import datetime
import psycopg2
from psycopg2.extras import execute_batch
from pypdf import PdfReader

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:5432/documind_hr")

def pdf_to_text_and_pages(pdf_path: str):
    with open(pdf_path, "rb") as f:
        data = f.read()
    reader = PdfReader(io.BytesIO(data))
    texts = []
    for p in reader.pages:
        try:
            texts.append(p.extract_text() or "")
        except Exception:
            texts.append("")
    return "\n".join(texts).strip(), len(reader.pages), data

def chunk_text(text: str, max_chars: int = 1400, overlap: int = 150):
    text = " ".join(text.split())
    out, i, n = [], 0, len(text)
    while i < n:
        j = min(i + max_chars, n)
        out.append(text[i:j])
        i = max(0, j - overlap)
    return [c for c in out if c.strip()]

def make_doc_code(prefix: str, dept: str):
    today = datetime.now().strftime("%Y%m%d")
    suffix = uuid.uuid4().hex[:4].upper()
    return f"{prefix}-{dept[:3].upper()}-{today}-{suffix}"

def main():
    if len(sys.argv) < 5:
        print("Usage: python ingest_one.py <pdf_path> <dept:animal|labour> <lang:en|hi> <title...>")
        sys.exit(1)

    pdf_path = sys.argv[1]
    dept = sys.argv[2].strip().lower()
    lang = sys.argv[3].strip().lower()
    title = " ".join(sys.argv[4:]).strip()

    if not os.path.exists(pdf_path):
        print(f"File not found: {pdf_path}")
        sys.exit(2)

    text, pages, file_bytes = pdf_to_text_and_pages(pdf_path)
    if not text:
        print("No extractable text (likely scanned). We’ll handle OCR in a later step.")
        sys.exit(3)

    chunks = chunk_text(text)
    characters = len(text)
    sha1 = hashlib.sha1(file_bytes).hexdigest()
    file_size = len(file_bytes)
    doc_uuid = uuid.uuid4()
    doc_id = str(doc_uuid)  # machine id
    doc_code = make_doc_code("HR", dept)  # human-facing code for citations

    with psycopg2.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO documents
                (id, doc_id, doc_code, title, dept, lang, path, pages, characters, chunks,
                 issued_by, document_type, issued_date, tags, metadata, file_size, sha1, ocr)
                VALUES
                (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                 %s,%s,%s,%s,%s,%s,%s,%s)
            """, (
                doc_uuid, doc_id, doc_code, title, dept, lang, pdf_path, pages, characters, len(chunks),
                None, None, None, None, None, file_size, sha1, False
            ))
            execute_batch(cur, """
                INSERT INTO chunks (id, document_id, chunk_index, text, char_count, embedding)
                VALUES (%s,%s,%s,%s,%s, NULL)
            """, [
                (uuid.uuid4(), doc_uuid, idx, c, len(c))
                for idx, c in enumerate(chunks)
            ])

    print(f"OK: inserted {len(chunks)} chunks for '{title}' as {doc_code}")

if __name__ == "__main__":
    main()
