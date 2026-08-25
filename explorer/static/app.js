/* pdf-mcp Explorer — schema-driven UI.
 *
 * Nothing about the 13 tools is hardcoded here: the forms are built from the
 * inputSchema that tools/list returns, so the page follows pdf-mcp's own
 * definitions rather than a copy of them that can drift.
 */

const state = {
  tools: [],
  active: null,
  files: [],
  lastResult: null,
  view: "pretty",
};

const el = (id) => document.getElementById(id);

/* ---------------- schema helpers ---------------- */

// A property may be declared directly or wrapped in anyOf (optional values).
function describeType(schema) {
  if (!schema) return "any";
  if (schema.type) return schema.type;
  if (Array.isArray(schema.anyOf)) {
    const parts = schema.anyOf
      .map((s) => s.type)
      .filter((t) => t && t !== "null");
    if (parts.length) return parts.join(" | ");
  }
  return "any";
}

function isNullable(schema) {
  return (
    Array.isArray(schema.anyOf) && schema.anyOf.some((s) => s.type === "null")
  );
}

function buildField(name, schema, required) {
  const type = describeType(schema);
  const wrap = document.createElement("div");
  wrap.className = type === "boolean" ? "field checkbox" : "field";

  const label = document.createElement("label");
  label.className = "field-label";
  label.htmlFor = `f-${name}`;

  const fname = document.createElement("span");
  fname.className = "fname";
  fname.textContent = name;
  label.appendChild(fname);

  const ftype = document.createElement("span");
  ftype.className = "ftype";
  ftype.textContent = type;
  label.appendChild(ftype);

  if (required) {
    const req = document.createElement("span");
    req.className = "req";
    req.textContent = "required";
    label.appendChild(req);
  }

  let input;
  if (type === "boolean") {
    input = document.createElement("input");
    input.type = "checkbox";
    input.checked = schema.default === true;
  } else if (type === "integer" || type === "number") {
    input = document.createElement("input");
    input.type = "number";
    if (schema.default !== undefined && schema.default !== null) {
      input.value = schema.default;
    }
    input.placeholder = schema.default === null ? "(none)" : "";
  } else {
    input = document.createElement("input");
    input.type = "text";
    if (schema.default !== undefined && schema.default !== null) {
      input.value = schema.default;
    }
    if (isNullable(schema)) input.placeholder = "(optional)";
  }
  input.id = `f-${name}`;
  input.name = name;
  input.dataset.jsonType = type;

  if (type === "boolean") {
    wrap.appendChild(input);
    wrap.appendChild(label);
  } else {
    wrap.appendChild(label);
    wrap.appendChild(input);
  }

  if (schema.description) {
    const help = document.createElement("p");
    help.className = "field-help";
    help.textContent = schema.description;
    wrap.appendChild(help);
  }
  return wrap;
}

function collectArguments(form) {
  const args = {};
  for (const input of form.querySelectorAll("input")) {
    const type = input.dataset.jsonType;
    if (type === "boolean") {
      if (input.checked) args[input.name] = true;
      continue;
    }
    const raw = input.value.trim();
    if (raw === "") continue;
    if (type === "integer") {
      const n = Number.parseInt(raw, 10);
      if (!Number.isNaN(n)) args[input.name] = n;
    } else if (type === "number") {
      const n = Number.parseFloat(raw);
      if (!Number.isNaN(n)) args[input.name] = n;
    } else {
      args[input.name] = raw;
    }
  }
  return args;
}

/* ---------------- tool list ---------------- */

function renderToolList(filterText) {
  const filter = (filterText || "").toLowerCase();
  const list = el("tool-list");
  list.textContent = "";
  const shown = state.tools.filter((t) => t.name.toLowerCase().includes(filter));
  for (const tool of shown) {
    const li = document.createElement("li");
    const btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = tool.name;
    btn.title = firstLine(tool.description || "");
    if (state.active && state.active.name === tool.name) btn.classList.add("active");
    btn.addEventListener("click", () => selectTool(tool.name));
    li.appendChild(btn);
    list.appendChild(li);
  }
  el("tool-count").textContent = `${shown.length}/${state.tools.length}`;
}

function firstLine(text) {
  const idx = text.indexOf("\n");
  return idx === -1 ? text : text.slice(0, idx);
}

// The server prefixes each description with a SECURITY notice; show the part
// that actually describes the tool.
function usefulDescription(text) {
  if (!text) return "";
  const marker = "\n\n";
  const parts = text.split(marker);
  if (parts.length > 1 && parts[0].startsWith("SECURITY:")) {
    return parts.slice(1).join(marker).trim();
  }
  return text.trim();
}

function selectTool(name) {
  const tool = state.tools.find((t) => t.name === name);
  if (!tool) return;
  state.active = tool;

  el("tool-name").textContent = tool.name;
  el("tool-desc").textContent = usefulDescription(tool.description);

  const form = el("tool-form");
  form.textContent = "";
  const schema = tool.inputSchema || {};
  const props = schema.properties || {};
  const required = new Set(schema.required || []);

  const names = Object.keys(props).sort((a, b) => {
    const ra = required.has(a) ? 0 : 1;
    const rb = required.has(b) ? 0 : 1;
    if (ra !== rb) return ra - rb;
    return a.localeCompare(b);
  });

  if (names.length === 0) {
    const note = document.createElement("p");
    note.className = "note";
    note.textContent = "This tool takes no arguments.";
    form.appendChild(note);
  }
  for (const propName of names) {
    form.appendChild(buildField(propName, props[propName], required.has(propName)));
  }

  applySelectedDocument();
  el("actions").hidden = false;
  el("result-head").hidden = true;
  el("result").textContent = "";
  el("timing").textContent = "";
  renderToolList(el("tool-filter").value);
}

/* ---------------- document picker ---------------- */

function applySelectedDocument() {
  const chosen = el("doc-select").value;
  if (!chosen) return;
  for (const fieldName of ["path", "paths"]) {
    const input = document.getElementById(`f-${fieldName}`);
    if (input && input.value.trim() === "") input.value = chosen;
  }
}

function humanSize(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

async function loadDocuments() {
  const res = await fetch("api/documents");
  const data = await res.json();
  state.files = data.files || [];
  const select = el("doc-select");
  select.textContent = "";

  const blank = document.createElement("option");
  blank.value = "";
  blank.textContent = state.files.length
    ? "— pick a document —"
    : "— no PDFs under allowed roots —";
  select.appendChild(blank);

  for (const file of state.files) {
    const opt = document.createElement("option");
    opt.value = file.path;
    opt.textContent = `${file.name}  (${humanSize(file.size_bytes)})`;
    select.appendChild(opt);
  }

  const roots = (data.documents && data.documents.roots) || [];
  select.title = roots.length ? `Allowed roots:\n${roots.join("\n")}` : "";
  // Preselect the first document: a required `path` left blank just makes the
  // first Run fail with a validation error, which teaches nothing.
  if (state.files.length) {
    select.value = state.files[0].path;
    applySelectedDocument();
  }
}

/* ---------------- running a tool ---------------- */

async function runTool() {
  if (!state.active) return;
  const runBtn = el("run-btn");
  runBtn.disabled = true;
  runBtn.textContent = "Running…";
  el("result").textContent = "";
  el("result-head").hidden = false;

  const args = collectArguments(el("tool-form"));
  try {
    const res = await fetch("api/call", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: state.active.name, arguments: args }),
    });
    const data = await res.json();
    if (!res.ok) {
      showError(data.detail || JSON.stringify(data));
      return;
    }
    state.lastResult = data;
    el("timing").textContent = data._elapsed_ms ? `${data._elapsed_ms} ms` : "";
    renderResult();
  } catch (err) {
    showError(String(err));
  } finally {
    runBtn.disabled = false;
    runBtn.textContent = "Run";
  }
}

function showErrorAppend(target, message) {
  const box = document.createElement("div");
  box.className = "error-box";
  box.textContent = message;
  target.appendChild(box);
}

function showError(message) {
  const target = el("result");
  target.textContent = "";
  showErrorAppend(target, message);
}

/* ---------------- result rendering ---------------- */

function renderResult() {
  const target = el("result");
  target.textContent = "";
  const data = state.lastResult;
  if (!data) return;

  if (state.view === "raw") {
    const pre = document.createElement("pre");
    pre.textContent = JSON.stringify(data, null, 2);
    target.appendChild(pre);
    return;
  }

  // FastMCP reports tool-level failures as a normal result carrying isError,
  // not as a JSON-RPC error, so this is the only place they surface.
  if (data.isError) {
    for (const block of data.content || []) {
      if (block.type === "text") showErrorAppend(target, block.text);
    }
    if (target.hasChildNodes()) return;
  }

  for (const block of data.content || []) {
    if (block.type === "image") {
      const img = document.createElement("img");
      img.src = `data:${block.mimeType || "image/png"};base64,${block.data}`;
      img.alt = "rendered page";
      target.appendChild(img);
    } else if (block.type === "text") {
      renderTextBlock(target, block.text);
    }
  }
  if (!target.hasChildNodes()) {
    const pre = document.createElement("pre");
    pre.textContent = JSON.stringify(data, null, 2);
    target.appendChild(pre);
  }
}

function renderTextBlock(target, text) {
  let payload;
  try {
    payload = JSON.parse(text);
  } catch (err) {
    const pre = document.createElement("pre");
    pre.textContent = text;
    target.appendChild(pre);
    return;
  }

  if (payload && payload.error) {
    const box = document.createElement("div");
    box.className = "error-box";
    box.textContent = payload.hint
      ? `${payload.error}\n\nhint: ${payload.hint}`
      : payload.error;
    target.appendChild(box);
    return;
  }

  if (payload && Array.isArray(payload.matches)) {
    renderMatches(target, payload);
    return;
  }
  if (payload && Array.isArray(payload.pages)) {
    renderPages(target, payload);
    return;
  }

  const pre = document.createElement("pre");
  pre.textContent = JSON.stringify(payload, null, 2);
  target.appendChild(pre);
}

function renderMatches(target, payload) {
  const summary = document.createElement("p");
  summary.className = "note";
  const bits = [`${payload.matches.length} shown`];
  if (payload.total_matches !== undefined) bits.push(`${payload.total_matches} total`);
  if (payload.search_mode) bits.push(`mode: ${payload.search_mode}`);
  if (payload.searched_pages) bits.push(`${payload.searched_pages} pages searched`);
  summary.textContent = bits.join(" · ");
  target.appendChild(summary);

  for (const match of payload.matches) {
    const card = document.createElement("div");
    card.className = "hit";

    const head = document.createElement("div");
    head.className = "hit-head";
    const page = document.createElement("span");
    page.className = "hit-page";
    page.textContent = `p.${match.page}`;
    head.appendChild(page);
    if (match.score !== undefined) {
      const score = document.createElement("span");
      score.textContent = `score ${Number(match.score).toFixed(4)}`;
      head.appendChild(score);
    }
    if (match.semantic_score !== undefined) {
      const sem = document.createElement("span");
      sem.textContent = `semantic ${Number(match.semantic_score).toFixed(3)}`;
      head.appendChild(sem);
    }
    if (match.doc_title) {
      const doc = document.createElement("span");
      doc.textContent = match.doc_title;
      head.appendChild(doc);
    }
    card.appendChild(head);

    const body = document.createElement("p");
    body.className = "hit-text";
    body.textContent = match.excerpt || "";
    card.appendChild(body);
    target.appendChild(card);
  }
}

function renderPages(target, payload) {
  const summary = document.createElement("p");
  summary.className = "note";
  const bits = [`${payload.pages.length} page(s)`];
  if (payload.total_chars !== undefined) bits.push(`${payload.total_chars} chars`);
  if (payload.estimated_tokens !== undefined) bits.push(`~${payload.estimated_tokens} tokens`);
  summary.textContent = bits.join(" · ");
  target.appendChild(summary);

  for (const page of payload.pages) {
    const card = document.createElement("div");
    card.className = "hit";
    const head = document.createElement("div");
    head.className = "hit-head";
    const num = document.createElement("span");
    num.className = "hit-page";
    num.textContent = `p.${page.page}`;
    head.appendChild(num);
    const meta = document.createElement("span");
    meta.textContent = `${page.chars ?? 0} chars · ${page.image_count ?? 0} images · ${page.table_count ?? 0} tables`;
    head.appendChild(meta);
    card.appendChild(head);

    const body = document.createElement("p");
    body.className = "hit-text";
    body.textContent = page.text || "(no text)";
    card.appendChild(body);
    target.appendChild(card);
  }
}

/* ---------------- activity feed ---------------- */

function addLogLine(kind, text) {
  const log = el("log");
  const li = document.createElement("li");
  const time = document.createElement("span");
  time.className = "t";
  time.textContent = new Date().toLocaleTimeString();
  const label = document.createElement("span");
  label.className = kind;
  label.textContent = `${kind} `;
  li.appendChild(time);
  li.appendChild(label);
  li.appendChild(document.createTextNode(text));
  log.prepend(li);
  while (log.childElementCount > 200) log.removeChild(log.lastElementChild);
}

function startEvents() {
  const source = new EventSource("api/events");
  state.events = source; // exposed so tests/tools can close the stream
  source.onmessage = (event) => {
    let payload;
    try {
      payload = JSON.parse(event.data);
    } catch (err) {
      return;
    }
    if (payload.kind === "hello") return;
    const body = payload.payload || {};
    if (payload.kind === "call") {
      addLogLine("call", `${body.tool} ${JSON.stringify(body.arguments)}`);
    } else if (payload.kind === "done") {
      addLogLine("done", `${body.tool} in ${body.elapsed_ms} ms`);
    } else if (payload.kind === "error") {
      addLogLine("error", `${body.tool}: ${body.message}`);
    } else if (payload.kind === "documents") {
      // Pushed by the backend's folder watcher — refresh the picker in place,
      // no page reload. This is the SPA update path.
      addLogLine("done", `documents changed (${body.count}); refreshing picker`);
      const previous = el("doc-select").value;
      loadDocuments().then(() => {
        const select = el("doc-select");
        if (previous && state.files.some((f) => f.path === previous)) {
          select.value = previous;
        }
      });
    } else {
      addLogLine("notification", JSON.stringify(body).slice(0, 300));
    }
  };
  source.onerror = () => addLogLine("error", "event stream dropped; retrying");
}

/* ---------------- health + theme ---------------- */

async function pollHealth() {
  try {
    const res = await fetch("api/health");
    const data = await res.json();
    const dot = el("health-dot");
    dot.classList.toggle("ok", data.status === "ok");
    dot.classList.toggle("down", data.status !== "ok");
    const name = data.server && data.server.name ? data.server.name : "pdf-mcp";
    const version = data.server && data.server.version ? data.server.version : "?";
    el("server-version").textContent = `${name} ${version} · pid ${data.pid}`;
  } catch (err) {
    el("health-dot").classList.add("down");
    el("server-version").textContent = "backend unreachable";
  }
}

function initTheme() {
  const saved = localStorage.getItem("explorer-theme");
  if (saved) document.documentElement.dataset.theme = saved;
  el("theme-toggle").addEventListener("click", () => {
    const current =
      document.documentElement.dataset.theme ||
      (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    const next = current === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    localStorage.setItem("explorer-theme", next);
  });
}

/* ---------------- boot ---------------- */

async function boot() {
  initTheme();
  startEvents();
  await pollHealth();
  setInterval(pollHealth, 10000);

  const res = await fetch("api/tools");
  const data = await res.json();
  state.tools = (data.tools || []).sort((a, b) => a.name.localeCompare(b.name));
  renderToolList("");
  await loadDocuments();

  el("tool-filter").addEventListener("input", (e) => renderToolList(e.target.value));
  el("run-btn").addEventListener("click", runTool);
  el("reset-btn").addEventListener("click", () => {
    if (state.active) selectTool(state.active.name);
  });
  el("doc-select").addEventListener("change", applySelectedDocument);
  for (const tab of document.querySelectorAll(".tab")) {
    tab.addEventListener("click", () => {
      for (const other of document.querySelectorAll(".tab")) {
        other.classList.toggle("active", other === tab);
      }
      state.view = tab.dataset.view;
      renderResult();
    });
  }

  if (state.tools.length) selectTool("pdf_info");
}

boot();
