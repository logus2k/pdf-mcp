/* Markdown rendering — marked + KaTeX + highlight.js.
 *
 * Ported from cv/widget/cv-chat.js, which ports noted's ChatPanel.js: GFM
 * tables, $…$ / $$…$$ math, and fenced code with syntax highlighting. The
 * CV-specific citation and bare-domain-autolink layers are left out; page
 * citations here are inserted by chat.js from real tool results rather than
 * parsed back out of the model's prose.
 *
 * All three libraries are vendored under ./vendor and loaded as globals by
 * chat.html, so this module degrades to escaped plain text if any is absent.
 */

export function escapeHtml(s) {
  return String(s)
    .split("&").join("&amp;")
    .split("<").join("&lt;")
    .split(">").join("&gt;");
}

let _mathInstalled = false;

/* Intercept math before marked processes the text, so backslashes and
 * underscores inside expressions are never mangled by markdown rules. */
function installMarkedMath() {
  if (_mathInstalled) return;
  if (typeof marked === "undefined" || typeof katex === "undefined") return;
  _mathInstalled = true;
  marked.use({
    extensions: [
      {
        name: "math_block",
        level: "block",
        start(src) { return src.indexOf("$$"); },
        tokenizer(src) {
          if (!src.startsWith("$$")) return;
          const end = src.indexOf("$$", 2);
          if (end === -1) return;
          return { type: "math_block", raw: src.slice(0, end + 2), math: src.slice(2, end) };
        },
        renderer(token) {
          try {
            return katex.renderToString(token.math.trim(),
              { displayMode: true, throwOnError: false });
          } catch (e) { return "<div>$$" + escapeHtml(token.math) + "$$</div>"; }
        },
      },
      {
        name: "math_inline",
        level: "inline",
        start(src) { return src.indexOf("$"); },
        tokenizer(src) {
          if (!src.startsWith("$")) return;
          const nl = src.indexOf("\n");
          const end = src.indexOf("$", 1);
          if (end === -1 || (nl !== -1 && nl < end)) return;
          const body = src.slice(1, end);
          if (!body.trim()) return;
          return { type: "math_inline", raw: src.slice(0, end + 1), math: body };
        },
        renderer(token) {
          try {
            return katex.renderToString(token.math.trim(),
              { displayMode: false, throwOnError: false });
          } catch (e) { return "<span>$" + escapeHtml(token.math) + "$</span>"; }
        },
      },
    ],
  });
}

export function renderMarkdown(src) {
  if (typeof marked === "undefined") return escapeHtml(src || "");
  installMarkedMath();
  try {
    return marked.parse(String(src || ""));
  } catch (e) {
    return escapeHtml(src || "");
  }
}

/* Applied after every innerHTML write so streaming and final renders both
 * pick up the visual upgrade. hljs auto-detects the language when no
 * language- class is present, which is the common case. */
export function applyMarkdownExtras(rootEl) {
  if (!rootEl) return;
  if (typeof hljs !== "undefined") {
    rootEl.querySelectorAll("pre code").forEach((block) => {
      try { hljs.highlightElement(block); } catch (e) {}
    });
  }
  if (typeof renderMathInElement !== "undefined") {
    try {
      renderMathInElement(rootEl, {
        delimiters: [
          { left: "$$", right: "$$", display: true },
          { left: "$", right: "$", display: false },
          { left: "\\(", right: "\\)", display: false },
          { left: "\\[", right: "\\]", display: true },
        ],
        throwOnError: false,
      });
    } catch (e) {}
  }
  rootEl.querySelectorAll("a[href]").forEach((a) => {
    const href = a.getAttribute("href") || "";
    if (href.startsWith("http://") || href.startsWith("https://")) {
      a.setAttribute("target", "_blank");
      a.setAttribute("rel", "noopener noreferrer");
    }
  });
}
