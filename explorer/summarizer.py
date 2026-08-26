"""
Core-map generation: a compact navigation aid for one document.

Why this exists
---------------
The agent searches blind. It guesses query terms with no picture of the
document, which is fine for "what does it say about X" and poor for "what is
this about", "how do chapters 3 and 9 relate", or simply knowing where to look.

What it is NOT: a replacement for retrieval. The map is injected into the system
prompt for navigation only; every answer still comes from pdf_search /
pdf_read_pages so it stays grounded in real pages with citations. A summary has
no page anchor and must never become the source of an answer.

Method borrowed from book-to-skill (MIT, virgiliojr94/book-to-skill): the
hierarchical core-map idea and its Quality Rules. Their pipeline also emits
per-chapter files, a glossary and a cheatsheet; those duplicate retrieval we
already have, so only the core is generated here.

Per chapter we ask for a small JSON object rather than prose (~200 tokens out
instead of ~1,500), because the map needs structure, not readable summaries.
One final pass turns those objects into the framework digest.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

import providers

MAP_DIR = Path(os.environ.get(
    "EXPLORER_MAP_DIR", str(Path.home() / ".cache" / "pdf-mcp-explorer" / "maps")))

# Chapters shorter than this are usually front matter or section dividers.
MIN_CHAPTER_PAGES = 3
# Guard against a pathological TOC turning one document into hundreds of calls.
MAX_CHAPTERS = int(os.environ.get("EXPLORER_MAP_MAX_CHAPTERS", "40"))
# Per-chapter text handed to the model. Chapters longer than this are sampled
# head+tail rather than blindly truncated, and the model is told so.
CHAPTER_CHAR_BUDGET = int(os.environ.get("EXPLORER_MAP_CHAPTER_CHARS", "18000"))
# Chapters are indexed concurrently, sized to the slots the model server will
# actually serve (llama.cpp --parallel). Sequential indexing left every slot but
# one idle: 32 chapters at ~4s each ran 127s with half the machine unused.
# 0 = discover from the provider.
MAP_CONCURRENCY = int(os.environ.get("EXPLORER_MAP_CONCURRENCY", "0"))

# Quality Rules from book-to-skill's SKILL.md, trimmed to the ones that bear on
# a navigation map. Rule 2 (exact naming) is what makes the topic index usable;
# rule 7 also keeps generated text clear of reproducing the book.
RULES = """Rules:
1. Extract structure, not summaries - named frameworks, exact formulations, anti-patterns.
2. Preserve the author's precision - "The 5 Whys" != "ask why multiple times". Keep exact naming.
3. Density over completeness.
4. Practitioner voice - "Use X when Y", not "The book explains X".
7. Never copy raw text - synthesize."""

CHAPTER_PROMPT = """You are indexing one chapter of a document so an agent can
navigate it later. """ + RULES + """

Return ONLY a JSON object, no prose, no code fence:
{"one_line": "<=25 words, what this chapter is for, practitioner voice",
 "frameworks": ["exact named framework or method", "..."],
 "terms": ["key term", "..."]}

At most 6 frameworks and 10 terms. Use the author's exact names. If the chapter
introduces no named framework, return an empty list rather than inventing one."""

DIGEST_PROMPT = """You are writing the core orientation for an agent that will
answer questions about this document. """ + RULES + """

You are given the document title and a per-chapter index. Write markdown:

## What this document is
<2-3 sentences: subject, argument, who it is for.>

## Core frameworks
<The 5-8 most important named frameworks across the whole document. For each:
**Name** (pp.X-Y) - what it is, and when to use it. One or two lines each.
Copy the page range verbatim from the chapter index; never invent one.>

Do not invent anything that is not in the chapter index. Under 700 words."""


def map_path_for(pdf_path: str) -> Path:
    digest = hashlib.sha256(pdf_path.encode()).hexdigest()[:16]
    return MAP_DIR / f"{digest}.json"


def load_map(pdf_path: str) -> dict[str, Any] | None:
    """Return a stored map, or None if absent or stale.

    Keyed on path + mtime + size, matching pdf-mcp's own cache invalidation, so
    replacing a document silently discards its map instead of describing the
    previous file.
    """
    target = map_path_for(pdf_path)
    if not target.is_file():
        return None
    try:
        data = json.loads(target.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    try:
        stat = Path(pdf_path).stat()
    except OSError:
        return None
    if data.get("mtime") != stat.st_mtime or data.get("size") != stat.st_size:
        return None
    return data


def _segment_chapters(toc: list[dict[str, Any]], page_count: int,
                      _want_source: bool = False):
    """Turn a flat TOC into non-overlapping page ranges.

    Prefers the shallowest level that yields a usable number of chapters: a
    453-page book with 382 TOC entries would otherwise produce 382 LLM calls.
    """
    def _out(chapters, source):
        return (chapters, source) if _want_source else chapters

    # No early return for an empty TOC: it used to hand back one whole-document
    # "chapter", so a document with ZERO entries — exactly the case the map
    # exists for — got the weakest possible index. Fall through to the block
    # splitter below, which is the real fallback.

    # Pick the level giving the FINEST usable granularity, not the shallowest.
    # Shallowest-first produced "Part I: The Patterns, pp.40-355" as a single
    # chapter on a 453-page book — a 315-page span is useless for navigation,
    # and the digest then cited the table-of-contents pages as the home of a
    # pattern taught 200 pages later.
    best: list[dict[str, Any]] | None = None
    for level in sorted({int(e.get("level", 1)) for e in toc}):
        entries = [e for e in toc if int(e.get("level", 1)) == level and e.get("page")]
        if not entries:
            continue
        chapters = []
        for i, entry in enumerate(entries):
            start = int(entry["page"])
            end = (int(entries[i + 1]["page"]) - 1) if i + 1 < len(entries) else page_count
            if end - start + 1 < MIN_CHAPTER_PAGES:
                continue
            chapters.append({"title": str(entry.get("title") or f"Section {i+1}"),
                             "start": start, "end": min(end, page_count), "level": level})
        if not (2 <= len(chapters) <= MAX_CHAPTERS):
            continue
        # Reject a level where any single span swallows a big slice of the book.
        widest = max((c["end"] - c["start"] + 1) for c in chapters)
        if page_count > 60 and widest > page_count * 0.25:
            continue
        # More chapters == finer navigation, so keep the largest valid split.
        if best is None or len(chapters) > len(best):
            best = chapters
    if best:
        return _out(best, "toc")

    # No level gave a workable split: fall back to fixed page blocks so a
    # document with a useless TOC still gets a map. This path is the whole
    # feature for TOC-less documents, so it must not be coarse: the previous
    # formula gave an 18-page paper two 9-page blocks, which is barely an index
    # at all. Target ~15 pages per block, with a floor of 4 blocks.
    target_blocks = min(MAX_CHAPTERS, max(4, page_count // 15))
    block = max(3, -(-page_count // target_blocks))
    return _out([{"title": f"Pages {s}-{min(s + block - 1, page_count)}",
                  "start": s, "end": min(s + block - 1, page_count), "level": 0}
                 for s in range(1, page_count + 1, block)], "fallback")


def toc_is_navigable(toc: list[dict[str, Any]], page_count: int) -> bool:
    """Can the agent navigate this document from its own table of contents?

    Decided by the same segmentation the map would use, rather than an
    arbitrary entry-count threshold: if a TOC level yields a workable set of
    page ranges, pdf_get_toc already gives the agent that index losslessly and
    for free, so a generated map is redundant.

    Measured over 144 A/B turns: on a 453-page book with 382 TOC entries the
    map was slightly WORSE (22/24 vs 23/24 on-target, 8.3 vs 10.0 page refs);
    on an 18-page paper whose TOC held a single entry it was clearly BETTER
    (27/27 vs 25/27, 4.0 vs 2.9 page refs).
    """
    _, source = _segment_chapters(toc, page_count, _want_source=True)
    return source == "toc"


def _sample(text: str, budget: int = CHAPTER_CHAR_BUDGET) -> str:
    """Head+tail sample for an oversized chapter, labelled so the model knows.

    A blind head-truncation would hide a chapter's conclusions, which is often
    where the author names the framework.
    """
    if len(text) <= budget:
        return text
    head = int(budget * 0.7)
    tail = budget - head
    return (text[:head]
            + f"\n\n[... {len(text) - budget} characters omitted from the middle ...]\n\n"
            + text[-tail:])


async def _ask(provider: str, system: str, user: str, max_tokens: int) -> str:
    chunks: list[str] = []
    history = [{"role": "user", "content": user}]
    async for event in providers.stream(provider, history, [], system):
        if event.get("type") == "text":
            chunks.append(event["text"])
        elif event.get("type") == "error":
            raise RuntimeError(event["message"])
    return "".join(chunks)


def _parse_json_object(raw: str) -> dict[str, Any]:
    """Recover a JSON object from a model reply that may wrap it in prose."""
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("```")[1] if "```" in text[3:] else text[3:]
        if text.lstrip().startswith("json"):
            text = text.lstrip()[4:]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("no JSON object in reply")
    return json.loads(text[start:end + 1])


async def build_core_map(
    pdf_path: str,
    page_count: int,
    call_tool: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]],
    extract_json: Callable[[dict[str, Any]], Any],
    provider: str = "local",
    on_progress: Callable[[int, int, str], None] | None = None,
) -> dict[str, Any]:
    started = time.monotonic()

    toc_result = extract_json(await call_tool("pdf_get_toc", {"path": pdf_path}))
    toc = toc_result.get("toc", []) if isinstance(toc_result, dict) else []
    chapters, segmentation = _segment_chapters(toc, page_count, _want_source=True)

    slots = MAP_CONCURRENCY or await providers.slots_for(provider)
    limit = asyncio.Semaphore(max(1, slots))
    done = 0

    async def index_chapter(chapter: dict[str, Any]) -> dict[str, Any] | None:
        nonlocal done
        # pdf_read_pages is cheap and already serialised over the one stdio
        # pipe; only the LLM call is gated, since that is what the slots limit.
        pages = extract_json(await call_tool(
            "pdf_read_pages",
            {"path": pdf_path, "pages": f"{chapter['start']}-{chapter['end']}"}))
        text = "\n\n".join(p.get("text", "") for p in (pages or {}).get("pages", []))
        if not text.strip():
            done += 1
            return None
        async with limit:
            try:
                reply = await _ask(
                    provider, CHAPTER_PROMPT,
                    f"Chapter title: {chapter['title']}\n"
                    f"Pages {chapter['start']}-{chapter['end']}\n\n{_sample(text)}",
                    max_tokens=400)
                parsed = _parse_json_object(reply)
            except Exception as exc:
                # One bad chapter must not lose the rest of the map.
                parsed = {"one_line": f"(indexing failed: {exc})",
                          "frameworks": [], "terms": []}
        done += 1
        if on_progress:
            on_progress(done, len(chapters), chapter["title"])
        return {
            "title": chapter["title"],
            "start": chapter["start"], "end": chapter["end"],
            "one_line": str(parsed.get("one_line") or "")[:300],
            "frameworks": [str(f)[:80] for f in (parsed.get("frameworks") or [])][:6],
            "terms": [str(t)[:60] for t in (parsed.get("terms") or [])][:10],
        }

    if on_progress:
        on_progress(0, len(chapters), f"indexing with {slots} slot(s)")
    # gather preserves order, so the chapter index stays in document order.
    results = await asyncio.gather(*(index_chapter(c) for c in chapters))
    indexed = [r for r in results if r]

    if on_progress:
        on_progress(len(chapters), len(chapters), "assembling")

    index_lines = [
        f"- pp.{c['start']}-{c['end']} | {c['title']} | {c['one_line']}"
        + (f" | frameworks: {', '.join(c['frameworks'])}" if c["frameworks"] else "")
        for c in indexed
    ]
    try:
        digest = await _ask(
            provider, DIGEST_PROMPT,
            f"Document: {Path(pdf_path).stem}\nPages: {page_count}\n\n"
            "Chapter index:\n" + "\n".join(index_lines),
            max_tokens=1200)
    except Exception as exc:
        digest = f"(digest generation failed: {exc})"

    # Topic index: term -> pages. Built in code, not by the model, so it cannot
    # point at a chapter that does not exist.
    topics: dict[str, list[str]] = {}
    for c in indexed:
        for term in c["terms"] + c["frameworks"]:
            key = term.strip()
            if not key:
                continue
            topics.setdefault(key.lower(), []).append(f"pp.{c['start']}-{c['end']}")

    stat = Path(pdf_path).stat()
    return {
        "path": pdf_path, "name": Path(pdf_path).name,
        "mtime": stat.st_mtime, "size": stat.st_size,
        "page_count": page_count, "provider": provider,
        "generated_at": time.time(),
        "elapsed": round(time.monotonic() - started, 1),
        "segmentation": segmentation,
        "chapters": indexed,
        "digest": digest.strip(),
        "topics": {k: sorted(set(v)) for k, v in sorted(topics.items())},
    }


def save_map(data: dict[str, Any]) -> Path:
    MAP_DIR.mkdir(parents=True, exist_ok=True)
    target = map_path_for(data["path"])
    target.write_text(json.dumps(data, indent=1))
    return target


def render_for_prompt(data: dict[str, Any], max_topics: int = 60) -> str:
    """Flatten a stored map into the block injected into the system prompt."""
    # Deliberately minimal. A fuller block (digest + per-chapter one-liners,
    # ~2,800 tokens) was measured over 72 A/B turns: it won 24/24 vs 22/24 on
    # finding the right pages, but produced 6 front-matter locators against 1,
    # cost 19% more latency and yielded 17% fewer page citations. The prose was
    # what the model paraphrased into wrong page numbers, so only the pointers
    # survive here — the part that produced the win.
    lines = [
        "=== DOCUMENT INDEX (pointers only — NOT a source) ===",
        f"{data['name']}, {data['page_count']} pages.",
        "Use this ONLY to choose which pages to open. Never quote or answer from",
        "it; cite the specific pages your tool call returns.",
        "",
        "Sections:",
    ]
    for c in data.get("chapters", []):
        lines.append(f"  pp.{c['start']}-{c['end']}  {c['title']}")
    topics = list(data.get("topics", {}).items())[:max_topics]
    if topics:
        lines += ["", "Topics:"]
        for term, where in topics:
            lines.append(f"  {term} → {where[0]}")
    lines.append("=== end of index ===")
    return "\n".join(lines)
