# pdf-mcp explorer

Chat over a PDF: the document rendered beside a conversation, where every answer
is produced by reading the actual pages and cites them, and clicking a citation
jumps the viewer to the passage and highlights it.

This repository contains **two layers that are easy to confuse**:

| layer | what it is | who owns it |
|---|---|---|
| **pdf-mcp** | third-party MCP server (PyPI `pdf-mcp`, v2.2.0). Extraction, FTS5 keyword search, TOC, rendering, chart extraction. 13 tools. | upstream |
| **explorer** | this repo's FastAPI app in `explorer/`. Spawns pdf-mcp over stdio, adds semantic search, figure descriptions, document maps, the agent loop and the UI. | us |

Nothing here patches pdf-mcp. The explorer drives it as a subprocess and adds
the halves it does not cover.

---

## 1. Division of labour

Understand this before changing anything; most of the design follows from it.

**pdf-mcp owns** page text extraction (with OCR for scanned pages), the FTS5
keyword index, the embedded PDF outline, page rendering, and a SQLite cache
keyed on `file_path + file_mtime`.

**The explorer owns** everything semantic:

- `semantic.py` — chunked **bge-m3** vector index. Exists because pdf-mcp
  embeds whole pages with bge-small, which truncates at **512 tokens**.
  Measured on a 1,466-token page: embedding the first 25% produced a
  bit-identical vector to embedding the whole page. The tail of every long page
  was invisible to semantic search while still costing the full extraction time.
  pdf-mcp's `[embedding] model` is configurable, but the bundled fastembed build
  does not offer bge-m3, so this could not be fixed by configuration.
- `figures.py` — figure descriptions via a multimodal model.
- `summarizer.py` — generated navigation map for documents whose own table of
  contents cannot navigate them.
- `agent.py` — the tool loop, prompt, and citation extraction.
- `providers.py` — local (llama.cpp/OpenAI-compatible) and Claude behind one
  streaming interface.
- `voice.py` — TTS relay.

Keyword search stays entirely with pdf-mcp. Its FTS5 index was never the broken
half; only the semantic half needed replacing.

---

## 2. Integration surfaces

There are three ways to consume this, in increasing order of coupling.

### 2.1 Use pdf-mcp directly (no explorer)

The MCP server is standalone and needs nothing from this app. `.mcp.json`:

```json
{ "mcpServers": { "pdf-mcp": {
    "command": "/path/to/.venv_pdf-mcp/bin/pdf-mcp", "args": [], "env": {} } } }
```

`opencode.json` shows the equivalent for opencode. **stdio is the transport to
use** — over HTTP the tool surface is reduced.

**Access control is machine-global and hardcoded.** `pdf_mcp/config.py` reads
`~/.config/pdf-mcp/config.toml` with no environment or CLI override. Every
pdf-mcp process on the host shares that file, so widening the allow-list for one
integration widens it for all of them. In the container the file is `COPY`d to
exactly that path (`docker/pdf-mcp-config.toml`) and confines reads to
`/documents`, with `[urls] deny = ["*"]` so the server cannot fetch remote URLs.

### 2.2 Call the explorer's HTTP API

The app is a normal FastAPI service. Endpoints that matter to an integrator:

| endpoint | purpose |
|---|---|
| `POST /api/chat` | the agent turn. Body: `{messages, provider, document, all_tools, use_map}`. Streams SSE. |
| `GET /api/events` | server-sent events: indexing progress, document list changes |
| `POST /api/upload` | multipart PDF upload; validates `%PDF-` header and size |
| `POST /api/warm` · `GET /api/warm` | start indexing / read progress for every document |
| `POST /api/warm/cancel` | abort an in-flight import and purge what it wrote (keeps the file) |
| `POST /api/document/delete` | delete the PDF **and** everything indexed from it |
| `GET /api/documents` · `GET /api/document?path=` | list / stream a PDF |
| `GET /api/outline?path=` | embedded outline, else the printed TOC recovered at ingest |
| `GET /api/map?path=` · `POST /api/map` | read / build the generated navigation map |
| `GET /api/semantic` | vector index stats |
| `GET /api/tools?document=` | pdf-mcp's tools plus `pdf_semantic_search` when indexed |
| `POST /api/call` | call any pdf-mcp tool directly: `{name, arguments}` |
| `GET /api/health` | reports the pdf-mcp child's liveness, not just the web app |

`/api/chat` SSE event types: `start`, `token`, `tool_call`, `tool_result`
(carries `citations`, `chars`, `preview`, `images`, `is_error`), `tool_error`,
`answer`, `error`, and a final `end`.

### 2.3 Import the modules

`semantic.py`, `figures.py` and `summarizer.py` have no FastAPI dependency and
can be imported directly. `semantic.py` needs only an OpenAI-compatible
`/v1/embeddings` endpoint; `figures.py` and `summarizer.py` need a chat model.

---

## 3. Data stores

Three stores, all keyed on `path + mtime (+ size)` so replacing a file
invalidates its entries automatically.

**pdf-mcp cache** — `~/.cache/pdf-mcp/cache.db` plus extracted images on disk.
Tables all carry `file_path`: `pdf_metadata`, `page_text`, `page_images`,
`page_embeddings`, `pdf_search_fts`, and others. **There is no per-path clear**:
`pdf_cache_clear` accepts only `expired_only`, so deleting one document's
entries means deleting rows directly (see `_purge_mcp_cache` in `app.py`, which
discovers tables at runtime, skips FTS shadow tables, and unlinks the PNGs that
`page_images.file_path_on_disk` points at).

**Semantic index** — `~/.cache/pdf-mcp-explorer/semantic.db`, one table:

```sql
CREATE TABLE chunks (
  path TEXT, mtime REAL, size INTEGER,      -- cache key
  page INTEGER, ordinal INTEGER,
  start_char INTEGER, end_char INTEGER,     -- offsets within the page text
  bbox TEXT,                                -- JSON [x0,y0,x1,y1], PDF coords
  text TEXT,
  vector BLOB,                              -- float32 array, 1024 dims (bge-m3)
  dim INTEGER,
  kind TEXT DEFAULT 'text'                  -- 'text' | 'figure'
);
```

It is a vector database: embeddings in, cosine nearest-neighbour out, with
metadata filtering by `path`. No server, no ANN index — a BLOB column and a
dot-product loop. Search is a brute-force linear scan.

**Maps** — `~/.cache/pdf-mcp-explorer/maps/<hash>.json`: chapters with
`one_line`, `frameworks`, `terms`, a digest, a topic index, and `printed_toc`.

Volume mapping: `pdf_mcp_cache` → `/root/.cache/pdf-mcp` (pdf-mcp's own cache);
`pdf_mcp_maps` → `/root/.cache/pdf-mcp-explorer` (**both** explorer stores — the
semantic index and the maps). Named volumes, so a rebuild does not discard
indexing work.

---

## 4. Retrieval design

Two search tools reach the model, and the split is deliberate.

**`pdf_search`** — pdf-mcp's FTS5 keyword index. Strong on exact tokens
(`R17`, `SysML`, section numbers).

**`pdf_semantic_search`** — served by the explorer, not pdf-mcp. Chunked
bge-m3, 1,400 chars with 250 overlap, capped at `PAGE_CAP = 2` chunks per page.
The cap exists because overlapping chunks around one passage all score highly;
a plain top-k returned pages `[12, 12, 12]`, spending context on duplicates.

**The system prompt must name the semantic tool explicitly.** The model would
otherwise never choose it: `pdf_search` almost always returns *something*, so
"use it when keyword search fails" never triggers. Measured — four paraphrased
questions in a row answered from `pdf_search` alone, until `agent.py` was
changed to state the choice. The prompt addition is appended only when the tool
is actually in the list.

**Do not add RRF fusion.** Measured on 30 paraphrased queries with known target
pages: fusing keyword and dense **lowered** R@1 from 27% to 17%. This matches
the literature — fusion harms an already-strong dense retriever.

| retriever | R@1 | R@3 | R@5 | R@10 |
|---|---|---|---|---|
| keyword (FTS5) | 3% | 10% | 27% | 37% |
| dense (bge-m3) — shipped | 27% | 50% | 50% | 70% |
| RRF fusion | 17% | 33% | 57% | 70% |

**Reranking chunks does not help either** — a cross-encoder over the same dense
candidates dropped R@3 from 50% to 40%. The residual failures are diffuse
conceptual queries ("make sure everything is included") where dozens of chunks
genuinely discuss the concept; re-scoring cannot create a distinction the text
does not contain. Reranking *section titles* did help (R@1 40% → 47%), which is
an unbuilt direction, not a shipped feature.

Caveat on all of the above: n=30, one document. Differences under ~10pp are noise.

---

## 5. Figures

The local model is multimodal, so figures are described at ingest and indexed as
`kind='figure'` chunks — searchable and citable like text.

**Capability is probed, never assumed.** A text-only server answers an image
request with HTTP 200 and confident prose about nothing. At startup the app
sends a red PNG and checks the model names the colour
(`EXPLORER_INDEX_FIGURES=auto|on|off`).

**Two detectors, unioned**, because each alone has a measured blind spot:

- **embedded images**, minus furniture — an image appearing on ≥4 pages is a
  logo, not a figure (one appeared on all 115 pages of a test document). Misses
  vector diagrams, which are drawn rather than placed.
- **captions** — `Figure 3:` counts, `Figure 4 shows an…` does not; the
  separator after the number is what distinguishes a caption from a mention.
  Contents pages are excluded by leader-dot density. Misses figures whose
  caption is part of the artwork.

Vector density is deliberately *not* a third detector: thresholds that catch a
real diagram also flagged 65–80 of 115 pages, because callout boxes are drawn
with the same primitives.

Whole pages are sent, not crops — the crop loses the caption that names the
figure. `RENDER_SCALE = 2.0`; higher is worse (at 4.0 the model found none of a
diagram's key elements).

Descriptions are **partial and nondeterministic** on dense diagrams. One page
answered "NO FIGURE" on 2 of 4 attempts, and temperature 0 made recall *worse*
by deterministically refusing — hence the retry that tells the model a figure
was already detected there. Treat descriptions as searchable pointers, not
authoritative readings; results are tagged `kind: "figure"` with a note that
they were generated from the page image.

---

## 6. Document map

Built only when a document's own TOC cannot navigate it (`EXPLORER_BUILD_MAP=auto`).
Measured over 144 A/B turns: the map **hurts** where a usable TOC exists
(`pdf_get_toc` is lossless and free) and **helps** where there is none.

When there is no embedded outline, the front matter is scanned for a **printed**
contents listing and parsed by the model into entries. Printed page numbers are
offset from PDF indices (front matter numbered i, ii, iii), so entries vote on
the delta by having their titles located in the document; the majority wins.
Chapters are contiguous — short sections merge into neighbours rather than being
dropped, which previously punched holes through the map.

---

## 7. Ingestion pipeline

Queued, one worker, priority `upload > discovered > startup sweep`. Phases:
`extracting` → `semantic` → `figures` → `embedding` → `mapping` → `ready`.

**Every phase is guarded on `mtime + size`.** Without the guards a restart
re-embedded every page of every document and rebuilt every map — minutes of GPU
work to reproduce identical rows. Note `semantic.index_document` calls
`drop_text()`, *not* `drop()`, so figure descriptions survive text re-indexing;
reversing that makes the figure guard always miss.

Measured on a warm restart with guards in place: 2.1 s / 6.2 s / 26.8 s for
28-, 115- and 472-page documents.

---

## 8. Configuration

All optional; defaults in brackets.

**Service** — `EXPLORER_HOST` [0.0.0.0], `EXPLORER_PORT` [8090],
`EXPLORER_PDF_MCP_BIN`, `EXPLORER_MAX_UPLOAD_MB`.

**Models** — `EXPLORER_LLM_BASE` [http://127.0.0.1:8500/v1], `EXPLORER_LLM_MODEL`
(override; otherwise discovered per request from `/v1/models`),
`EXPLORER_MAX_TOKENS` [32768], `EXPLORER_MAX_STEPS`, `EXPLORER_TOOL_BUDGET`,
`ANTHROPIC_API_KEY`, `EXPLORER_CLAUDE_MODEL`, `EXPLORER_CLAUDE_CONCURRENCY`.

**Semantic** — `EXPLORER_EMBED_URL`, `EXPLORER_EMBED_MODEL` [bge-m3],
`EXPLORER_CHUNK_CHARS` [1400], `EXPLORER_CHUNK_OVERLAP` [250],
`EXPLORER_EMBED_MAX_TOKENS` [4096], `EXPLORER_EMBED_BATCH` [16],
`EXPLORER_SEMANTIC_PAGE_CAP` [2].

**Figures** — `EXPLORER_INDEX_FIGURES` [auto], `EXPLORER_FIGURE_MIN_COVERAGE`
[0.02], `EXPLORER_FIGURE_BOILERPLATE_PAGES` [4], `EXPLORER_FIGURE_RENDER_SCALE`
[2.0], `EXPLORER_FIGURE_CONCURRENCY` [0 = discover from `--parallel`].

**Map** — `EXPLORER_BUILD_MAP` [auto], `EXPLORER_MAP_PROVIDER` [local],
`EXPLORER_MAP_MAX_CHAPTERS` [40], `EXPLORER_MAP_CHAPTER_CHARS` [18000],
`EXPLORER_MAP_CONCURRENCY` [0], `EXPLORER_TOC_SCAN_PAGES` [20].

**Voice** — `EXPLORER_TTS_URL`, `EXPLORER_STT_URL`, `EXPLORER_TTS_VOICE`,
`EXPLORER_TTS_SPEED`, `EXPLORER_TTS_MAX_CHARS`, `EXPLORER_TTS_TIMEOUT`.

Concurrency defaults of `0` mean "discover the model server's `--parallel` at
run time" rather than pinning a number that goes stale.

---

## 9. Deployment

```bash
docker compose --profile default up -d --build   # --profile is required
```

One image holds both halves, with **separate dependency trees**: pdf-mcp in its
own virtualenv (`/opt/pdf-mcp-venv`), the app in system site-packages, so a
version bump on one cannot drag the other.

Publishes host port **8090**. Container joins `logus2k_network`;
`host.docker.internal:host-gateway` reaches the host's llama.cpp.

**Never run `python explorer/app.py` on the host while the container is up** —
both bind 8090 via `SO_REUSEPORT` and requests split silently between them.

---

## 10. Gotchas

Hard-won; each cost real debugging time.

**Bounding boxes are top-left, viewports are bottom-left.** pdf-mcp returns
PyMuPDF coordinates; `convertToViewportRectangle` expects PDF coordinates.
Highlights must be flipped about `page.view[3]` or they render mirrored.

**pdf.js's hidden canvas is a flex item.** It appends a canvas to `<body>`;
inside a flex layout it steals space and collapses the panes. `body > canvas {
position: absolute !important; visibility: hidden; }`.

**Frontend uses relative URLs throughout**, so the app works at the origin root
and under a path prefix with no build step. Keep it that way.

**The health check reports the MCP child**, so a dead subprocess marks the
container unhealthy instead of silently serving a UI whose every tool call fails.

**`SSE` requires `proxy_buffering off`** at any reverse proxy, or the chat
appears frozen until the turn ends.

**Publishing 8090 on `0.0.0.0` bypasses any reverse-proxy authentication.**
The app is reachable directly on the LAN. If the deployment gates access at the
proxy, either bind the published port to loopback or drop `ports:` and route to
the container by service name.

**Corpus tools are withheld from the agent.** `pdf_search` takes `path`,
`pdf_corpus_search` takes `paths`; small models reliably confuse the two, so
`CORPUS_TOOLS` and `HOUSEKEEPING_TOOLS` are filtered out of the tool list.

**Answer length**: a 2,000-token cap truncated answers mid-sentence with
`finish_reason: length`. Raised to 32,768 — but short answers were caused by the
prompt saying "answer concisely", not by the cap. Check the prompt first.

---

## 11. Measured performance

On one machine, local models over llama.cpp with `--parallel 2`:

| operation | cost |
|---|---|
| pdf-mcp page embeddings (CPU, bge-small) | ~0.5 s/page — 240 s for 453 pages |
| chunked bge-m3 index (GPU) | ~7 s for a 115-page document |
| figure description | ~2.3–3 s/page; 6 pages in 7.1 s across 2 slots |
| printed-TOC parse | ~26 s |
| map generation | ~3.0 s/chapter; 14 chapters ≈ 21 s across 2 slots |
| semantic search, one document (326 vectors) | ~28–42 ms |
| semantic search, whole corpus (2,168 vectors) | ~76 ms |
| **full answer, end to end** | **4.6–8.4 s** |

Retrieval is **1–2% of the time a user waits**; the local model generating is
the rest. Optimising the store — a faster database, an ANN index — cannot
improve perceived speed at this scale. The linear scan is fine to roughly
50,000 vectors on this trajectory; the first move if that changes is
vectorising in-process, not adding a database service.

---

## 12. Known limits

- The Claude provider has **never executed** here; no `ANTHROPIC_API_KEY` present.
- STT is wired but the browser path is untested end to end. TTS is verified.
- bbox coverage for semantic chunks is **~64%**; the rest fall back to
  whole-page highlight, because extraction text differs from layout text.
- Figure descriptions are partial on dense diagrams (see §5).
- Retrieval reaches **70% R@10** on paraphrased queries — roughly 3 in 10 hard
  questions never get the right page into context.
- Enumeration recall is imperfect: asking for every rule in a section returned
  5 of 6.
- Everything is **per document**. Cross-document semantic search is one
  `WHERE path=?` filter away (measured at 76 ms over the whole corpus) but is
  not exposed as a tool.
- `.txt` / `.md` are not ingestible; pdf-mcp is PDF-only.
