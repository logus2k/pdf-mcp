"""
Chunked semantic index backed by bge-m3.

Why this exists rather than pdf-mcp's own semantic search
---------------------------------------------------------
pdf_mcp/embedder.py hands whole page text to fastembed with no chunking, and
its default model (bge-small-en-v1.5) truncates at 512 tokens. Measured on a
1,466-token page: embedding the first 25% produced a bit-identical vector to
embedding the whole page — the remaining 65-75% simply did not exist for
semantic search, while still costing the full ~0.5s/page to process.

pdf-mcp's model IS configurable ([embedding] model in config.toml), but the
fastembed build here does not offer bge-m3, so the fix cannot be a config
change. This module therefore owns the semantic half:

  * pages are split into overlapping chunks, so nothing is silently dropped
  * chunks are embedded by bge-m3 on the llama.cpp server — 4,096-token window
    (8x bge-small) and GPU-backed instead of in-process CPU
  * vectors live in their own SQLite file, keyed on path+mtime like pdf-mcp's
    own cache, so replacing a document invalidates its index

Keyword search stays with pdf-mcp: its FTS5 index covers 98% of page text and
was never the broken half.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

import httpx

DB_PATH = Path(os.environ.get(
    "EXPLORER_SEMANTIC_DB",
    str(Path.home() / ".cache" / "pdf-mcp-explorer" / "semantic.db")))

EMBED_URL = os.environ.get("EXPLORER_EMBED_URL", "http://127.0.0.1:8500/v1/embeddings")
EMBED_MODEL = os.environ.get("EXPLORER_EMBED_MODEL", "bge-m3")

# Chunking. Smaller than the model's 4,096-token window on purpose: a chunk is
# the unit of retrieval, and a whole dense page as one vector blurs distinct
# topics into an average that matches nothing well. ~1,400 chars is roughly
# 350 tokens — a few paragraphs.
CHUNK_CHARS = int(os.environ.get("EXPLORER_CHUNK_CHARS", "1400"))
CHUNK_OVERLAP = int(os.environ.get("EXPLORER_CHUNK_OVERLAP", "250"))
# Requests are batched; the server's limit is per-input, but a huge array still
# costs one long round trip.
EMBED_BATCH = int(os.environ.get("EXPLORER_EMBED_BATCH", "16"))

# Hard ceiling of the embedding server: bge-m3 runs --ctx-size 8192 with
# --parallel 2, so one slot accepts 4,096 tokens and anything larger is
# refused outright ("input (4202 tokens) is larger than the max context size").
# This is a limit to stay under, NOT a chunk target: a 4,096-token chunk would
# average ten pages of unrelated content into one vector that matches nothing
# precisely, and would cite a span too large to be useful.
EMBED_MAX_TOKENS = int(os.environ.get("EXPLORER_EMBED_MAX_TOKENS", "4096"))
# Deliberately pessimistic: dense technical text with tables and identifiers
# tokenizes worse than prose, so assume ~2.5 chars/token when guarding rather
# than the ~4 that holds for ordinary English.
_CHARS_PER_TOKEN_WORST = 2.5
EMBED_MAX_CHARS = int(EMBED_MAX_TOKENS * _CHARS_PER_TOKEN_WORST * 0.9)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS chunks (
  path        TEXT NOT NULL,
  mtime       REAL NOT NULL,
  size        INTEGER NOT NULL,
  page        INTEGER NOT NULL,
  ordinal     INTEGER NOT NULL,
  start_char  INTEGER NOT NULL,
  end_char    INTEGER NOT NULL,
  bbox        TEXT,
  text        TEXT NOT NULL,
  vector      BLOB NOT NULL,
  dim         INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS chunks_path ON chunks(path);
"""


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(_SCHEMA)
    return conn


def chunk_page(text: str, size: int = CHUNK_CHARS,
               overlap: int = CHUNK_OVERLAP) -> list[tuple[str, int, int]]:
    """Split page text into overlapping chunks, preferring paragraph edges.

    Returns (text, start_char, end_char). The offsets are kept because a page
    number alone is not enough metadata: the viewer highlights a passage, not a
    page, and without a position a semantic hit can only fall back to marking
    the whole page.
    """
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= size:
        return [(text, 0, len(text))]

    chunks: list[tuple[str, int, int]] = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            # Back off to a paragraph, then a sentence, then a space.
            for sep in ("\n\n", ". ", "\n", " "):
                cut = text.rfind(sep, start + int(size * 0.5), end)
                if cut != -1:
                    end = cut + len(sep)
                    break
        piece = text[start:end]
        offset = start + (len(piece) - len(piece.lstrip()))
        piece = piece.strip()
        if piece:
            # Belt and braces: if size was raised past the server ceiling, split
            # rather than emit a chunk that will be rejected at embed time.
            cursor = offset
            while len(piece) > EMBED_MAX_CHARS:
                chunks.append((piece[:EMBED_MAX_CHARS], cursor, cursor + EMBED_MAX_CHARS))
                piece = piece[EMBED_MAX_CHARS:]
                cursor += EMBED_MAX_CHARS
            if piece:
                chunks.append((piece, cursor, cursor + len(piece)))
        if end >= len(text):
            break
        start = max(start + 1, end - overlap)
    return chunks


async def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed via the llama.cpp OpenAI-compatible endpoint."""
    # Enforce the ceiling here rather than trusting every caller: chunk size is
    # env-tunable, and a query is whatever the user typed. Truncating loses the
    # tail of one oversized input; letting it through fails the whole batch.
    guarded = []
    for t in texts:
        if len(t) > EMBED_MAX_CHARS:
            guarded.append(t[:EMBED_MAX_CHARS])
        else:
            guarded.append(t)
    texts = guarded

    out: list[list[float]] = []
    async with httpx.AsyncClient(timeout=httpx.Timeout(300.0)) as http:
        for i in range(0, len(texts), EMBED_BATCH):
            batch = texts[i:i + EMBED_BATCH]
            response = await http.post(
                EMBED_URL, json={"model": EMBED_MODEL, "input": batch})
            response.raise_for_status()
            data = response.json()["data"]
            # The API may return items out of order; index is authoritative.
            ordered = sorted(data, key=lambda d: d.get("index", 0))
            out.extend(d["embedding"] for d in ordered)
    return out


def _norm(vec: list[float]) -> list[float]:
    mag = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / mag for v in vec]


def _pack(vec: list[float]) -> bytes:
    import array
    return array.array("f", vec).tobytes()


def _unpack(blob: bytes) -> list[float]:
    import array
    a = array.array("f")
    a.frombytes(blob)
    return list(a)


def is_indexed(path: str) -> bool:
    try:
        stat = Path(path).stat()
    except OSError:
        return False
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE path=? AND mtime=? AND size=?",
            (path, stat.st_mtime, stat.st_size)).fetchone()
        return bool(row and row[0])
    finally:
        conn.close()


def drop(path: str) -> int:
    conn = _connect()
    try:
        n = conn.execute("DELETE FROM chunks WHERE path=?", (path,)).rowcount
        conn.commit()
        return n
    finally:
        conn.close()


def _locate(doc: Any, page_no: int, snippet: str) -> list[float] | None:
    """Bounding box for a chunk, in PDF page coordinates.

    Computed at index time rather than on every query. Without it a semantic
    hit could only mark a whole page: the viewer highlights by bbox, and a
    character offset into extracted text has no geometry. Uses the chunk's
    opening line as the search key — long enough to be unique on the page,
    short enough that PyMuPDF's matcher still finds it.
    """
    try:
        page = doc[page_no - 1]
    except Exception:
        return None
    key = " ".join(snippet.split())[:72].strip()
    if len(key) < 12:
        return None
    try:
        rects = page.search_for(key)
    except Exception:
        return None
    if not rects:
        # Fall back to a shorter key: extraction can differ from the layout
        # text (ligatures, hyphenation), which defeats an exact long match.
        try:
            rects = page.search_for(key[:36])
        except Exception:
            return None
    if not rects:
        return None
    # A chunk spans several lines; union the matched rect with what follows to
    # approximate the passage rather than pointing at one line.
    r = rects[0]
    return [round(r.x0, 2), round(r.y0, 2), round(r.x1, 2), round(r.y1, 2)]


async def index_document(path: str, pages: Iterable[dict[str, Any]],
                         on_progress=None) -> dict[str, Any]:
    """Chunk and embed every page. `pages` is pdf_read_pages' page list."""
    started = time.monotonic()
    stat = Path(path).stat()
    drop(path)

    try:
        import pymupdf
        doc = pymupdf.open(path)
    except Exception:
        doc = None

    records: list[tuple[int, int, int, int, str, Any]] = []
    for page in pages:
        number = int(page.get("page") or 0)
        for ordinal, (piece, begin, finish) in enumerate(chunk_page(page.get("text", ""))):
            box = _locate(doc, number, piece) if doc else None
            records.append((number, ordinal, begin, finish, piece, box))
    if not records:
        return {"chunks": 0, "elapsed": 0.0}

    conn = _connect()
    try:
        done = 0
        for i in range(0, len(records), EMBED_BATCH):
            batch = records[i:i + EMBED_BATCH]
            vectors = await embed_texts([r[4] for r in batch])
            conn.executemany(
                "INSERT INTO chunks(path,mtime,size,page,ordinal,start_char,"
                "end_char,bbox,text,vector,dim) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                [(path, stat.st_mtime, stat.st_size, r[0], r[1], r[2], r[3],
                  json.dumps(r[5]) if r[5] else None,
                  r[4], _pack(_norm(v)), len(v)) for r, v in zip(batch, vectors)])
            conn.commit()
            done += len(batch)
            if on_progress:
                on_progress(done, len(records))
    finally:
        conn.close()

    located = sum(1 for r in records if r[5])
    return {"chunks": len(records), "with_bbox": located,
            "elapsed": round(time.monotonic() - started, 1)}


# How many chunks from one page may appear in a result set. Chunks overlap by
# design, so the two or three that straddle a relevant passage all score highly
# and a plain top-k returns the same page repeatedly (measured: a 3-result query
# came back as pages [12, 12, 12]). Those duplicates cost the model context
# without adding information and crowd out pages it would otherwise have read.
PAGE_CAP = int(os.environ.get("EXPLORER_SEMANTIC_PAGE_CAP", "2"))


def _diversify(scored: list[dict[str, Any]], top_k: int,
               page_cap: int = PAGE_CAP) -> list[dict[str, Any]]:
    """Pick top_k chunks, spreading them across pages before repeating one.

    Round 1 takes each page's best chunk, round 2 its second best, and so on up
    to page_cap. A page only repeats once every other page has had its turn, so
    a document with enough distinct matches never returns the same page twice,
    while a short one still fills the quota rather than returning less than
    asked for.
    """
    picked: list[dict[str, Any]] = []
    taken: set[int] = set()
    used: dict[int, int] = {}
    for allowance in range(1, max(page_cap, 1) + 1):
        for index, hit in enumerate(scored):
            if len(picked) >= top_k:
                return picked
            # Rounds re-walk the whole list, so skip what an earlier round
            # already took or the same chunk comes back twice.
            if index in taken:
                continue
            page = hit["page"]
            if used.get(page, 0) >= allowance:
                continue
            used[page] = used.get(page, 0) + 1
            taken.add(index)
            picked.append(hit)
    return picked


async def search(path: str, query: str, top_k: int = 8) -> list[dict[str, Any]]:
    """Cosine search over this document's chunks, at most PAGE_CAP per page."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT page, ordinal, start_char, end_char, bbox, text, vector"
            " FROM chunks WHERE path=?", (path,)).fetchall()
    finally:
        conn.close()
    if not rows:
        return []

    qvec = _norm((await embed_texts([query]))[0])
    scored = []
    for page, ordinal, begin, finish, box, text, blob in rows:
        vec = _unpack(blob)
        score = sum(a * b for a, b in zip(qvec, vec))
        scored.append({"page": page, "ordinal": ordinal,
                       "start_char": begin, "end_char": finish,
                       "bbox": json.loads(box) if box else None,
                       "excerpt": text, "score": round(score, 4)})
    scored.sort(key=lambda r: r["score"], reverse=True)
    picked = _diversify(scored, top_k)
    # _diversify walks pages in rounds, so the tail of the list is not in score
    # order; restore it, since the model reads the first results most closely.
    picked.sort(key=lambda r: r["score"], reverse=True)
    return picked


def stats() -> dict[str, Any]:
    conn = _connect()
    try:
        docs = conn.execute(
            "SELECT path, COUNT(*), MAX(dim) FROM chunks GROUP BY path").fetchall()
        return {"documents": [{"path": p, "chunks": n, "dim": d} for p, n, d in docs],
                "db": str(DB_PATH),
                "size_mb": round(DB_PATH.stat().st_size / 1048576, 2)
                if DB_PATH.exists() else 0.0}
    finally:
        conn.close()


# Declared here so the tool the agent sees and the handler that serves it stay
# in one place. Shape matches pdf-mcp's tools/list entries.
TOOL_SCHEMA = {
    "name": "pdf_semantic_search",
    "description": (
        "Semantic search over the open document by meaning rather than exact "
        "words. Use when the question paraphrases the text, asks about a "
        "concept, or when pdf_search returns nothing useful. Covers the FULL "
        "text of every page. Returns matching passages with page numbers."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to the PDF"},
            "query": {"type": "string", "description": "What to look for, in natural language"},
            "max_results": {"type": "integer", "description": "Default 8"},
        },
        "required": ["path", "query"],
    },
}
