# mini_ingest.py — RAM-safe direct insert for one PDF
import os, sys, uuid, hashlib, psycopg2
from psycopg2.extras import DictCursor, execute_values
from pypdf import PdfReader
EMBED_DIM = 768

DSN = os.getenv('DOCUMIND_HR_DSN') or os.getenv('DATABASE_URL') or 'postgresql://postgres:postgres@localhost:5432/documind_hr'

def read_pdf_text(path):
    r = PdfReader(path)
    text = "".join((p.extract_text() or "") for p in r.pages)
    return r, text

def chunk_text(txt, size=1200):
    return [txt[i:i+size] for i in range(0, len(txt), size)] or [""]

def get_id_type(cur):
    cur.execute("""SELECT data_type FROM information_schema.columns
                   WHERE table_schema='public' AND table_name='documents' AND column_name='id'""")
    return cur.fetchone()[0]

def next_pk(cur, id_type):
    if id_type in ('integer','bigint'):
        cur.execute("SELECT COALESCE(MAX(id),0)+1 FROM documents")
        return cur.fetchone()[0]
    else:
        return str(uuid.uuid4())

def sha1_of(path):
    h = hashlib.sha1()
    with open(path,'rb') as f:
        for chunk in iter(lambda: f.read(1024*1024), b''):
            h.update(chunk)
    return h.hexdigest(), os.path.getsize(path)

def ingest_one(path, doc_id, dept, lang):
    r, full = read_pdf_text(path)
    pages, characters = len(r.pages), len(full)
    chunks = chunk_text(full, 1200)
    sha1, fsize = sha1_of(path)
    title = os.path.basename(path)

    conn = psycopg2.connect(DSN); cur = conn.cursor(cursor_factory=DictCursor)

    cur.execute("SELECT id FROM documents WHERE doc_id=%s", (doc_id,))
    row = cur.fetchone()
    if row:
        doc_pk = str(row['id'])
    else:
        id_type = get_id_type(cur)
        doc_pk = next_pk(cur, id_type)

    cur.execute("DELETE FROM chunks WHERE document_id=%s", (doc_pk,))
    cur.execute("DELETE FROM documents WHERE id=%s", (doc_pk,))
    cur.execute("""INSERT INTO documents
                   (id, doc_id, title, dept, lang, path, pages, characters, chunks, file_size, sha1, ocr)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (doc_pk, doc_id, title, dept, lang, path, pages, characters, len(chunks), fsize, sha1, False))

    ZERO = '[' + ','.join(['0']*EMBED_DIM) + ']'
    data = [(str(uuid.uuid4()), doc_pk, i, c, ZERO, len(c)) for i, c in enumerate(chunks)]
    execute_values(cur, "INSERT INTO chunks (id, document_id, chunk_index, text, embedding, char_count) VALUES %s", data, template="(%s,%s,%s,%s,%s::vector,%s)")

    conn.commit(); cur.close(); conn.close()
    print(f"OK doc_id={doc_id} id={doc_pk} pages={pages} chars={characters} chunks={len(chunks)}")

if __name__ == "__main__":
    if len(sys.argv) != 5:
        print("usage: python mini_ingest.py <pdf_path> <doc_id> <dept> <lang>")
        sys.exit(1)
    ingest_one(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4])
