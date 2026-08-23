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
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import agent
import providers
import summarizer

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent
SERVER_BIN = PROJECT / ".venv_pdf-mcp" / "bin" / "pdf-mcp"

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
    watcher = asyncio.create_task(_watch_documents(roots))
    try:
        yield
    finally:
        watcher.cancel()
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

    async def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
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

# Core-map generation is opt-in: it costs one LLM call per chapter, and its
# value (fewer blind searches) is exactly what we are trying to measure.
BUILD_MAP = os.environ.get("EXPLORER_BUILD_MAP", "").strip() == "1"
MAP_PROVIDER = os.environ.get("EXPLORER_MAP_PROVIDER", "local")


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

        state["phase"] = "embedding"
        state["eta"] = None
        publish()
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
    except Exception as exc:
        state["state"] = "failed"
        state["message"] = str(exc)
        publish()
        return

    if BUILD_MAP:
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
        asyncio.create_task(_warm_document(str(destination), int(pages)))
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
    asyncio.create_task(_warm_document(body.path, int(info["page_count"])))
    return {"status": "started", "pages": info["page_count"]}


@app.get("/api/warm")
async def api_warm_status() -> dict[str, Any]:
    return {"warming": list(warm_state.values())}


@app.get("/api/map")
async def api_map(path: str) -> dict[str, Any]:
    data = summarizer.load_map(path)
    if not data:
        return {"present": False, "enabled": BUILD_MAP}
    return {"present": True, "enabled": BUILD_MAP, "map": data,
            "prompt_block": summarizer.render_for_prompt(data)}


@app.post("/api/map")
async def api_build_map(body: WarmRequest) -> dict[str, Any]:
    """Build a map on demand, for documents warmed before the flag was on."""
    info = _first_json_block(await client.call_tool("pdf_info", {"path": body.path}))
    if not isinstance(info, dict) or info.get("error"):
        raise HTTPException(status_code=400,
                            detail=(info or {}).get("error", "cannot read that PDF"))
    data = await summarizer.build_core_map(
        body.path, int(info["page_count"]), client.call_tool, _first_json_block,
        provider=MAP_PROVIDER)
    summarizer.save_map(data)
    return {"chapters": len(data["chapters"]), "elapsed": data["elapsed"],
            "topics": len(data["topics"])}


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
async def api_tools() -> dict[str, Any]:
    tools = await client.list_tools()
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
