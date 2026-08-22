# pdf-mcp Explorer

Two pages over the **stdio** transport, FastAPI + uvicorn on the back:

* **`/chat`** — chat over a PDF, with the document open beside the conversation.
* **`/`** — a form-per-tool explorer for driving all 13 tools by hand.

## Chat (`/chat`)

Three resizable panes — contents | PDF | chat — mirroring docbro's layout. The
model answers only from the document: it calls pdf-mcp's tools, and every call
is shown as a chip with its arguments and result size rather than hidden behind
a spinner. Page citations under each answer jump the viewer to that page and
highlight the matching region, using the `bbox` that `pdf_search` returns.

**Adding documents.** Two routes, both landing in the same place:

* **↑ Upload** beside the document dropdown — `POST /api/upload`, the file sent
  as `multipart/form-data` in the request body. The server takes only the
  basename (so a crafted `../../` filename cannot escape), requires a `.pdf`
  extension *and* a real `%PDF-` header, caps size at `EXPLORER_MAX_UPLOAD_MB`
  (default 200), de-duplicates names as `name (2).pdf`, then calls `pdf_info`
  to confirm pdf-mcp can actually open it before reporting success.
* **Dropping a file into the folder** by any other means — the folder watcher
  notices within ~2s and pushes a `documents` event over SSE; both pages
  refresh their picker in place, no reload.

There is no separate ingest or indexing step. pdf-mcp caches lazily, keyed on
path+mtime, so a new file is queryable the moment it exists.

**Providers.** Pick per turn in the top bar:

| Provider | Endpoint | Requires |
|---|---|---|
| `local` (default) | `http://127.0.0.1:8500/v1` | llama.cpp running |
| `claude` | `https://api.anthropic.com` | `ANTHROPIC_API_KEY` |

Override with `EXPLORER_LLM_MODEL`, `EXPLORER_CLAUDE_MODEL`,
`EXPLORER_LLM_BASE`. A provider whose prerequisite is missing appears disabled
in the dropdown rather than failing mid-turn.

**Reused from your own codebase**, not reinvented: the pdf.js renderer /
layout manager / controls trio and the `DocumentViewer` wrapper come from
`cv/widget` (originally `docbro/script`); the markdown + KaTeX + highlight.js
layer and the streaming `<think>` parser are ported from `cv-chat.js` (itself
from `noted`). Every library is **vendored** under `static/vendor/` — pdf.js,
Split.js, marked, KaTeX, highlight.js, socket.io. Nothing loads from a CDN.

**Two deliberate reductions**, both stated rather than silent:
* The corpus tools are hidden from the chat model. `pdf_search` and
  `pdf_corpus_search` differ by one character in their required argument
  (`path` vs `paths`) and small models reliably confuse them; removing the
  collision measurably stopped the wasted retries. Pass `all_tools: true` to
  restore them.
* Each tool's `SECURITY:` preamble is stripped before the schema reaches the
  model — identical boilerplate on all 13 tools, stated once in the system
  prompt instead. Oversized tool results are trimmed at a paragraph edge and
  the model is *told* they were trimmed, so it can narrow the next call.

## Tool explorer (`/`)

A form for every tool by hand.

## Run

```bash
cd /home/logus/env/assets/pdf-mcp
./.venv_explorer/bin/python explorer/app.py
# → http://127.0.0.1:8090
```

`EXPLORER_HOST` (default `127.0.0.1`) and `EXPLORER_PORT` (default `8090`)
override the bind. Keep it on loopback: the backend inherits whatever the
pdf-mcp process can read, so exposing it publicly exposes the allow-listed
document roots too.

## How it fits together

```
browser ──HTTP/JSON──> FastAPI ──JSON-RPC over stdio──> pdf-mcp
        <──── SSE ────           (one long-lived subprocess)
```

* **One subprocess, kept warm.** pdf-mcp caches in SQLite keyed on path+mtime
  and loads the fastembed model lazily on first semantic search. Respawning per
  request would throw both away, so the process is started in the FastAPI
  lifespan and reused.
* **Multiplexed by JSON-RPC id.** Every request parks on a `Future` that the
  reader task resolves, so concurrent calls share the one pipe without a lock
  and without head-of-line blocking.
* **stderr is drained.** FastMCP prints a startup banner to stderr; left unread
  it can eventually block the child. The last 200 lines are kept and surfaced
  at `/api/health`.
* **Stream limit raised to 64 MiB.** asyncio's default 64 KiB line limit is far
  below what `pdf_read_all` and page renders return.

## The UI is generated, not written

No tool is hardcoded. `tools/list` returns full JSON Schema for all 13 tools,
and the frontend builds each form from `inputSchema` — types, defaults,
required flags, and descriptions all come from the server. When pdf-mcp changes
its tools, this page follows automatically.

## Why SSE and not socket.io

The traffic is request→response (plain POST) plus server→client push (progress,
activity, document changes). SSE covers the push direction natively via
`EventSource`, which also reconnects on its own. socket.io would add a backend
dependency and a client bundle to buy bidirectional messaging nothing here
needs. It would be the right call if several browsers ever had to coordinate.

**No client-side libraries at all** — no CDN links, no bundles, nothing to
vendor. Everything the browser loads is served from `static/`.

## Live updates without a refresh

A folder watcher polls the allow-listed roots every 2s and pushes a
`documents` event over SSE; the page refreshes its document picker in place.
Drop a PDF into `data/` and it appears in the dropdown without a reload.

## Endpoints

| Endpoint | Purpose |
|---|---|
| `GET /api/tools` | tool list with JSON Schemas |
| `GET /api/documents` | allow-listed roots plus the PDFs under them |
| `POST /api/call` | `{name, arguments}` → tool result |
| `GET /api/events` | SSE: `call`, `done`, `error`, `documents` |
| `GET /api/health` | subprocess status, pid, stderr tail |

## Notes

* Reads stay confined to the `[paths] allow` list in
  `~/.config/pdf-mcp/config.toml` — that is enforced inside pdf-mcp, not here.
* Tool failures come back as a normal result carrying `isError`, not as a
  JSON-RPC error, so the frontend checks that flag explicitly.
