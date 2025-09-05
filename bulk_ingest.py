import os, sys, re, glob, uuid, hashlib, shutil, tempfile, subprocess
from pathlib import Path
from typing import Tuple, Optional, List
import psycopg2
from psycopg2.extras import DictCursor, execute_values
from pypdf import PdfReader

# Try to load the embedding helpers; if unavailable, we’ll fall back to zeros.
try:
    from src.utils.embeddings import encode, to_pgvector
    _EMB_OK = True
except Exception:
    encode = to_pgvector = None
    _EMB_OK = False

# --- Config defaults (overridable by env, CLI, or prompts) ---
DSN = os.getenv('DOCUMIND_HR_DSN') or os.getenv('DATABASE_URL') or 'postgresql://postgres:postgres@127.0.0.1:5432/documind_hr'
DEFAULT_ROOT     = os.getenv('INGEST_ROOT',   'data/pdfs')
DEFAULT_CHUNK    = int(os.getenv('CHUNK',     '900'))
DEFAULT_OVERLAP  = int(os.getenv('OVERLAP',   '100'))

DEFAULT_FORCE_OCR_HI = os.getenv('FORCE_OCR_HI', '1')      # keep HI OCR on by default
DEFAULT_FORCE_OCR_EN = os.getenv('FORCE_OCR_EN', '0')      # EN OCR opt-in

# OCR language packs (tweakable via env)
OCR_LANG_HI = os.getenv('OCR_LANG_HI', 'hin+eng')  # better for mixed pages
OCR_LANG_EN = os.getenv('OCR_LANG_EN', 'eng')

EMBED_DIM = 768  # matches your DB

# -------- Utilities ----------
def to_bool(x) -> bool:
    return str(x).strip().lower() in ('1','y','yes','true','on')

def sha1_of(path: str):
    h = hashlib.sha1()
    with open(path, 'rb') as f:
        for b in iter(lambda: f.read(1024*1024), b''):
            h.update(b)
    return h.hexdigest(), os.path.getsize(path)

def extract_pypdf_text(path: str):
    r = PdfReader(path)
    text = "".join((p.extract_text() or "") for p in r.pages)
    return len(r.pages), text

def _run(cmd: list) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

def ocr_pdf(path: str, lang_code: str) -> Tuple[int, str]:
    """OCR any PDF with tesseract for the given lang code (e.g., 'eng', 'hin+eng')."""
    tmp = tempfile.mkdtemp(prefix=f"ocr_{lang_code.replace('+','')}_")
    try:
        out = os.path.join(tmp, "pg")
        r = _run(["pdftoppm","-r","300","-png",path,out])
        if r.returncode != 0:
            raise RuntimeError("pdftoppm failed: "+r.stderr.decode("utf-8","ignore"))
        pages = sorted(glob.glob(out+"-*.png"))
        texts = []
        for img in pages:
            r = _run(["tesseract", img, "stdout", "-l", lang_code, "--oem","1","--psm","6"])
            if r.returncode != 0:
                raise RuntimeError("tesseract failed: "+r.stderr.decode("utf-8","ignore"))
            texts.append(r.stdout.decode("utf-8","ignore"))
        full = re.sub(r"[\s\u00A0]+", " ", "".join(texts)).strip()
        return len(pages), full
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

def devanagari_ratio(s: str) -> float:
    if not s: return 0.0
    dev = sum(1 for ch in s if '\u0900' <= ch <= '\u097F')
    return dev / max(1, len(s))

def latin_ratio(s: str) -> float:
    if not s: return 0.0
    lat = sum(1 for ch in s if ('A' <= ch <= 'Z') or ('a' <= ch <= 'z'))
    return lat / max(1, len(s))

def good_hi_text(s: str) -> bool:
    # heuristic: has some Devanagari and enough length
    return len(s) >= 200 and devanagari_ratio(s) >= 0.20

def good_en_text(s: str) -> bool:
    # heuristic: has enough Latin characters and length
    return len(s) >= 200 and latin_ratio(s) >= 0.20

def chunk_text(txt: str, size: int, overlap: int) -> List[str]:
    size = max(1, int(size))
    overlap = max(0, int(overlap))
    step = max(1, size - overlap)
    out = []
    i = 0
    n = len(txt)
    while i < n:
        out.append(txt[i:i+size])
        i += step
    return out or [""]

def detect_dept_lang(path: str):
    parts = Path(path).parts
    try:
        idx = parts.index('pdfs')
        return parts[idx+1].lower(), parts[idx+2].lower()
    except Exception:
        dept = 'animal' if '/animal/' in path else ('labour' if '/labour/' in path else 'unknown')
        lang = 'hi' if '/hi/' in path else ('en' if '/en/' in path else 'en')
        return dept, lang

def make_doc_id(cur, dept: str, lang: str, src: str) -> str:
    import re
    base = re.sub(r'[^A-Za-z0-9]', '', dept).upper()  # "hp-rural" -> "HPRURAL"
    code = (base[:3] or 'GEN').ljust(3, 'X')          # -> "HPR"
    prefix = f"{code}-{lang.upper()}-{src.upper()}-"
    cur.execute("SELECT doc_id FROM documents WHERE doc_id LIKE %s", (prefix+'%',))
    max_n = 0
    for (doc_id,) in cur.fetchall():
        try: max_n = max(max_n, int(doc_id.split('-')[-1]))
        except: pass
    return f"{prefix}{max_n+1:04d}"

def next_doc_pk(cur) -> str:
    return str(uuid.uuid4())

def upsert_document(cur, *, doc_pk, doc_id, title, dept, lang, path, pages, characters, chunks, file_size, sha1, ocr):
    cur.execute("DELETE FROM documents WHERE id=%s", (doc_pk,))
    cur.execute("""
      INSERT INTO documents (id, doc_id, title, dept, lang, path, pages, characters, chunks, file_size, sha1, ocr)
      VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
    """, (doc_pk, doc_id, title, dept, lang, path, pages, characters, chunks, file_size, sha1, ocr))

# -- Embedding helper ----------------------------------------------------------
_WARNED_EMB = False
def _embed_texts(texts: List[str]) -> List[str]:
    """
    Returns a list of pgvector strings, one per text.
    Falls back to zero vectors if embeddings are unavailable or fail.
    """
    global _WARNED_EMB
    ZERO = "[" + ",".join(["0"]*EMBED_DIM) + "]"
    if not texts:
        return []
    if not _EMB_OK:
        if not _WARNED_EMB:
            print("WARN: embeddings unavailable (using ZERO vectors). Install sentence-transformers & add src/utils/embeddings.py", file=sys.stderr)
            _WARNED_EMB = True
        return [ZERO] * len(texts)
    try:
        vecs = encode([t[:1000] for t in texts])  # safety slice
        return [to_pgvector(v) for v in vecs]
    except Exception as e:
        if not _WARNED_EMB:
            print(f"WARN: embedding encode failed ({e}); using ZERO vectors.", file=sys.stderr)
            _WARNED_EMB = True
        return [ZERO] * len(texts)

def insert_chunks(cur, doc_pk: str, texts):
    vstrs = _embed_texts(list(texts))
    data = [(str(uuid.uuid4()), doc_pk, i, c, vstrs[i], len(c)) for i,c in enumerate(texts)]
    execute_values(cur,
        "INSERT INTO chunks (id, document_id, chunk_index, text, embedding, char_count) VALUES %s",
        data,
        template="(%s,%s,%s,%s,%s::vector,%s)"
    )

def process_file(conn, path: str, *, overwrite: bool, force_ocr_hi: bool, force_ocr_en: bool, chunk: int, overlap: int):
    path = os.path.abspath(path)
    if not path.lower().endswith('.pdf'): return None
    dept, lang = detect_dept_lang(path)
    sha1, fsize = sha1_of(path); title = os.path.basename(path)

    with conn.cursor(cursor_factory=DictCursor) as cur:
        # path-based tracking
        cur.execute("SELECT id, doc_id, sha1 FROM documents WHERE path=%s", (path,))
        row = cur.fetchone()
        if row:
            if not overwrite and row['sha1'] == sha1:
                return f"SKIP (unchanged) {title}"
            doc_pk = row['id']; doc_id = row['doc_id']
        else:
            doc_pk = next_doc_pk(cur); doc_id = None

        # extract or OCR
        ocr_used = False
        if lang == 'hi' and force_ocr_hi:
            pages, text = ocr_pdf(path, OCR_LANG_HI); ocr_used = True
        elif lang == 'en' and force_ocr_en:
            pages, text = ocr_pdf(path, OCR_LANG_EN); ocr_used = True
        else:
            pages, text = extract_pypdf_text(path)
            # auto-fallback if text looks weak
            if lang == 'hi' and not good_hi_text(text):
                pages, text = ocr_pdf(path, OCR_LANG_HI); ocr_used = True
            elif lang == 'en' and not good_en_text(text):
                pages, text = ocr_pdf(path, OCR_LANG_EN); ocr_used = True

        texts = chunk_text(text, chunk, overlap)
        src = 'OCR' if ocr_used else 'DATA'
        if not doc_id:
            doc_id = make_doc_id(cur, dept, lang, src)

        cur.execute("DELETE FROM chunks WHERE document_id=%s", (doc_pk,))
        upsert_document(cur,
            doc_pk=doc_pk, doc_id=doc_id, title=title, dept=dept, lang=lang, path=path,
            pages=pages, characters=sum(len(t) for t in texts), chunks=len(texts),
            file_size=fsize, sha1=sha1, ocr=bool(ocr_used)
        )
        insert_chunks(cur, doc_pk, texts)
        conn.commit()
        return f"OK {doc_id} {title} pages={pages} chunks={len(texts)} src={src}"

# -------- Main (CLI + interactive prompts) ----------
def main():
    import argparse
    ap = argparse.ArgumentParser(description="Bulk ingest PDFs (EN text, HI OCR; OCR fallback for both; embeddings on write).")
    ap.add_argument("--root", default=None, help=f"Root folder (default: {DEFAULT_ROOT})")
    ap.add_argument("--dept", help="Only this department (e.g., animal, labour, hp-rural)")
    ap.add_argument("--lang", choices=["en","hi"], help="Only this language")
    ap.add_argument("--chunk", type=int, default=None, help=f"Chunk size (default: {DEFAULT_CHUNK})")
    ap.add_argument("--overlap", type=int, default=None, help=f"Chunk overlap (default: {DEFAULT_OVERLAP})")
    ap.add_argument("--overwrite", action="store_true", help="Reprocess even if sha1 unchanged")
    ap.add_argument("--force-ocr-hi", action="store_true", help=f"Force OCR for Hindi (default: {DEFAULT_FORCE_OCR_HI})")
    ap.add_argument("--force-ocr-en", action="store_true", help=f"Force OCR for English (default: {DEFAULT_FORCE_OCR_EN})")
    ap.add_argument("--dry-run", action="store_true", help="List work without writing DB")
    ap.add_argument("--no-prompt", action="store_true", help="Skip prompts; use CLI/env defaults")
    args = ap.parse_args()

    # decide whether to prompt
    prompt = not args.no_prompt and (len(sys.argv) == 1 or args.root is None and not any([args.dept, args.lang, args.chunk, args.overlap, args.overwrite, args.force_ocr_hi, args.force_ocr_en, args.dry_run]))
    # base defaults
    root    = args.root    if args.root    is not None else DEFAULT_ROOT
    chunk   = args.chunk   if args.chunk   is not None else DEFAULT_CHUNK
    overlap = args.overlap if args.overlap is not None else DEFAULT_OVERLAP
    overwrite    = args.overwrite
    force_ocr_hi = args.force_ocr_hi or to_bool(DEFAULT_FORCE_OCR_HI)
    force_ocr_en = args.force_ocr_en or to_bool(DEFAULT_FORCE_OCR_EN)
    dept = args.dept
    lang = args.lang

    if prompt:
        print("DocuMind-HR • Bulk Ingest (press Enter to accept defaults)")
        root    = input(f"Root directory [{root}]: ").strip() or root
        d_in    = input(f"Department (animal/labour/hp-rural or blank=all) [{dept or 'all'}]: ").strip().lower()
        dept    = d_in or None
        l_in    = input(f"Language (en/hi or blank=all) [{lang or 'all'}]: ").strip().lower()
        lang    = l_in or None
        try:
            chunk = int(input(f"Chunk size [{chunk}]: ").strip() or chunk)
        except: pass
        try:
            overlap = int(input(f"Chunk overlap [{overlap}]: ").strip() or overlap)
        except: pass
        overwrite    = to_bool(input(f"Overwrite existing (y/N) [{'Y' if overwrite else 'N'}]: ").strip() or ('Y' if overwrite else 'N'))
        force_ocr_hi = to_bool(input(f"Force OCR for Hindi (y/N) [{'Y' if force_ocr_hi else 'N'}]: ").strip() or ('Y' if force_ocr_hi else 'N'))
        force_ocr_en = to_bool(input(f"Force OCR for English (y/N) [{'Y' if force_ocr_en else 'N'}]: ").strip() or ('Y' if force_ocr_en else 'N'))

    # collect files
    if not os.path.isdir(root):
        print("No such dir:", root); sys.exit(1)

    pdfs = []
    for dirpath, _, files in os.walk(root):
        for f in files:
            if f.lower().endswith(".pdf"):
                p = os.path.join(dirpath, f)
                d, l = detect_dept_lang(p)
                if dept and d != dept: continue
                if lang and l != lang: continue
                pdfs.append(p)
    pdfs.sort()

    print(f"\nPlan: files={len(pdfs)} root={root} dept={dept or 'ALL'} lang={lang or 'ALL'} chunk={chunk} overlap={overlap} overwrite={overwrite} force_ocr_hi={force_ocr_hi} force_ocr_en={force_ocr_en}")
    if prompt:
        go = input("Proceed? (Y/n): ").strip().lower()
        if go and go not in ('y','yes'):
            print("Aborted."); return

    if not pdfs:
        print("Nothing to ingest."); return

    conn = psycopg2.connect(DSN)
    try:
        with conn: pass
        for p in pdfs:
            if args.dry_run:
                d,l = detect_dept_lang(p)
                print(f"DRY {os.path.relpath(p, root)} dept={d} lang={l}")
                continue
            msg = process_file(conn, p,
                overwrite=overwrite, force_ocr_hi=force_ocr_hi, force_ocr_en=force_ocr_en,
                chunk=chunk, overlap=overlap
            )
            if msg: print(msg)
    finally:
        conn.close()

if __name__ == "__main__":
    main()
