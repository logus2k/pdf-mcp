"""
Figure descriptions: make a document's diagrams searchable.

Why this exists
---------------
Everything else in this app indexes text. A page's figures were extracted by
pdf-mcp (1,540 PNGs cached for two documents) and then used for nothing: the
semantic index never saw them, and the agent could render a page for the reader
to look at but never see it itself. Ask "what does the needs-to-requirements
diagram show" and the answer came from whatever prose happened to sit near the
caption.

The local model is multimodal, so a figure can be described at ingestion time
and the description indexed as ordinary text. The answer then cites the page
the figure is on, and the viewer highlights it.

What makes this affordable
--------------------------
Naively, "describe every image" means one vision call per extracted image: 164
for the INCOSE guide, of which 115 are the same header logo. Measured on that
document, 42 images are distinct and one appears on every single page.

So images are grouped by page and filtered twice — repeated furniture is
dropped, and a page must give its figures a minimum share of its area before it
is worth a call. That takes the INCOSE guide from 164 calls to 6.

A whole page is sent rather than a cropped image. The crop loses the caption,
and the caption is what names the figure: sending page 11 whole is what let the
model answer "Figure 2: Needs and Requirements in Context" instead of
describing an unlabelled box diagram.
"""

from __future__ import annotations

import asyncio
import base64
import collections
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

import httpx

import providers

# An image repeated across the document is furniture — a logo, a rule, a footer
# badge. Four placements is enough to establish that: real figures appear once,
# occasionally twice when a document repeats one for reference.
BOILERPLATE_MIN_PAGES = int(os.environ.get("EXPLORER_FIGURE_BOILERPLATE_PAGES", "4"))

# Share of the page its non-repeated images must cover before the page is worth
# a vision call. Below this the "figure" is an inline icon or a signature.
MIN_PAGE_COVERAGE = float(os.environ.get("EXPLORER_FIGURE_MIN_COVERAGE", "0.02"))

# Render scale for the page sent to the model. 2x the PDF's own 72dpi keeps
# diagram labels legible without inflating the request.
RENDER_SCALE = float(os.environ.get("EXPLORER_FIGURE_RENDER_SCALE", "2.0"))

# Vision calls run concurrently, sized to the model server's own --parallel.
# 0 = discover it from the provider at run time rather than pinning a number
# that goes stale when the router is relaunched with different flags.
CONCURRENCY = int(os.environ.get("EXPLORER_FIGURE_CONCURRENCY", "0"))

INSTRUCTION = (
    "This is a page from a technical document. Describe the figures, diagrams, "
    "charts and tables shown on it, so that someone searching later can find "
    "them by what they contain. Name every label, axis, box, arrow and legend "
    "entry you can read, and state what the diagram shows and how its parts "
    "relate. If the figure has a caption, quote it exactly. Ignore body "
    "paragraphs and page headers or footers. If the page has no figure, reply "
    "with exactly: NO FIGURE."
)


# Used when the first pass answers "NO FIGURE" for a page the geometry says
# carries one. Measured on page 108 of the INCOSE guide: at temperature 0.2 the
# model called it NO FIGURE on 2 of 4 attempts and described Figure C-1 on the
# other 2; at temperature 0 it chose NO FIGURE every time. Lowering the
# temperature therefore makes recall WORSE, deterministically. Since a page only
# reaches here after a non-repeated image was found covering enough of it, the
# refusal contradicts the evidence, and the retry says so rather than re-rolling
# the same prompt and hoping for a better sample.
INSTRUCTION_INSIST = (
    "This page contains at least one image, diagram, chart or figure — its "
    "position on the page has already been detected. Describe what that image "
    "shows, in enough detail that someone searching later can find it by its "
    "contents. Name every label, axis, box, arrow and legend entry you can "
    "read. If the figure has a caption, quote it exactly. Describe the image "
    "even if it is a cover graphic, a logo lockup or a decorative illustration."
)


def _hash(document: Any, xref: int) -> str | None:
    try:
        return hashlib.md5(document.extract_image(xref)["image"]).hexdigest()
    except Exception:
        # A broken or unsupported image should not sink the whole document.
        return None


# Words that open a figure or table caption. Matched as whole words against the
# start of a line, never as a pattern inside one.
CAPTION_LEADS = ("figure", "fig.", "table", "chart", "exhibit")


def _looks_like_caption(line: str) -> bool:
    """Is this line a figure caption rather than a mention of one?

    A caption names its figure and then stops: "Figure 3: Entity-relationship
    Diagram ...". Body text refers to one and keeps going: "Figure 4 shows an
    Entity-Relationship Diagram that ...". The difference is the separator
    straight after the number, and it is what keeps page 18's sentence about
    Figure 4 from being mistaken for page 19's actual caption.
    """
    parts = " ".join(line.split()).split()
    if len(parts) < 2 or parts[0].lower().rstrip(":.") not in CAPTION_LEADS:
        return False
    label = parts[1]
    if not label.endswith((":", ".")):
        return False
    stem = label.rstrip(":.").replace("-", "").replace(".", "")
    # "Figure 3", "Figure C-1", "Table B.2" — a number, possibly appendix-prefixed.
    return bool(stem) and (stem[0].isdigit()
                           or (len(stem) > 1 and stem[0].isalpha() and stem[1:].isdigit()))


def _caption_pages(document: Any) -> dict[int, str]:
    """Pages whose text carries a figure or table caption."""
    found: dict[int, str] = {}
    for index in range(document.page_count):
        try:
            text = document[index].get_text()
        except Exception:
            continue
        # A contents listing repeats every caption, so its lines look exactly
        # like captions. Judge the PAGE, not the line: extraction often puts a
        # title and its dot leaders on separate lines, so a per-line test let
        # the "List of Figures" page through as if it held Figure 3 itself.
        lines = text.splitlines()
        leaders = sum(1 for line in lines if "...." in line or ". . . ." in line)
        lowered = text.lower()
        listing = ("list of figures" in lowered or "list of tables" in lowered
                   or "table of contents" in lowered)
        # Same rule the summarizer uses to find contents pages. A leader count
        # alone is not enough: the guide's "List of Figures" page carries only
        # 6 leader lines and was being read as though it held Figure 3 itself.
        if leaders >= 8 or (listing and leaders >= 3):
            continue
        for line in lines:
            if "...." in line or ". . . ." in line:
                continue
            if _looks_like_caption(line):
                found[index + 1] = " ".join(line.split())[:120]
                break
    return found


def _vector_bbox(page: Any) -> list[float] | None:
    """Extent of a page's vector drawing, for figures that are not images."""
    try:
        rects = [d["rect"] for d in page.get_drawings() if d.get("rect")]
    except Exception:
        return None
    if len(rects) < 4:
        return None
    x0 = min(r.x0 for r in rects); y0 = min(r.y0 for r in rects)
    x1 = max(r.x1 for r in rects); y1 = max(r.y1 for r in rects)
    if x1 <= x0 or y1 <= y0:
        return None
    return [round(x0, 2), round(y0, 2), round(x1, 2), round(y1, 2)]


def figure_pages(path: str) -> list[dict[str, Any]]:
    """Pages carrying a real figure, with the area it occupies.

    Two detectors, unioned, because each alone has a blind spot measured on the
    INCOSE guide:

      * embedded images miss vector diagrams entirely — Figure 4 on page 19 is
        drawn, not placed, so the page holds only the header logo;
      * captions miss figures whose caption is itself part of the artwork —
        page 11's Figure 2 is found by its images, not its text.

    Vector density alone is not a third detector: thresholds that catch page 19
    also catch 65-80 of the document's 115 pages, because its rule callout
    boxes are drawn with the same primitives as its diagrams.

    Returns [{page, count, coverage, bbox, source}], bbox being the region to
    highlight, or None to mark the whole page.
    """
    try:
        import pymupdf
        document = pymupdf.open(path)
    except Exception:
        return []

    try:
        placements: collections.Counter = collections.Counter()
        per_page: dict[int, list[tuple[str, int]]] = collections.defaultdict(list)
        for index in range(document.page_count):
            for image in document[index].get_images(full=True):
                digest = _hash(document, image[0])
                if digest is None:
                    continue
                placements[digest] += 1
                per_page[index + 1].append((digest, image[0]))

        furniture = {d for d, n in placements.items() if n >= BOILERPLATE_MIN_PAGES}

        by_page: dict[int, dict[str, Any]] = {}
        for number, entries in sorted(per_page.items()):
            page = document[number - 1]
            page_area = page.rect.width * page.rect.height or 1.0
            covered, count = 0.0, 0
            x0 = y0 = float("inf")
            x1 = y1 = float("-inf")
            for digest, xref in entries:
                if digest in furniture:
                    continue
                count += 1
                for rect in page.get_image_rects(xref) or []:
                    covered += (rect.width * rect.height) / page_area
                    x0, y0 = min(x0, rect.x0), min(y0, rect.y0)
                    x1, y1 = max(x1, rect.x1), max(y1, rect.y1)
            if count and covered >= MIN_PAGE_COVERAGE and x1 > x0:
                by_page[number] = {
                    "page": number, "count": count, "coverage": round(covered, 3),
                    "bbox": [round(x0, 2), round(y0, 2), round(x1, 2), round(y1, 2)],
                    "source": "image", "caption": None,
                }

        for number, caption in _caption_pages(document).items():
            existing = by_page.get(number)
            if existing:
                existing["source"] = "image+caption"
                existing["caption"] = caption
                continue
            # Caption but no image: a vector diagram. Highlight the drawn
            # region if there is one, otherwise let the viewer mark the page.
            by_page[number] = {
                "page": number, "count": 1, "coverage": 0.0,
                "bbox": _vector_bbox(document[number - 1]),
                "source": "caption", "caption": caption,
            }

        return [by_page[n] for n in sorted(by_page)]
    finally:
        try:
            document.close()
        except Exception:
            pass


def render_page(path: str, page_number: int) -> str | None:
    """One page as a base64 PNG data URI, ready for the vision endpoint."""
    try:
        import pymupdf
        document = pymupdf.open(path)
    except Exception:
        return None
    try:
        pixmap = document[page_number - 1].get_pixmap(
            matrix=pymupdf.Matrix(RENDER_SCALE, RENDER_SCALE))
        encoded = base64.b64encode(pixmap.tobytes("png")).decode()
        return f"data:image/png;base64,{encoded}"
    except Exception:
        return None
    finally:
        try:
            document.close()
        except Exception:
            pass


async def describe_page(data_uri: str, model: str | None = None,
                        insist: bool = False) -> str:
    """Ask the local model what a page's figures show.

    No max_tokens ceiling. A cap here is the same blind truncation as slicing an
    input: it cuts the description mid-sentence and the tail — usually the part
    naming the diagram's relationships — is silently lost.

    `enable_thinking` is off deliberately. With it on the model spends its reply
    in `reasoning_content` and returns an EMPTY `content`, which reads exactly
    like a server that cannot see images at all.
    """
    name = model or await _vision_model()
    if not name:
        return ""
    body = {
        "model": name,
        "temperature": 0.2,
        "chat_template_kwargs": {"enable_thinking": False},
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": INSTRUCTION_INSIST if insist else INSTRUCTION},
            {"type": "image_url", "image_url": {"url": data_uri}},
        ]}],
    }
    base = providers.LOCAL_BASE.rstrip("/")
    async with httpx.AsyncClient(timeout=httpx.Timeout(600.0)) as http:
        response = await http.post(f"{base}/chat/completions", json=body)
        response.raise_for_status()
        payload = response.json()
    choice = (payload.get("choices") or [{}])[0]
    text = ((choice.get("message") or {}).get("content") or "").strip()
    if text.upper().startswith("NO FIGURE"):
        return ""
    return text


async def _vision_model() -> str | None:
    """First chat model the router serves, embedding models excluded."""
    try:
        discovered = await providers.discover_local_models()
    except Exception:
        return None
    for entry in discovered:
        name = entry.get("id") or entry.get("name") or ""
        if name and not any(x in name.lower() for x in ("embed", "rerank", "bge")):
            return name
    return None


async def vision_available() -> bool:
    """Probe rather than assume.

    A text-only server answers an image request with 200 and prose about
    nothing, so the capability cannot be inferred from the model name or from
    the call succeeding. This sends a real image with a checkable answer.
    """
    name = await _vision_model()
    if not name:
        return False
    # A 2x2 red PNG; a model that sees it can name the colour.
    red = ("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAYAAABytg0kA"
           "AAAFUlEQVR42mP8z8BQz0AEYBxVSF+FABJADveWkH6oAAAAAElFTkSuQmCC")
    body = {"model": name, "temperature": 0, "max_tokens": 16,
            "chat_template_kwargs": {"enable_thinking": False},
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": "What colour is this image? Answer with one word."},
                {"type": "image_url", "image_url": {"url": red}}]}]}
    try:
        base = providers.LOCAL_BASE.rstrip("/")
        async with httpx.AsyncClient(timeout=httpx.Timeout(120.0)) as http:
            response = await http.post(f"{base}/chat/completions", json=body)
            response.raise_for_status()
            reply = ((response.json().get("choices") or [{}])[0]
                     .get("message", {}).get("content") or "")
    except Exception:
        return False
    return "red" in reply.lower()


async def describe_document(path: str, provider: str = "local",
                            on_progress=None) -> dict[str, Any]:
    """Describe every figure-bearing page. Returns records ready to index.

    Pages are described concurrently, sized to the slots the model server will
    actually serve (llama.cpp --parallel), the same way the summarizer indexes
    chapters. Running them one at a time left every slot but one idle.
    """
    started = time.monotonic()
    pages = figure_pages(path)
    if not pages:
        return {"pages": 0, "described": 0, "elapsed": 0.0, "records": []}

    model = await _vision_model()
    slots = CONCURRENCY or await providers.slots_for(provider)
    limit = asyncio.Semaphore(max(1, slots))
    done = 0

    async def describe_one(entry: dict[str, Any]) -> dict[str, Any] | None:
        nonlocal done
        # Rendering is CPU-bound and local, so it stays outside the semaphore;
        # only the model call is gated, since that is what the slots limit.
        data_uri = render_page(path, entry["page"])
        text = ""
        if data_uri:
            async with limit:
                try:
                    text = await describe_page(data_uri, model)
                    if not text:
                        # Refused, though a figure was detected on this page.
                        text = await describe_page(data_uri, model, insist=True)
                except Exception:
                    # One unreadable page must not lose the descriptions the
                    # other pages produced; the phase reports how many landed.
                    text = ""
        done += 1
        if on_progress:
            on_progress(done, len(pages))
        if not text:
            return None
        return {"page": entry["page"], "bbox": entry["bbox"],
                "text": text, "coverage": entry["coverage"]}

    gathered = await asyncio.gather(*(describe_one(e) for e in pages))
    # gather preserves argument order, but a failed page yields None.
    records = [r for r in gathered if r]
    return {"pages": len(pages), "described": len(records),
            "elapsed": round(time.monotonic() - started, 1), "records": records}
