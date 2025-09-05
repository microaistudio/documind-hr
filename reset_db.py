import os, psycopg2
from psycopg2.extras import DictCursor

DSN = os.getenv("DOCUMIND_HR_DSN") or os.getenv("DATABASE_URL") or "postgresql://postgres:postgres@127.0.0.1:5432/documind_hr"

def count(cur, table):
    cur.execute(f"SELECT COUNT(*) FROM {table};")
    return cur.fetchone()[0]

def main():
    conn = psycopg2.connect(DSN); cur = conn.cursor(cursor_factory=DictCursor)
    print("DSN:", DSN)
    before_docs = count(cur, "documents"); before_chunks = count(cur, "chunks")
    print(f"Before -> documents={before_docs} chunks={before_chunks}")
    try:
        cur.execute("TRUNCATE TABLE chunks;")
        cur.execute("TRUNCATE TABLE documents;")
    except Exception as e:
        print("TRUNCATE failed, falling back to DELETE:", e)
        cur.execute("DELETE FROM chunks;")
        cur.execute("DELETE FROM documents;")
    conn.commit()
    after_docs = count(cur, "documents"); after_chunks = count(cur, "chunks")
    print(f"After  -> documents={after_docs} chunks={after_chunks}")
    cur.close(); conn.close()

if __name__ == "__main__":
    main()
