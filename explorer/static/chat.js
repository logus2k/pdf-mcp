/* pdf-mcp Chat — PDF beside an LLM that reads it.
 *
 * Layout mirrors docbro: tocPane | contentPane | rightPane, resizable with
 * the vendored Split.js. The middle pane hosts docbro's PdfRenderer /
 * LayoutManager / PdfControls trio via DocumentViewer (copied from cv).
 *
 * The chat streams from /api/chat: the backend runs the agent loop against
 * pdf-mcp's tools and forwards every step as an SSE event, so tool calls and
 * their results are visible rather than hidden behind a spinner.
 */

import { DocumentViewer } from "./viewer.js";
import { SUPERSAMPLE_KEY, readSupersample } from "./pdf-renderer.js";
import { ThinkingParser } from "./thinking.js";
import { renderMarkdown, applyMarkdownExtras, escapeHtml } from "./markdown.js";

const el = (id) => document.getElementById(id);

/* Opt-in tracing: append ?debug=1 to the URL. Prints every event that can
 * change the open document or the dropdown selection, so a misbehaving
 * sequence can be read straight off the console. */
const DEBUG = new URLSearchParams(location.search).has("debug");
let _traceT0 = Date.now();
function trace(...args) {
  if (!DEBUG) return;
  const t = String(Date.now() - _traceT0).padStart(6, " ");
  console.log(`%c[pdfchat +${t}ms]`, "color:#2f6feb;font-weight:bold", ...args);
}

const state = {
  files: [],
  document: null,
  viewer: null,
  history: [],      // neutral transcript sent back to the server each turn
  streaming: false,
  abort: null,
  activeCitation: null,
};

/* ---------------- viewer ---------------- */

function initViewer() {
  const host = el("contentPane");
  state.viewer = new DocumentViewer();
  host.appendChild(state.viewer.element);
}

let _openSeq = 0;

/* Dump the state of the viewer one layer below the DOM: box sizes, computed
 * visibility, scroll position, how many canvases actually exist, and the
 * renderer's per-page state machine. A pane that is blank while the page divs
 * are present is one of: zero-sized boxes, hidden/clipped ancestors, a scroll
 * position past the content, or pages stuck 'idle' (never rasterised). */
function traceGeometry(seq) {
  if (!DEBUG) return;
  const box = (sel) => {
    const e = document.querySelector(sel);
    if (!e) return "MISSING";
    const r = e.getBoundingClientRect();
    const cs = getComputedStyle(e);
    return {
      w: Math.round(r.width), h: Math.round(r.height), top: Math.round(r.top),
      display: cs.display, visibility: cs.visibility, opacity: cs.opacity,
      overflow: cs.overflow, position: cs.position,
    };
  };
  const inner = document.querySelector(".document-content-inner");
  const pages = [...document.querySelectorAll(".pdf-page")];
  const first = pages[0] ? pages[0].getBoundingClientRect() : null;

  setTimeout(() => {
    trace("GEOMETRY", seq, {
      wrapper: box(".document-viewer-wrapper"),
      container: box(".content-container"),
      content: box(".document-content"),
      inner: box(".document-content-inner"),
      innerScroll: inner
        ? { scrollTop: inner.scrollTop, scrollHeight: inner.scrollHeight,
            clientHeight: inner.clientHeight,
            gridCols: getComputedStyle(inner).gridTemplateColumns }
        : "MISSING",
      pageCount: pages.length,
      firstPage: first
        ? { w: Math.round(first.width), h: Math.round(first.height),
            top: Math.round(first.top), left: Math.round(first.left) }
        : null,
      renderStates: pages.slice(0, 5).map((d) => d._renderState),
      hasPdfPage: pages.slice(0, 3).map((d) => !!d._pdfPage),
      canvases: document.querySelectorAll("canvas").length,
      canvasSizes: [...document.querySelectorAll("canvas")].slice(0, 3)
        .map((c) => c.width + "x" + c.height),
    });
  }, 2500);   // after the eager prime has had a chance to rasterise
}

async function openDocument(path) {
  if (!path) return;
  const seq = ++_openSeq;
  trace("openDocument START", seq, path.split("/").pop());
  state.document = path;
  el("viewer-empty")?.remove();
  updateDocStatus(path);
  const url = `/api/document?path=${encodeURIComponent(path)}`;
  try {
    await state.viewer.show({ url });
    trace("viewer.show DONE", seq,
          "pageDivs=" + document.querySelectorAll(".pdf-page").length,
          "contentDivs=" + document.querySelectorAll(".document-content").length);
  } catch (err) {
    trace("viewer.show FAILED", seq, String(err));
    throw err;
  }
  traceGeometry(seq);
  if (seq !== _openSeq) {
    trace("SUPERSEDED", seq, "-> a newer open (", _openSeq, ") started; abandoning");
    return;
  }
  await loadToc(path);
  trace("openDocument END", seq, "select=" + el("doc-select").value.split("/").pop());
  refreshCacheStatus();
}

async function loadToc(path) {
  const list = el("toc-list");
  list.textContent = "";
  try {
    const res = await fetch("/api/call", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: "pdf_get_toc", arguments: { path } }),
    });
    const data = await res.json();
    const payload = JSON.parse(data.content[0].text);
    const entries = payload.toc || [];
    el("toc-count").textContent = entries.length ? String(entries.length) : "";
    if (!entries.length) {
      const li = document.createElement("li");
      li.className = "toc-empty";
      li.textContent = "No outline in this PDF.";
      list.appendChild(li);
      return;
    }
    for (const entry of entries) {
      const li = document.createElement("li");
      li.className = `toc-item lvl-${Math.min(entry.level || 1, 4)}`;
      const btn = document.createElement("button");
      btn.type = "button";
      btn.textContent = entry.title || "(untitled)";
      btn.title = `page ${entry.page}`;
      btn.addEventListener("click", () => jumpToPage(entry.page));
      li.appendChild(btn);
      list.appendChild(li);
    }
  } catch (err) {
    const li = document.createElement("li");
    li.className = "toc-empty";
    li.textContent = "Outline unavailable.";
    list.appendChild(li);
  }
}

function jumpToPage(page, bbox) {
  if (!state.viewer || !page) return;
  const regions = [{ page_no: page, bbox: bbox || null }];
  if (bbox) {
    state.viewer.showBboxHighlights(regions);
  } else {
    state.viewer.showBboxHighlights([{ page_no: page, bbox: [0, 0, 1, 1] }]);
    state.viewer.clearBboxHighlights();
  }
}

/* ---------------- chat rendering ---------------- */

function addBubble(role) {
  const scroll = el("chat-scroll");
  const wrap = document.createElement("div");
  wrap.className = `bubble ${role}`;

  const body = document.createElement("div");
  body.className = "bubble-body";
  wrap.appendChild(body);

  scroll.appendChild(wrap);
  scroll.scrollTop = scroll.scrollHeight;
  return { wrap, body };
}

function ensureThinkingBlock(wrap) {
  let block = wrap.querySelector(".think");
  if (block) return block;
  block = document.createElement("details");
  block.className = "think";
  const summary = document.createElement("summary");
  summary.textContent = "Thinking";
  const body = document.createElement("div");
  body.className = "think-body";
  block.appendChild(summary);
  block.appendChild(body);
  wrap.insertBefore(block, wrap.firstChild);
  return block;
}

function addToolChip(wrap, tool, args) {
  const chip = document.createElement("div");
  chip.className = "toolchip running";
  const name = document.createElement("span");
  name.className = "tool-name";
  name.textContent = tool;
  const detail = document.createElement("code");
  detail.textContent = summariseArgs(args);
  chip.appendChild(name);
  chip.appendChild(detail);
  // Tool calls precede the answer they inform, so insert above the answer body
  // rather than appending — reading order should match execution order.
  wrap.insertBefore(chip, wrap.querySelector(".bubble-body"));
  el("chat-scroll").scrollTop = el("chat-scroll").scrollHeight;
  return chip;
}

function summariseArgs(args) {
  const parts = [];
  for (const [k, v] of Object.entries(args || {})) {
    let shown = typeof v === "string" ? v : JSON.stringify(v);
    if (k === "path" || k === "paths") {
      const bits = String(shown).split("/");
      shown = bits[bits.length - 1];
    }
    if (shown && shown.length > 40) shown = shown.slice(0, 40) + "…";
    parts.push(`${k}=${shown}`);
  }
  return parts.join("  ");
}

function addCitations(wrap, citations) {
  if (!citations || !citations.length) return;
  const seen = new Set();
  const row = document.createElement("div");
  row.className = "cites";
  for (const cite of citations) {
    if (cite.page == null || seen.has(cite.page)) continue;
    seen.add(cite.page);
    const badge = document.createElement("button");
    badge.type = "button";
    badge.className = "cite";
    badge.textContent = `p.${cite.page}`;
    if (cite.excerpt) badge.title = cite.excerpt;
    badge.addEventListener("click", () => {
      // Clicking the active badge again dismisses the highlight.
      if (state.activeCitation === badge) {
        state.viewer?.clearBboxHighlights();
        state.activeCitation = null;
        badge.classList.remove("active");
        return;
      }
      document.querySelectorAll(".cite.active").forEach((b) => b.classList.remove("active"));
      badge.classList.add("active");
      state.activeCitation = badge;
      jumpToPage(cite.page, cite.bbox);
    });
    row.appendChild(badge);
  }
  if (row.childElementCount) wrap.insertBefore(row, wrap.querySelector(".bubble-body"));
}

document.addEventListener("bboxhighlights-cleared", () => {
  document.querySelectorAll(".cite.active").forEach((b) => b.classList.remove("active"));
  state.activeCitation = null;
});

/* ---------------- the turn ---------------- */

async function send(question) {
  if (state.streaming || !question.trim()) return;
  el("chat-intro")?.remove();

  const user = addBubble("user");
  user.body.textContent = question;
  state.history.push({ role: "user", content: question });

  const assistant = addBubble("assistant");
  const parser = new ThinkingParser();
  let answer = "";
  let thinkingBody = null;

  state.streaming = true;
  const startedAt = Date.now();
  let toolCount = 0;
  el("send-btn").disabled = true;
  el("stop-btn").hidden = false;
  status.set("activity", "waiting for model…", "busy");
  const controller = new AbortController();
  state.abort = controller;

  const paintAnswer = () => {
    assistant.body.innerHTML = renderMarkdown(answer);
    applyMarkdownExtras(assistant.body);
    el("chat-scroll").scrollTop = el("chat-scroll").scrollHeight;
  };

  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        messages: state.history,
        provider: el("provider-select").value || "local",
        document: state.document,
      }),
      signal: controller.signal,
    });

    if (!res.ok || !res.body) {
      throw new Error(`chat request failed: ${res.status}`);
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      // SSE frames are separated by a blank line.
      let idx;
      while ((idx = buffer.indexOf("\n\n")) !== -1) {
        const frame = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);
        if (!frame.startsWith("data:")) continue;
        let event;
        try {
          event = JSON.parse(frame.slice(5).trim());
        } catch (e) {
          continue;
        }

        if (event.type === "token") {
          const out = parser.processToken(event.text);
          if (out.type === "thinking_start" || out.type === "thinking_delta") {
            const block = ensureThinkingBlock(assistant.wrap);
            thinkingBody = block.querySelector(".think-body");
            thinkingBody.textContent = out.thinking || "";
          } else if (out.type === "thinking_end") {
            const block = ensureThinkingBlock(assistant.wrap);
            block.querySelector(".think-body").textContent = out.thinking || "";
            if (out.answer) { answer += out.answer; paintAnswer(); }
          } else if (out.type === "answer_delta" && out.answer) {
            answer += out.answer;
            paintAnswer();
          }
        } else if (event.type === "tool_call") {
          const chip = addToolChip(assistant.wrap, event.tool, event.arguments);
          chip.dataset.tool = event.tool;
          toolCount += 1;
          status.set("activity", `${event.tool}…`, "busy");
        } else if (event.type === "tool_result") {
          const chips = assistant.wrap.querySelectorAll(`.toolchip.running[data-tool="${event.tool}"]`);
          const chip = chips[chips.length - 1];
          if (chip) {
            chip.classList.remove("running");
            chip.classList.add(event.is_error ? "failed" : "ok");
            const meta = document.createElement("span");
            meta.className = "tool-meta";
            meta.textContent = event.is_error ? "error" : `${event.chars} chars`;
            chip.appendChild(meta);
          }
          addCitations(assistant.wrap, event.citations);
          for (const img of event.images || []) {
            const image = document.createElement("img");
            image.className = "tool-image";
            image.src = `data:${img.mimeType};base64,${img.data}`;
            assistant.wrap.appendChild(image);
          }
        } else if (event.type === "tool_error") {
          const chip = assistant.wrap.querySelector(`.toolchip.running[data-tool="${event.tool}"]`);
          if (chip) { chip.classList.remove("running"); chip.classList.add("failed"); }
        } else if (event.type === "answer") {
          const tail = parser.flush();
          if (tail) answer += tail;
          if (event.content && !answer) answer = event.content;
          paintAnswer();
        } else if (event.type === "error") {
          const box = document.createElement("div");
          box.className = "chat-error";
          box.textContent = event.message;
          assistant.wrap.appendChild(box);
          status.set("activity", "turn failed", "failed");
        }
      }
    }

    const tail = parser.flush();
    if (tail) { answer += tail; paintAnswer(); }
    if (answer) state.history.push({ role: "assistant", content: answer });
  } catch (err) {
    if (err.name !== "AbortError") {
      const box = document.createElement("div");
      box.className = "chat-error";
      box.textContent = String(err);
      assistant.wrap.appendChild(box);
    }
  } finally {
    state.streaming = false;
    state.abort = null;
    el("send-btn").disabled = false;
    el("stop-btn").hidden = true;
    const secs = ((Date.now() - startedAt) / 1000).toFixed(1);
    if (el("st-activity").className.indexOf("failed") === -1) {
      status.set("activity",
        `${toolCount} tool${toolCount === 1 ? "" : "s"} · ${secs}s`, "");
    }
    refreshCacheStatus();
  }
}

/* ---------------- chrome ---------------- */

async function loadDocuments() {
  const res = await fetch("/api/documents");
  const data = await res.json();
  state.files = data.files || [];
  const select = el("doc-select");
  select.textContent = "";
  if (!state.files.length) {
    const opt = document.createElement("option");
    opt.value = "";
    opt.textContent = "— no PDFs under allowed roots —";
    select.appendChild(opt);
    return;
  }
  for (const file of state.files) {
    const opt = document.createElement("option");
    opt.value = file.path;
    opt.textContent = file.name;
    select.appendChild(opt);
  }
  // Rebuilding the list must never steal the selection: if a document is open,
  // keep pointing at it. Only pick a default when nothing is open yet.
  // (Assigning .value does not fire 'change', so this cannot reopen anything.)
  const before = select.value;
  if (state.document && state.files.some((f) => f.path === state.document)) {
    select.value = state.document;
  } else {
    select.value = state.files[0].path;
  }
  if (before !== select.value) {
    trace("loadDocuments changed selection",
          (before || "(none)").split("/").pop(), "->", select.value.split("/").pop());
  }
}

async function loadProviders() {
  const res = await fetch("/api/providers");
  const data = await res.json();
  const select = el("provider-select");
  select.textContent = "";
  for (const [key, info] of Object.entries(data.providers)) {
    const opt = document.createElement("option");
    opt.value = key;
    opt.textContent = info.ready ? info.label : `${info.label} (unavailable)`;
    opt.disabled = !info.ready;
    opt.title = info.detail;
    select.appendChild(opt);
  }
  // Default to the first ready local model; provider keys are now
  // "local:<model id>" discovered at runtime, so no literal key is valid here.
  const firstReady = Object.entries(data.providers)
    .find(([key, info]) => info.ready && key.startsWith("local:"));
  select.value = firstReady ? firstReady[0] : select.options[0]?.value || "";
  const syncModel = () => {
    const info = data.providers[select.value];
    status.set("model", info ? info.label : select.value);
  };
  syncModel();
  select.addEventListener("change", syncModel);
  select.addEventListener("change", () => {
    const chosen = data.providers[select.value];
    el("composer-note").textContent = chosen && !chosen.ready ? chosen.detail : "";
  });
}

/* ---------------- status bar ----------------
 * One setter per slot so adding a new readout later is a single line here
 * plus a <span> in chat.html. Every value shown is real state pulled from the
 * viewer, the backend, or the running turn — no decorative placeholders.
 */

const status = {
  set(slot, text, cls) {
    const node = el(`st-${slot}`);
    if (!node) return;
    if (text !== undefined && text !== null) node.textContent = text;
    if (cls !== undefined) node.className = "st" + (cls ? " " + cls : "");
  },
  conn(text, dotClass) {
    const node = el("st-conn-text");
    if (node) node.textContent = text;
    const dot = document.querySelector("#st-conn .st-dot");
    if (dot) dot.className = "st-dot" + (dotClass ? " " + dotClass : "");
  },
};

function humanBytes(bytes) {
  if (!bytes && bytes !== 0) return "—";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/* The PDF control box already tracks page and zoom; mirror its readouts rather
 * than forking the copied renderer to emit events. Polling is cheap and keeps
 * the vendored trio untouched. */
function startViewerReadouts() {
  setInterval(() => {
    const pageInput = document.getElementById("pcbPageInput");
    const pageTotal = document.getElementById("pcbPageTotal");
    const zoom = document.getElementById("pcbZoomValue");
    if (pageInput && pageTotal && pageTotal.textContent !== "0") {
      status.set("page", `page ${pageInput.value} / ${pageTotal.textContent}`);
    }
    if (zoom && zoom.value) status.set("zoom", `zoom ${zoom.value}`);
  }, 700);
}

async function refreshCacheStatus() {
  try {
    const res = await fetch("/api/call", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: "pdf_cache_stats", arguments: {} }),
    });
    const data = await res.json();
    const s = JSON.parse(data.content[0].text);
    // Whole-cache totals, NOT the open document. Showing a page count here read
    // as "this document has 18 pages" next to a 453-page book, so the count is
    // dropped from the label and spelled out in the tooltip instead.
    status.set("cache", `cache ${s.total_files} docs · ${s.cache_size_mb} MB`);
    const node = el("st-cache");
    if (node) {
      node.title = `pdf-mcp cache across ALL documents: ${s.total_files} docs, ` +
                   `${s.total_pages} pages indexed, ${s.cache_size_mb} MB`;
    }
  } catch (err) {
    status.set("cache", "cache —");
  }
}

function updateDocStatus(path) {
  const file = state.files.find((f) => f.path === path);
  if (!file) { status.set("doc", "—"); return; }
  status.set("doc", `${file.name} · ${humanBytes(file.size_bytes)}`);
  el("st-doc").title = file.path;
}

/* ---------------- indexing progress ----------------
 * A document is viewable immediately but not answerable until pdf-mcp has
 * extracted text and computed embeddings for every page (~44s for 453 pages).
 * The backend streams that progress; this renders it into the status bar so
 * the wait is visible instead of looking like a hang. */

let _warmHideTimer = null;

function renderWarm(p) {
  const slot = el("st-warm");
  if (!slot) return;
  const total = p.total || 0;
  const units = (p.text_done || 0) + (p.emb_done || 0);
  const pct = total ? Math.min(100, Math.round((units / (2 * total)) * 100)) : 0;

  slot.hidden = false;
  slot.classList.toggle("done", p.state === "done");
  slot.classList.toggle("failed", p.state === "failed");
  el("warm-fill").style.width = pct + "%";

  if (p.state === "failed") {
    el("warm-label").textContent = "indexing failed";
    el("warm-num").textContent = p.message ? String(p.message).slice(0, 60) : "";
  } else if (p.state === "done") {
    el("warm-label").textContent = "ready";
    el("warm-num").textContent = `${total} pages · ${p.elapsed}s`;
  } else {
    el("warm-label").textContent =
      p.phase === "embedding" ? "embedding" : "extracting";
    const shown = p.phase === "embedding" ? p.emb_done : p.text_done;
    const eta = p.eta != null && p.eta > 0 ? ` · ~${Math.ceil(p.eta)}s left` : "";
    el("warm-num").textContent = `${shown}/${total}${eta}`;
  }
  slot.title = `${p.name || "document"} — ${pct}% indexed`;

  // Leave the finished state up briefly so the user sees it completed.
  if (p.state === "failed") {
    const note = el("composer-note");
    if (note) {
      note.className = "composer-note";
      note.textContent = `Indexing failed for ${p.name}: ${p.message || "unknown error"}`;
    }
  }

  clearTimeout(_warmHideTimer);
  if (p.state === "done" || p.state === "failed") {
    _warmHideTimer = setTimeout(() => { slot.hidden = true; }, 8000);
    refreshCacheStatus();
  }
}

/* ---------------- upload ---------------- */

async function uploadDocument(input) {
  const file = input.files && input.files[0];
  if (!file) return;
  const note = el("composer-note");
  const btn = el("upload-btn");
  btn.disabled = true;
  btn.textContent = "Uploading…";
  note.className = "composer-note";
  note.textContent = `Uploading ${file.name}…`;   // cleared when the POST returns

  try {
    // The bytes travel in the request body as multipart/form-data — never in
    // the URL, which a 20 MB PDF would blow past instantly.
    const form = new FormData();
    form.append("file", file, file.name);

    const res = await fetch("/api/upload", { method: "POST", body: form });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `upload failed (${res.status})`);

    await loadDocuments();
    el("doc-select").value = data.path;
    await openDocument(data.path);

    // No success note: the status-bar progress reports the document name,
    // page count and indexing state already, so a second copy of the same
    // information under the composer is redundant. Failures still speak up.
    note.className = "composer-note";
    note.textContent = data.error
      ? `Saved ${data.name}, but pdf-mcp could not read it: ${data.error}`
      : "";
  } catch (err) {
    note.className = "composer-note";
    note.textContent = String(err.message || err);
  } finally {
    btn.disabled = false;
    btn.textContent = "↑ Upload";
    input.value = "";   // let the same file be re-picked later
  }
}

/* Live document changes pushed by the backend's folder watcher, so a PDF
 * dropped into the folder by any other means shows up without a reload. */
function startDocumentWatch() {
  const source = new EventSource("/api/events");
  state.events = source;
  source.onmessage = (evt) => {
    let payload;
    try { payload = JSON.parse(evt.data); } catch (e) { return; }
    if (payload.kind === "warm") { renderWarm(payload.payload || {}); return; }
    if (payload.kind !== "documents") return;
    // Refresh the list only. This event also fires for our own uploads, and
    // reopening or re-selecting here raced the upload handler: the dropdown
    // snapped back to the previous document while the viewer was mid-load,
    // leaving a blank pane. Whoever initiated the change opens the document.
    trace("SSE documents event -> refreshing list only");
    loadDocuments();
  };
}

async function pollHealth() {
  try {
    const res = await fetch("/api/health");
    const data = await res.json();
    el("health-dot").classList.toggle("ok", data.status === "ok");
    const s = data.server || {};
    el("server-version").textContent = `${s.name || "pdf-mcp"} ${s.version || ""}`;
    status.conn(`${s.name || "pdf-mcp"} ${s.version || ""} · pid ${data.pid}`,
                data.status === "ok" ? "ok" : "down");
  } catch (err) {
    el("health-dot").classList.add("down");
    el("server-version").textContent = "backend unreachable";
    status.conn("backend unreachable", "down");
  }
}

function initTheme() {
  const saved = localStorage.getItem("explorer-theme");
  if (saved) document.documentElement.dataset.theme = saved;
  el("theme-toggle").addEventListener("click", () => {
    const current = document.documentElement.dataset.theme ||
      (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    const next = current === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    localStorage.setItem("explorer-theme", next);
  });
}

function initSplit() {
  if (typeof Split === "undefined") return;
  Split(["#tocPane", "#contentPane", "#rightPane"], {
    sizes: [16, 52, 32],
    minSize: [0, 240, 260],
    gutterSize: 6,
    cursor: "col-resize",
  });
}

/* Live crispness control. Rendering is super-sampled and downscaled, which
 * sharpens edges; too high and glyph antialiasing gets crushed. Exposed so the
 * value can be compared side by side without an edit-reload cycle. */
function installSupersampleControl() {
  window.pdfSupersample = (value) => {
    if (value === undefined) return readSupersample();
    const n = Math.min(4, Math.max(1, Number.parseFloat(value)));
    if (!Number.isFinite(n)) {
      console.warn("pdfSupersample(n): pass a number between 1 and 4");
      return readSupersample();
    }
    try { localStorage.setItem(SUPERSAMPLE_KEY, String(n)); } catch (e) {}
    const repainted = state.viewer?.rerenderAll?.() ?? 0;
    console.log(`%c[pdfchat] supersample = ${n} — re-rendering ${repainted} page(s)`,
                "color:#2f6feb;font-weight:bold");
    return n;
  };
  trace("supersample", readSupersample());
}

async function boot() {
  installSupersampleControl();
  initTheme();
  initSplit();
  initViewer();
  startDocumentWatch();
  startViewerReadouts();
  await pollHealth();
  refreshCacheStatus();
  setInterval(pollHealth, 15000);
  await loadProviders();
  await loadDocuments();

  const select = el("doc-select");
  select.addEventListener("change", () => {
    trace("dropdown CHANGE ->", select.value.split("/").pop());
    openDocument(select.value);
  });
  if (select.value) await openDocument(select.value);

  el("page-jump-btn").addEventListener("click", () => {
    const n = Number.parseInt(el("page-jump-input").value, 10);
    if (!Number.isNaN(n)) jumpToPage(n);
  });

  const input = el("chat-input");
  el("composer").addEventListener("submit", (e) => {
    e.preventDefault();
    const q = input.value;
    input.value = "";
    input.style.height = "auto";
    send(q);
  });
  input.addEventListener("input", () => {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 160) + "px";
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      el("composer").requestSubmit();
    }
  });
  el("stop-btn").addEventListener("click", () => state.abort?.abort());

  const uploadInput = el("upload-input");
  el("upload-btn").addEventListener("click", () => uploadInput.click());
  uploadInput.addEventListener("change", () => uploadDocument(uploadInput));
}

boot();
