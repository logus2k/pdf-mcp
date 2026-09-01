"""
pdf-mcp Explorer — a thin web UI over pdf-mcp's stdio transport.

One long-lived pdf-mcp subprocess is spawned at startup and kept warm for the
life of the server: its SQLite cache and the lazily-loaded fastembed model are
process state, so respawning per request would throw both away.

The single stdio pipe is multiplexed by JSON-RPC id — every request parks on a
Future that the reader task resolves — so concurrent browser calls do not need
a lock and do not head-of-line block each other.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import agent
import providers
import figures
import semantic
import summarizer
import voice

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent
# The pdf-mcp executable. On the host it lives in the project's own venv; in
# the container pdf-mcp is installed system-wide, so the path differs.
SERVER_BIN = Path(os.environ.get(
    "EXPLORER_PDF_MCP_BIN", str(PROJECT / ".venv_pdf-mcp" / "bin" / "pdf-mcp")))

# pdf_read_all and page renders return payloads far beyond asyncio's 64 KiB
# default line limit; renders in particular are multi-MB base64.
STREAM_LIMIT = 64 * 1024 * 1024
PROTOCOL_VERSION = "2025-06-18"


class MCPError(RuntimeError):
    pass


class StdioMCPClient:
    """Minimal JSON-RPC client for an MCP server speaking newline-delimited JSON."""

    def __init__(self, command: list[str]) -> None:
        self.command = command
        self.proc: asyncio.subprocess.Process | None = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._write_lock = asyncio.Lock()
        self.server_info: dict[str, Any] = {}
        self.stderr_tail: list[str] = []
        # Fan-out queues for the SSE activity feed.
        self.subscribers: set[asyncio.Queue[str]] = set()

    async def start(self) -> None:
        self.proc = await asyncio.create_subprocess_exec(
            *self.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=STREAM_LIMIT,
        )
        self._reader_task = asyncio.create_task(self._read_loop())
        self._stderr_task = asyncio.create_task(self._drain_stderr())

        result = await self.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "pdf-mcp-explorer", "version": "1.0"},
            },
        )
        self.server_info = result
        await self.notify("notifications/initialized", {})

    async def stop(self) -> None:
        for task in (self._reader_task, self._stderr_task):
            if task:
                task.cancel()
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                self.proc.kill()

    async def _drain_stderr(self) -> None:
        """FastMCP prints a startup banner to stderr; left unread it can block."""
        assert self.proc and self.proc.stderr
        while True:
            line = await self.proc.stderr.readline()
            if not line:
                return
            text = line.decode(errors="replace").rstrip()
            if text:
                self.stderr_tail.append(text)
                del self.stderr_tail[:-200]

    async def _read_loop(self) -> None:
        assert self.proc and self.proc.stdout
        while True:
            line = await self.proc.stdout.readline()
            if not line:
                for fut in self._pending.values():
                    if not fut.done():
                        fut.set_exception(MCPError("pdf-mcp closed its stdout"))
                self._pending.clear()
                return
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg_id = message.get("id")
            if msg_id is None:
                self._publish("notification", message)
                continue
            fut = self._pending.pop(msg_id, None)
            if fut and not fut.done():
                fut.set_result(message)

    def _publish(self, kind: str, payload: Any) -> None:
        event = json.dumps({"kind": kind, "at": time.time(), "payload": payload})
        for queue in list(self.subscribers):
            queue.put_nowait(event)

    async def _send(self, message: dict[str, Any]) -> None:
        assert self.proc and self.proc.stdin
        data = (json.dumps(message) + "\n").encode()
        async with self._write_lock:
            self.proc.stdin.write(data)
            await self.proc.stdin.drain()

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._next_id += 1
        msg_id = self._next_id
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = fut
        await self._send(
            {"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params}
        )
        message = await fut
        if "error" in message:
            raise MCPError(json.dumps(message["error"]))
        return message.get("result", {})

    async def list_tools(self) -> list[dict[str, Any]]:
        return (await self.request("tools/list", {}))["tools"]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        started = time.perf_counter()
        self._publish("call", {"tool": name, "arguments": arguments})
        try:
            result = await self.request(
                "tools/call", {"name": name, "arguments": arguments}
            )
        except MCPError as exc:
            self._publish("error", {"tool": name, "message": str(exc)})
            raise
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        self._publish("done", {"tool": name, "elapsed_ms": elapsed_ms})
        result["_elapsed_ms"] = elapsed_ms
        return result


client = StdioMCPClient([str(SERVER_BIN)])


def _scan_roots(roots: list[str]) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    for root in roots:
        root_path = Path(root)
        if not root_path.is_dir():
            continue
        for entry in sorted(root_path.rglob("*.pdf")):
            if entry.is_file():
                stat = entry.stat()
                files.append(
                    {
                        "path": str(entry),
                        "name": entry.name,
                        "root": root,
                        "size_bytes": stat.st_size,
                        "modified": stat.st_mtime,
                    }
                )
    return files


async def _watch_documents(roots: list[str], interval: float = 2.0) -> None:
    """Poll the allowed roots and push a change event, so the page never needs
    a manual refresh when a PDF is dropped into (or removed from) data/."""
    previous: list[tuple[str, int, float]] | None = None
    while True:
        try:
            current = [
                (f["path"], f["size_bytes"], f["modified"]) for f in _scan_roots(roots)
            ]
            if previous is not None and current != previous:
                client._publish("documents", {"count": len(current)})
                # Warm anything new. Without this a PDF copied into the folder
                # stays cold until someone asks about it, and pays for the
                # indexing mid-question.
                seen = {p for p, _, _ in previous}
                for path, _, _ in current:
                    if path not in seen:
                        enqueue_warm(path, WARM_DISCOVERED)
            previous = current
        except Exception as exc:  # a transient stat error must not kill the task
            client._publish("error", {"tool": "watch", "message": str(exc)})
        await asyncio.sleep(interval)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not SERVER_BIN.exists():
        raise RuntimeError(f"pdf-mcp binary not found at {SERVER_BIN}")
    await client.start()

    info = _first_json_block(await client.call_tool("server_info", {}))
    roots = info.get("documents", {}).get("roots", []) if isinstance(info, dict) else []
    # Settled before the warm worker starts, so the first document indexed
    # already knows whether its figures can be described.
    await _resolve_figure_indexing()

    watcher = asyncio.create_task(_watch_documents(roots))
    warmer = asyncio.create_task(_warm_worker())
    # Index whatever is already in the folder. Cheap when the cache is warm
    # (~11ms per document), and it is what makes a cleared cache recover on its
    # own instead of ambushing the next question.
    for existing in _scan_roots(roots):
        enqueue_warm(existing["path"], WARM_STARTUP)
    try:
        yield
    finally:
        watcher.cancel()
        warmer.cancel()
        await client.stop()


app = FastAPI(title="pdf-mcp Explorer", lifespan=lifespan)


class CallRequest(BaseModel):
    name: str
    arguments: dict[str, Any] = {}


class ChatRequest(BaseModel):
    messages: list[dict[str, Any]]
    provider: str = "local"
    document: str | None = None
    all_tools: bool = False
    use_map: bool = True


@app.get("/api/providers")
async def api_providers() -> dict[str, Any]:
    # Discovered live: the local router swaps models, so this must not be cached.
    return {"providers": await providers.available_providers()}


@app.post("/api/chat")
async def api_chat(body: ChatRequest) -> StreamingResponse:
    """Run the agent loop, streaming its events to the browser as SSE.

    POST-then-stream (rather than EventSource) because the request carries the
    whole conversation; EventSource can only issue GETs.
    """
    tools = await client.list_tools()
    if body.document and semantic.is_indexed(body.document):
        tools = tools + [semantic.TOOL_SCHEMA]

    async def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        # pdf_semantic_search is served here, not by pdf-mcp: it queries the
        # chunked bge-m3 index, which covers whole pages instead of the first
        # 512 tokens. Shaped like a pdf_search result so citations, page jumps
        # and bbox highlighting work unchanged.
        if name == "pdf_semantic_search":
            hits = await semantic.search(
                arguments.get("path") or body.document or "",
                arguments.get("query", ""),
                int(arguments.get("max_results") or 8))
            # Figure hits are labelled: their text is a description generated
            # from the page image, not words printed on the page. Without the
            # label the model would quote it as if it were the document's own
            # prose.
            payload = {"matches": [
                {"page": h["page"], "excerpt": h["excerpt"][:1200],
                 "score": h["score"], "bbox": h["bbox"],
                 "kind": h.get("kind", "text"),
                 **({"note": "description of a figure on this page, "
                             "generated from the page image"}
                    if h.get("kind") == "figure" else {})}
                for h in hits], "total_matches": len(hits),
                "search_mode": "semantic-chunked-bge-m3"}
            return {"content": [{"type": "text", "text": json.dumps(payload)}]}
        return await client.call_tool(name, arguments)

    # `use_map=False` lets a caller A/B the map's effect on the same question.
    document_map = None
    if body.document and body.use_map:
        stored = summarizer.load_map(body.document)
        if stored:
            document_map = summarizer.render_for_prompt(stored)

    async def stream():
        try:
            async for event in agent.run_agent(
                body.messages,
                tools,
                call_tool,
                provider=body.provider,
                all_tools=body.all_tools,
                document=body.document,
                document_map=document_map,
            ):
                yield f"data: {json.dumps(event)}\n\n"
        except Exception as exc:  # never leave the browser hanging on a stream
            yield f'data: {json.dumps({"type": "error", "message": str(exc)})}\n\n'
        yield 'data: {"type": "end"}\n\n'

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/chat")
async def chat_page() -> FileResponse:
    return FileResponse(HERE / "static" / "chat.html")


@app.get("/tools")
async def tools_page() -> FileResponse:
    return FileResponse(HERE / "static" / "index.html")


# ---------------------------------------------------------------------------
# Warming: the first search on a fresh document extracts text and computes
# embeddings for every page. On a 453-page book that measured ~44s, during
# which the UI looked ready but every question hung. Warming is therefore
# kicked off as soon as a document is uploaded, and its progress is streamed
# so the user can see when it will be answerable.
# ---------------------------------------------------------------------------

warm_state: dict[str, Any] = {}

# Warming is queued and served by one worker. Every document must be indexed
# before it can be searched at full speed, and that cost used to land on the
# reader's first question: 56s on a 115-page document, with no progress shown,
# because only *uploads* were warmed. Documents copied into the folder, and
# everything present at startup (or after a cache wipe), were never touched.
#
# Priorities: an upload is what someone is waiting on, so it goes first; a
# newly-discovered file next; the startup sweep last. Re-warming an already
# indexed document costs ~11ms, so the sweep is cheap when the cache is warm.
WARM_UPLOAD, WARM_DISCOVERED, WARM_STARTUP = 0, 1, 2
warm_queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
_warm_seq = 0

# The in-flight warm for each path, so it can be cancelled. Without a handle on
# the task the only way to stop an import was to restart the container, which
# also killed every other document's queued work.
warm_tasks: dict[str, asyncio.Task] = {}


def enqueue_warm(path: str, priority: int = WARM_DISCOVERED) -> None:
    global _warm_seq
    existing = warm_state.get(path, {})
    if existing.get("state") in ("running", "done"):
        return
    _warm_seq += 1
    warm_queue.put_nowait((priority, _warm_seq, path))


async def _warm_worker() -> None:
    """Serialise warming: two documents embedding at once would just contend."""
    while True:
        _, _, path = await warm_queue.get()
        try:
            if warm_state.get(path, {}).get("state") == "done":
                continue
            if not Path(path).is_file():
                continue
            info = _first_json_block(await client.call_tool("pdf_info", {"path": path}))
            if not isinstance(info, dict) or info.get("error"):
                continue
            task = asyncio.create_task(_warm_document(path, int(info["page_count"])))
            warm_tasks[path] = task
            try:
                await task
            except asyncio.CancelledError:
                # Cancelling the child must not take the worker down with it,
                # or one cancelled import would stall every document behind it
                # in the queue.
                pass
            finally:
                warm_tasks.pop(path, None)
        except Exception as exc:
            client._publish("warm", {"path": path, "name": Path(path).name,
                                     "state": "failed", "message": str(exc)})
        finally:
            warm_queue.task_done()

# Core-map generation is conditional by default. Measured over 144 A/B turns:
# the map HURTS on documents with a usable table of contents (pdf_get_toc is
# lossless and free, so a generated index only competes with it) and HELPS on
# documents without one. "auto" therefore builds a map only when the document
# cannot be navigated from its own TOC.
#   auto (default) | always | never
MAP_MODE = os.environ.get("EXPLORER_BUILD_MAP", "auto").strip().lower()
if MAP_MODE == "1":       # older flag value
    MAP_MODE = "always"
elif MAP_MODE in ("0", ""):
    MAP_MODE = "auto"
MAP_PROVIDER = os.environ.get("EXPLORER_MAP_PROVIDER", "local")

# Figure indexing: "auto" describes figures only if the model server really is
# multimodal, "on" forces it, "off" disables it. Auto probes rather than trusts
# the model name — a text-only server answers an image request with HTTP 200
# and confident prose about nothing, so assuming the capability would quietly
# fill the index with hallucinated descriptions.
FIGURE_MODE = os.environ.get("EXPLORER_INDEX_FIGURES", "auto").lower()
FIGURE_INDEXING = FIGURE_MODE == "on"


async def _resolve_figure_indexing() -> None:
    """Settle the auto case once, at startup, and say so in the log."""
    global FIGURE_INDEXING
    if FIGURE_MODE == "off":
        FIGURE_INDEXING = False
        print("[figures] disabled by EXPLORER_INDEX_FIGURES=off", flush=True)
        return
    if FIGURE_MODE == "on":
        FIGURE_INDEXING = True
        print("[figures] forced on", flush=True)
        return
    FIGURE_INDEXING = await figures.vision_available()
    print(f"[figures] vision probe: "
          f"{'available — figures will be described' if FIGURE_INDEXING else 'not available — skipping'}",
          flush=True)


async def _should_build_map(path: str) -> tuple[bool, str]:
    """(build?, reason) — reason is surfaced so the choice is never silent."""
    if MAP_MODE == "never":
        return False, "disabled (EXPLORER_BUILD_MAP=never)"
    # load_map validates mtime+size, so this only matches a map built for the
    # file as it is now. Without it every restart regenerated the map from
    # scratch — one LLM call per chapter, ~60s for a 14-chapter document —
    # to produce the map already sitting on disk.
    if summarizer.load_map(path):
        return False, "map already built for this file"
    info = _first_json_block(await client.call_tool("pdf_info", {"path": path}))
    if not isinstance(info, dict) or info.get("error"):
        return False, f"pdf_info failed: {(info or {}).get('error')}"
    toc = info.get("toc") or []
    entries = int(info.get("toc_entry_count") or 0)
    pages = int(info.get("page_count") or 0)
    if MAP_MODE == "always":
        return True, "forced (EXPLORER_BUILD_MAP=always)"
    # pdf_info inlines `toc` only when the entry count is small; a large TOC is
    # itself proof the document is navigable, so no second call is needed.
    if entries > 50:
        return False, f"TOC is navigable ({entries} entries) — pdf_get_toc suffices"
    if summarizer.toc_is_navigable(toc, pages):
        return False, f"TOC is navigable ({entries} entries) — pdf_get_toc suffices"
    return True, f"TOC unusable ({entries} entries) — generating an index"


async def _cache_counters() -> tuple[int, int]:
    """(pages with text, pages with embeddings) across the whole cache.

    pdf_cache_stats has no per-file breakdown, so progress is measured as a
    delta against a baseline taken before warming starts. Single-user by
    design; warming two documents at once would blur the two.
    """
    stats = _first_json_block(await client.call_tool("pdf_cache_stats", {}))
    if not isinstance(stats, dict):
        return (0, 0)
    return (int(stats.get("total_pages") or 0), int(stats.get("embedding_pages") or 0))


async def _warm_document(path: str, total_pages: int) -> None:
    """Extract and embed every page, reporting genuine progress.

    Text extraction is driven here in page chunks rather than delegated to
    pdf_corpus_warm, because that call commits the whole document at once: it
    produced a bar that sat at 0 for 29s and then jumped to 100%, which is
    worse than no bar. Chunked pdf_read_pages commits each batch (verified:
    50 -> 100 -> 150 rows), so the count shown is real work completed.

    Embeddings are still computed by pdf_corpus_warm, which does commit them
    in bulk; that phase is reported by elapsed time rather than a fake count.
    """
    CHUNK = 40
    started = time.monotonic()
    state = {
        "path": path, "name": Path(path).name, "total": total_pages,
        "text_done": 0, "emb_done": 0, "phase": "extracting",
        "elapsed": 0.0, "eta": None, "state": "running",
    }
    warm_state[path] = state

    def publish() -> None:
        state["elapsed"] = round(time.monotonic() - started, 1)
        client._publish("warm", dict(state))

    publish()
    try:
        for first in range(1, total_pages + 1, CHUNK):
            last = min(first + CHUNK - 1, total_pages)
            await client.call_tool(
                "pdf_read_pages", {"path": path, "pages": f"{first}-{last}"})
            state["text_done"] = last
            elapsed = time.monotonic() - started
            rate = last / elapsed if elapsed > 0 else 0
            if rate > 0:
                # Embedding costs roughly a third of extraction on measured runs;
                # good enough for an ETA, and it is labelled approximate.
                state["eta"] = round(((total_pages - last) / rate) + (total_pages / rate) * 0.33, 1)
            publish()

        # Chunked semantic index (bge-m3). This is the semantic half that
        # actually covers the whole page; pdf-mcp's own embeddings truncate at
        # 512 tokens and are left to serve only its internal hybrid scoring.
        state["phase"] = "semantic"
        state["eta"] = None
        publish()
        # Keyed on mtime+size, so a replaced file still re-indexes. Without this
        # every restart re-chunked and re-embedded every page of every document
        # — minutes of GPU work per large manual to rebuild rows that were
        # already correct. Only the figure phase was guarded.
        try:
            if semantic.is_indexed(path):
                # Already chunked and embedded for this exact file. Re-doing it
                # dropped every row and re-embedded every page on each restart —
                # minutes of GPU work per large manual to rebuild rows that were
                # already correct. Only the figure phase had a guard.
                state["chunks"] = semantic.chunk_count(path)
                state["chunks_done"] = state["chunks_total"] = state["chunks"]
                state["semantic_skipped"] = True
                publish()
            else:
                pages_payload = _first_json_block(await client.call_tool(
                    "pdf_read_pages", {"path": path, "pages": f"1-{total_pages}"}))
                page_list = (pages_payload or {}).get("pages", [])

                def _chunk_progress(done: int, total: int) -> None:
                    state["chunks_done"], state["chunks_total"] = done, total
                    publish()

                result = await semantic.index_document(
                    path, page_list, _chunk_progress)
                state["chunks"] = result.get("chunks")
                state["chunks_with_bbox"] = result.get("with_bbox")
        except Exception as exc:
            state["semantic_error"] = str(exc)
        publish()

        # Figure descriptions. The local model is multimodal, so a page's
        # diagrams are described and indexed as text; without this a figure is
        # invisible to every search the app can run. Only pages whose images
        # are not repeated furniture and cover enough of the page get a call —
        # 6 of 115 on the INCOSE guide, against 164 extracted images.
        # The text pass clears only text chunks, so descriptions from an
        # earlier run survive and this check can see them. Keyed on mtime+size,
        # so a replaced file is re-described and an unchanged one is not:
        # without it every rebuild paid for 57 vision calls on the 472-page
        # manual to regenerate descriptions that had not changed.
        if FIGURE_INDEXING and not semantic.figures_indexed(path):
            state["phase"] = "figures"
            state["eta"] = None
            publish()
            try:
                def _figure_progress(done: int, total: int) -> None:
                    state["figures_done"], state["figures_total"] = done, total
                    publish()

                described = await figures.describe_document(
                    path, provider=MAP_PROVIDER, on_progress=_figure_progress)
                stored = await semantic.index_figures(path, described["records"])
                state["figures"] = stored
                state["figure_pages"] = described["pages"]
            except Exception as exc:
                # A document is still fully usable without figure descriptions;
                # losing the whole warm over them would be a bad trade.
                state["figure_error"] = str(exc)
            publish()

        state["phase"] = "embedding"
        state["eta"] = None
        publish()

        # Embeddings are committed in bulk by pdf_corpus_warm, so there is no
        # count to report and nothing publishes between its calls — the bar sat
        # frozen through the longest phase. A ticker keeps elapsed moving so the
        # UI shows work in progress rather than a stall.
        async def _tick() -> None:
            while True:
                await asyncio.sleep(2)
                publish()

        ticker = asyncio.create_task(_tick())
        try:
            for _ in range(30):
                result = _first_json_block(await client.call_tool(
                    "pdf_corpus_warm",
                    {"paths": [path], "embeddings": True, "budget_seconds": 55},
                ))
                _, emb_now = await _cache_counters()
                state["emb_done"] = min(total_pages, emb_now)
                publish()
                if not isinstance(result, dict) or not result.get("unprocessed"):
                    break
        finally:
            ticker.cancel()
    except Exception as exc:
        state["state"] = "failed"
        state["message"] = str(exc)
        publish()
        return

    build_map, why = await _should_build_map(path)
    state["map_decision"] = why
    publish()
    if build_map:
        state["phase"] = "mapping"
        state["eta"] = None
        publish()

        def on_progress(done: int, total: int, label: str) -> None:
            state["map_done"], state["map_total"], state["map_label"] = done, total, label
            publish()

        try:
            data = await summarizer.build_core_map(
                path, total_pages, client.call_tool, _first_json_block,
                provider=MAP_PROVIDER, on_progress=on_progress)
            summarizer.save_map(data)
            state["map_chapters"] = len(data.get("chapters", []))
        except Exception as exc:
            # A missing map degrades navigation; it must not fail the warm.
            state["map_error"] = str(exc)

    state["state"] = "done"
    state["phase"] = "ready"
    state["text_done"] = state["emb_done"] = total_pages
    state["eta"] = 0
    publish()


MAX_UPLOAD_BYTES = int(os.environ.get("EXPLORER_MAX_UPLOAD_MB", "200")) * 1024 * 1024


@app.post("/api/upload")
async def api_upload(file: UploadFile = File(...)) -> dict[str, Any]:
    """Accept a PDF and drop it into the first allow-listed root.

    The file arrives as multipart/form-data in the request body. It must land
    under a root pdf-mcp already admits, otherwise the server would refuse to
    read what we just stored.
    """
    info = _first_json_block(await client.call_tool("server_info", {}))
    roots = info.get("documents", {}).get("roots", []) if isinstance(info, dict) else []
    target_root = next((Path(r) for r in roots if Path(r).is_dir()), None)
    if target_root is None:
        raise HTTPException(status_code=500, detail="No writable allow-listed root is configured")

    # Take only the basename: a crafted filename must not escape the root.
    name = Path(file.filename or "upload.pdf").name
    if not name.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only .pdf files are accepted")

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="The uploaded file is empty")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File is {len(data) // (1024 * 1024)} MB; the limit is "
                   f"{MAX_UPLOAD_BYTES // (1024 * 1024)} MB",
        )
    if not data.startswith(b"%PDF-"):
        raise HTTPException(status_code=400, detail="That file is not a PDF (missing %PDF- header)")

    destination = target_root / name
    stem, suffix = destination.stem, destination.suffix
    counter = 2
    while destination.exists():
        destination = target_root / f"{stem} ({counter}){suffix}"
        counter += 1

    destination.write_bytes(data)

    # Confirm pdf-mcp can actually open what we stored, so the caller learns
    # about a corrupt file here rather than on their first question.
    probe = _first_json_block(await client.call_tool("pdf_info", {"path": str(destination)}))
    pages = probe.get("page_count") if isinstance(probe, dict) else None
    error = probe.get("error") if isinstance(probe, dict) else None

    client._publish("documents", {"count": len(_scan_roots(roots))})
    if pages and not error:
        # Highest priority: this is the document someone is waiting to use, and
        # the status-bar progress bar tracks it through extraction, embeddings
        # and (where applicable) map generation before reporting ready.
        enqueue_warm(str(destination), WARM_UPLOAD)
    return {
        "path": str(destination),
        "name": destination.name,
        "size_bytes": len(data),
        "page_count": pages,
        "error": error,
    }


class WarmRequest(BaseModel):
    path: str


@app.post("/api/warm")
async def api_warm(body: WarmRequest) -> dict[str, Any]:
    """Warm a document that was not uploaded through this UI."""
    existing = warm_state.get(body.path)
    if existing and existing.get("state") == "running":
        return {"status": "already running", "progress": existing}
    info = _first_json_block(await client.call_tool("pdf_info", {"path": body.path}))
    if not isinstance(info, dict) or info.get("error"):
        raise HTTPException(status_code=400,
                            detail=(info or {}).get("error", "cannot read that PDF"))
    task = asyncio.create_task(_warm_document(body.path, int(info["page_count"])))
    warm_tasks[body.path] = task
    task.add_done_callback(lambda _t, p=body.path: warm_tasks.pop(p, None))
    return {"status": "started", "pages": info["page_count"]}


async def purge_document(path: str) -> dict[str, Any]:
    """Remove everything this document has written to the indexes.

    Used by cancel, where a half-finished import leaves rows in three separate
    stores. Each is cleared independently: a failure in one must not strand the
    others, since the point of cancelling is to be able to start clean.
    """
    result: dict[str, Any] = {}
    try:
        result["semantic_rows"] = semantic.drop(path)
    except Exception as exc:
        result["semantic_error"] = str(exc)
    try:
        map_file = summarizer.map_path_for(path)
        result["map_removed"] = map_file.exists()
        map_file.unlink(missing_ok=True)
    except Exception as exc:
        result["map_error"] = str(exc)
    try:
        result["cache"] = _purge_mcp_cache(path)
    except Exception as exc:
        result["cache_error"] = str(exc)
    return result


# pdf-mcp's own cache. Reached directly because its pdf_cache_clear tool takes
# only `expired_only` — it is all-or-nothing, and clearing everything to
# abandon one import would throw away every other document's index. Tables are
# discovered at run time rather than hardcoded, so a pdf-mcp schema change
# degrades to "purged less" instead of raising.
MCP_CACHE_DB = Path(os.environ.get(
    "EXPLORER_MCP_CACHE_DB", str(Path.home() / ".cache" / "pdf-mcp" / "cache.db")))


def _purge_mcp_cache(path: str) -> dict[str, Any]:
    if not MCP_CACHE_DB.exists():
        return {"skipped": "no cache db"}
    conn = sqlite3.connect(MCP_CACHE_DB, timeout=15)
    removed: dict[str, int] = {}
    try:
        # Extracted images are files on disk; the row only points at them, so
        # deleting rows alone would leak the PNGs.
        try:
            for (on_disk,) in conn.execute(
                    "SELECT file_path_on_disk FROM page_images WHERE file_path=?",
                    (path,)):
                if on_disk:
                    Path(on_disk).unlink(missing_ok=True)
        except sqlite3.Error:
            pass

        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view')")]
        for table in tables:
            # FTS shadow tables are maintained by their virtual table; writing
            # to them directly corrupts the index.
            if table.endswith(("_data", "_idx", "_content", "_docsize", "_config")):
                continue
            try:
                columns = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            except sqlite3.Error:
                continue
            if "file_path" not in columns:
                continue
            try:
                n = conn.execute(
                    f"DELETE FROM {table} WHERE file_path=?", (path,)).rowcount
                if n:
                    removed[table] = n
            except sqlite3.Error:
                continue
        conn.commit()
    finally:
        conn.close()
    return removed


@app.post("/api/warm/cancel")
async def api_warm_cancel(body: WarmRequest) -> dict[str, Any]:
    """Stop an in-flight import and clear what it already wrote.

    The document file itself is left in place: cancelling is about abandoning a
    partial index, not discarding the upload, so the import can simply be run
    again without re-uploading.
    """
    task = warm_tasks.get(body.path)
    was_running = bool(task and not task.done())
    if task:
        task.cancel()
        # Give the task a moment to unwind before clearing the stores, so a
        # write already in flight cannot land after the purge.
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=10)
        except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
            pass
        warm_tasks.pop(body.path, None)

    purged = await purge_document(body.path)
    warm_state.pop(body.path, None)
    client._publish("warm", {"path": body.path, "name": Path(body.path).name,
                             "state": "cancelled"})
    return {"status": "cancelled", "was_running": was_running, "purged": purged}


@app.post("/api/document/delete")
async def api_document_delete(body: WarmRequest) -> dict[str, Any]:
    """Delete a document and everything indexed from it.

    Unlike cancel, this removes the PDF itself. The path is re-validated
    against pdf-mcp's allow-listed roots before anything is touched: this
    endpoint unlinks a file, so a crafted path must not be able to reach
    outside the document folder.
    """
    info = _first_json_block(await client.call_tool("server_info", {}))
    roots = info.get("documents", {}).get("roots", []) if isinstance(info, dict) else []
    resolved = Path(body.path).resolve()
    if not roots or not any(_is_within(resolved, Path(r).resolve()) for r in roots):
        raise HTTPException(status_code=403, detail="Path is outside the allowed roots")
    if resolved.suffix.lower() != ".pdf":
        raise HTTPException(status_code=400, detail="Not a PDF")
    if not resolved.is_file():
        raise HTTPException(status_code=404, detail="No such document")

    # Stop any import first, or the warm task would keep writing rows for a
    # file that is about to disappear — and then fail on the missing file.
    task = warm_tasks.get(body.path)
    if task and not task.done():
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=10)
        except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
            pass
    warm_tasks.pop(body.path, None)

    purged = await purge_document(body.path)
    warm_state.pop(body.path, None)
    try:
        resolved.unlink()
        removed = True
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Could not delete the file: {exc}")

    client._publish("warm", {"path": body.path, "name": Path(body.path).name,
                             "state": "cancelled"})
    client._publish("documents", {"count": len(_scan_roots(roots))})
    return {"status": "deleted", "file_removed": removed, "purged": purged}


@app.get("/api/warm")
async def api_warm_status() -> dict[str, Any]:
    return {"warming": list(warm_state.values())}


@app.get("/api/outline")
async def api_outline(path: str) -> dict[str, Any]:
    """Contents for the viewer panel: the PDF's own outline, or the printed one.

    Many documents carry a full contents listing on their front-matter pages
    and no embedded outline at all, so pdf_get_toc reports nothing and the
    panel read "No outline in this PDF." while the listing sat on page 5. The
    printed entries are recovered during warming and stored with the map.
    """
    result = await client.call_tool("pdf_get_toc", {"path": path})
    payload = _first_json_block(result)
    entries = (payload or {}).get("toc") or [] if isinstance(payload, dict) else []
    if entries:
        return {"source": "outline", "entries": entries}

    stored = summarizer.load_map(path)
    printed = (stored or {}).get("printed_toc") or []
    if printed:
        return {"source": "printed", "entries": printed}
    return {"source": "none", "entries": []}


@app.get("/api/map")
async def api_map(path: str) -> dict[str, Any]:
    data = summarizer.load_map(path)
    if not data:
        build, why = await _should_build_map(path)
        return {"present": False, "mode": MAP_MODE, "would_build": build, "reason": why}
    return {"present": True, "mode": MAP_MODE, "map": data,
            "prompt_block": summarizer.render_for_prompt(data)}


@app.post("/api/map")
async def api_build_map(body: WarmRequest) -> dict[str, Any]:
    """Build a map on demand, for documents warmed before the flag was on."""
    info = _first_json_block(await client.call_tool("pdf_info", {"path": body.path}))
    if not isinstance(info, dict) or info.get("error"):
        raise HTTPException(status_code=400,
                            detail=(info or {}).get("error", "cannot read that PDF"))
    # Built in the background: a 453-page book takes minutes, and a synchronous
    # handler dies with the client's request (a curl timeout cancelled the task
    # and silently produced no map). Progress arrives on the SSE channel.
    async def build() -> None:
        try:
            data = await summarizer.build_core_map(
                body.path, int(info["page_count"]), client.call_tool, _first_json_block,
                provider=MAP_PROVIDER,
                on_progress=lambda done, total, label: client._publish(
                    "map", {"path": body.path, "done": done, "total": total,
                            "label": label, "state": "running"}))
            summarizer.save_map(data)
            client._publish("map", {"path": body.path, "state": "done",
                                    "chapters": len(data["chapters"]),
                                    "topics": len(data["topics"]),
                                    "elapsed": data["elapsed"]})
        except Exception as exc:
            client._publish("map", {"path": body.path, "state": "failed",
                                    "message": str(exc)})

    asyncio.create_task(build())
    return {"status": "started", "pages": info["page_count"]}


class SpeakRequest(BaseModel):
    text: str
    voice: str | None = None
    speed: float | None = None


@app.get("/api/voice")
async def api_voice_status(request: Request) -> dict[str, Any]:
    """Both directions of voice: TTS via this backend, STT straight from the page.

    The STT endpoint depends on how the page was reached. Served directly, the
    browser can hit stt_server on loopback. Served through the reverse proxy the
    page is on https and cannot reach 127.0.0.1:2700 at all (wrong host, and
    mixed content), so it must use the proxy's own /stt/socket.io route on the
    page origin — the same path cv uses. Detected from forwarding headers so
    neither deployment needs configuring.
    """
    tts = await voice.health()
    proxied = bool(request.headers.get("x-forwarded-host")
                   or request.headers.get("x-forwarded-proto"))
    if proxied:
        stt = {"url": "", "path": "/stt/socket.io", "mode": "proxy"}
    else:
        stt = {"url": os.environ.get("EXPLORER_STT_URL", "http://127.0.0.1:2700"),
               "path": "/socket.io", "mode": "direct"}
    return {"tts": tts, "stt": stt}


@app.post("/api/speak")
async def api_speak(body: SpeakRequest) -> dict[str, Any]:
    result = await voice.synthesize(body.text, body.voice, body.speed)
    if result.get("error") and not result["chunks"]:
        raise HTTPException(status_code=502, detail=result["error"])
    return result


@app.get("/api/semantic")
async def api_semantic() -> dict[str, Any]:
    return semantic.stats()


@app.get("/api/document")
async def api_document(path: str) -> FileResponse:
    """Serve a PDF to pdf.js — but only one the MCP server would itself admit.

    The allow list is the authority; this endpoint must not become a way to
    read files pdf-mcp would refuse.
    """
    info = _first_json_block(await client.call_tool("server_info", {}))
    roots = info.get("documents", {}).get("roots", []) if isinstance(info, dict) else []
    resolved = Path(path).resolve()
    if not any(_is_within(resolved, Path(r).resolve()) for r in roots):
        raise HTTPException(status_code=403, detail="Path is outside the allowed roots")
    if not resolved.is_file() or resolved.suffix.lower() != ".pdf":
        raise HTTPException(status_code=404, detail="Not a readable PDF")
    return FileResponse(resolved, media_type="application/pdf")


def _is_within(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


@app.get("/api/tools")
async def api_tools(document: str | None = None) -> dict[str, Any]:
    """The tool list, plus pdf_semantic_search when the document has an index.

    `document` is a query parameter: this is a GET, and the handler used to read
    it off a `body` that no route ever supplies, so every call raised NameError
    and returned 500 — which is why the /tools page listed nothing.
    """
    tools = await client.list_tools()
    if document and semantic.is_indexed(document):
        tools = tools + [semantic.TOOL_SCHEMA]
    return {
        "tools": tools,
        "server": client.server_info.get("serverInfo", {}),
        "instructions": client.server_info.get("instructions", ""),
    }


@app.get("/api/documents")
async def api_documents() -> dict[str, Any]:
    """Roots the server admits, and the PDFs currently sitting under them."""
    result = await client.call_tool("server_info", {})
    info = _first_json_block(result)
    documents = info.get("documents", {}) if isinstance(info, dict) else {}
    files = _scan_roots(documents.get("roots", []))
    return {"documents": documents, "files": files, "features": info.get("features", {})}


@app.post("/api/call")
async def api_call(body: CallRequest) -> dict[str, Any]:
    try:
        result = await client.call_tool(body.name, body.arguments)
    except MCPError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return result


@app.get("/api/health")
async def api_health() -> dict[str, Any]:
    proc = client.proc
    return {
        "status": "ok" if proc and proc.returncode is None else "down",
        "pid": proc.pid if proc else None,
        "server": client.server_info.get("serverInfo", {}),
        "stderr_tail": client.stderr_tail[-5:],
    }


@app.get("/api/events")
async def api_events() -> StreamingResponse:
    queue: asyncio.Queue[str] = asyncio.Queue()
    client.subscribers.add(queue)

    async def stream():
        try:
            yield 'data: {"kind":"hello"}\n\n'
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield f"data: {event}\n\n"
        finally:
            client.subscribers.discard(queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _first_json_block(result: dict[str, Any]) -> Any:
    """Tool results carry content blocks; the text ones hold the JSON payload."""
    for block in result.get("content", []):
        if block.get("type") == "text":
            try:
                return json.loads(block["text"])
            except json.JSONDecodeError:
                return block["text"]
    return {}


@app.get("/")
async def index() -> FileResponse:
    """Chat is the front door — reading a document is the point of this app.
    The per-tool explorer lives at /tools."""
    return FileResponse(HERE / "static" / "chat.html")


app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("EXPLORER_HOST", "127.0.0.1"),
        port=int(os.environ.get("EXPLORER_PORT", "8090")),
    )
