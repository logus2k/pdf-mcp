/**
 * DocumentViewer — thin CV wrapper over docbro's PdfRenderer +
 * LayoutManager + PdfControls trio.
 *
 * The three docbro files (pdf-renderer.js, layout-manager.js,
 * pdf-controls.js) are copied verbatim from docbro/script/, only the
 * pdf.js import path was rewritten to CV's vendor location. All CV-
 * specific behaviour — citation-highlight overlays, click-to-dismiss,
 * the `bboxhighlights-cleared` custom event contract that cv-chat.js
 * listens for — lives here.
 */

import { getDocument, GlobalWorkerOptions } from './vendor/pdfjs/pdf.min.mjs';
import { PdfRenderer } from './pdf-renderer.js';
import { LayoutManager } from './layout-manager.js';
import { PdfControls } from './pdf-controls.js';

GlobalWorkerOptions.workerSrc =
    new URL('./vendor/pdfjs/pdf.worker.min.mjs', import.meta.url).toString();

// Static markup for the PdfControls floating control box. PdfControls
// binds by DOM id (#pdfControlBox + #pcb*), so this HTML has to land
// in the document before PdfControls.attach() runs. Rather than push
// it into index.html (build.py) we own it here so the wrapper stays
// self-contained.
const CONTROL_BOX_HTML = `
<div class="pdf-controlbox" id="pdfControlBox" style="display:none" aria-label="PDF controls">
    <button class="pcb-btn" id="pcbPrev" title="Previous page" aria-label="Previous page">&#8249;</button>
    <span class="pcb-page">
        <input id="pcbPageInput" class="pcb-page-input" type="text" inputmode="numeric" value="1" aria-label="Current page">
        <span class="pcb-page-sep">/</span>
        <span id="pcbPageTotal" class="pcb-page-total">0</span>
    </span>
    <button class="pcb-btn" id="pcbNext" title="Next page" aria-label="Next page">&#8250;</button>
    <span class="pcb-sep"></span>
    <button class="pcb-btn" id="pcbZoomOut" title="Zoom out" aria-label="Zoom out">&#8722;</button>
    <input class="pcb-zoom pcb-zoom-input" id="pcbZoomValue" type="text" value="100%" inputmode="numeric" aria-label="Zoom" title="Zoom (type a %, Enter)">
    <button class="pcb-btn" id="pcbZoomIn" title="Zoom in" aria-label="Zoom in">+</button>
    <button class="pcb-btn pcb-glyph" id="pcbFitWidth" title="Fit width" aria-label="Fit width">&#8596;</button>
    <button class="pcb-btn pcb-glyph" id="pcbFitPage" title="Fit page" aria-label="Fit page">&#10530;</button>
    <span class="pcb-sep"></span>
    <span class="pcb-cols-icon" title="Pages per row">&#9638;</span>
    <input type="range" id="pcbColumns" class="pcb-cols" min="1" max="16" step="1" value="1" title="Pages per row" aria-label="Pages per row">
    <span class="pcb-cols-value" id="pcbColumnsValue">1</span>
    <span class="pcb-sep"></span>
    <button class="pcb-btn pcb-glyph" id="pcbRotate" title="Rotate" aria-label="Rotate pages">&#10227;</button>
</div>
`;

export class DocumentViewer {

    constructor() {
        // Docbro's DOM contract, re-created inside our own wrapper:
        //   .document-viewer-wrapper                <- .element, host mounts this
        //     .content-container.content-pane      <- docbro's contentContainer
        //       .document-content.active.pdf-doc   <- created per-document by show()
        //         .document-content-inner.pdf-content  <- the actual page tiles + scroll
        //       #pdfControlBox                     <- floating control box (docbro markup)
        this._wrapper = document.createElement('div');
        this._wrapper.className = 'document-viewer-wrapper';

        this._contentContainer = document.createElement('div');
        this._contentContainer.className = 'content-container content-pane';
        this._wrapper.appendChild(this._contentContainer);

        // Inject the control box markup so PdfControls' id-lookups resolve.
        this._contentContainer.insertAdjacentHTML('beforeend', CONTROL_BOX_HTML);

        this._contentDiv = null;   // .document-content.pdf-doc (per document)
        this._innerDiv = null;     // .document-content-inner.pdf-content
        this._pdfDoc = null;

        // Docbro trio — created lazily on first show() so the wrapper
        // is already attached to the DOM (PdfControls uses
        // document.getElementById).
        this._pdfRenderer = null;
        this._layoutManager = null;
        this._pdfControls = null;
    }

    /** Force one page (and its neighbours) to rasterise now.
     *
     *  Jumping to a citation must not land on a blank placeholder: lazy
     *  rendering only fires when the IntersectionObserver reports the page
     *  visible, which can lag the scroll or never fire at all. A highlight
     *  painted over an unrendered page looks like no highlight at all. */
    primePage(pageNo, radius = 1) {
        const renderer = this._pdfRenderer;
        if (!renderer || !this._pdfDoc || !this._innerDiv) return 0;
        const pageDivs = renderer.pdfPageDivs || [];
        let queued = 0;
        for (let n = pageNo - radius; n <= pageNo + radius; n++) {
            const pageDiv = pageDivs[n - 1];
            if (!pageDiv) continue;
            if (pageDiv._renderState === 'rendered' || pageDiv._renderState === 'rendering') continue;
            renderer._enqueueRender(pageDiv);
            queued++;
        }
        if (queued) {
            renderer._processRenderQueue(this._pdfDoc, this._innerDiv, renderer._renderVersion);
        }
        return queued;
    }

    /** Re-rasterise all rendered pages (used after a crispness change). */
    rerenderAll() {
        return this._pdfRenderer?.rerenderAll?.() ?? 0;
    }

    /** Host element to mount into the DOM. */
    get element() { return this._wrapper; }

    /** Load a PDF at `url` and show it. `kind` is accepted for
     *  backwards compatibility with the previous viewer's signature
     *  but PDF is the only supported document type here. */
    async show({ url } = {}) {
        // Wait one frame so the wrapper is guaranteed connected —
        // PdfControls looks up its DOM by document.getElementById.
        if (!this._wrapper.isConnected) {
            await new Promise(r => requestAnimationFrame(r));
        }

        // Tear down any previous document + renderer state.
        if (this._contentDiv) {
            this._pdfRenderer?.cleanup();
            this._contentDiv.remove();
            this._contentDiv = null;
            this._innerDiv = null;
        }

        this._contentDiv = document.createElement('div');
        this._contentDiv.className = 'document-content active pdf-doc';
        this._innerDiv = document.createElement('div');
        this._innerDiv.className = 'document-content-inner pdf-content';
        this._contentDiv.appendChild(this._innerDiv);
        // Insert BEFORE the control box so it stacks on top via z-index.
        this._contentContainer.insertBefore(
            this._contentDiv, this._contentContainer.firstChild);

        // First-run wiring of docbro's trio.
        if (!this._pdfRenderer) {
            this._pdfRenderer = new PdfRenderer({
                contentContainer: this._contentContainer,
                selectionMode: null,   // CV doesn't use selection mode
            });
            this._layoutManager = new LayoutManager({
                contentContainer: this._contentContainer,
                getPdfPageDivs: () => this._pdfRenderer.pdfPageDivs,
                onZoomApplied: () => this._pdfRenderer.refreshResolution(),
                onStateChanged: () => this._pdfControls?.syncReadouts(),
            });
            this._pdfControls = new PdfControls({
                contentPane: this._wrapper,
                layoutManager: this._layoutManager,
                pdfRenderer: this._pdfRenderer,
                getScrollContainer: () => this._innerDiv,
            });
            this._pdfControls.attach();
        }

        try {
            this._pdfDoc = await getDocument({ url }).promise;
            await this._pdfRenderer.setupPlaceholders(this._pdfDoc, this._innerDiv);
            this._layoutManager.initForDocument();
            this._pdfRenderer.startLazyRendering(this._pdfDoc, this._innerDiv);
            this._pdfControls.showForDocument(true);
            // Deferred on purpose: priming synchronously here rasterises
            // pages before the browser has laid the container out, and the
            // layout manager's fit calculation then measures an unsettled
            // container — collapsing every page to a ~40px speck in a
            // 16-column grid.
            //
            // Two schedulers race, first-one-wins: rAF is the correct signal
            // (fires once layout is done), but it never fires in some
            // headless/embedded configurations, so a timer backs it up.
            // primeFirstPages is idempotent and re-checks the layout itself.
            this._schedulePrime();
        } catch (err) {
            this._innerDiv.innerHTML =
                `<div class="document-viewer-error">Failed to load PDF: ${err.message}</div>`;
        }
    }

    /** Render the first few pages directly, without waiting for the
     *  IntersectionObserver to report them visible.
     *
     *  Lazy rendering is driven entirely by that observer. When it does not
     *  fire — it is silent in some headless/embedded browser configurations,
     *  and can be starved on a very long document whose scroll container is
     *  hundreds of thousands of pixels tall — every page stays 'idle' and the
     *  pane shows nothing but empty placeholders. Priming the top of the
     *  document guarantees the reader always sees page 1, and costs one render
     *  that lazy loading would have done anyway. */
    _schedulePrime() {
        if (this._primeScheduled) return;
        this._primeScheduled = true;
        let done = false;
        const run = () => {
            if (done) return;
            done = true;
            this._primeScheduled = false;
            this.primeFirstPages();
        };
        requestAnimationFrame(() => requestAnimationFrame(run));
        setTimeout(run, 300);
    }

    primeFirstPages(count = 3) {
        const renderer = this._pdfRenderer;
        if (!renderer || !this._pdfDoc || !this._innerDiv) return;
        const pageDivs = renderer.pdfPageDivs;
        if (!pageDivs || !pageDivs.length) return;

        // Refuse to prime against a container that has not been laid out.
        // Rendering into an unsettled layout is what caused the collapse this
        // guard exists to prevent, so skip rather than risk it.
        if (this._innerDiv.clientHeight < 100 || pageDivs[0].clientWidth < 100) {
            return 0;
        }

        let queued = 0;
        for (const pageDiv of pageDivs.slice(0, count)) {
            if (pageDiv._renderState === 'rendered' || pageDiv._renderState === 'rendering') continue;
            renderer._enqueueRender(pageDiv);
            queued++;
        }
        if (queued) {
            renderer._processRenderQueue(
                this._pdfDoc, this._innerDiv, renderer._renderVersion);
        }
        return queued;
    }

    // ── Citation-highlight API ─────────────────────────────────────
    // cv-chat.js click-through calls showBboxHighlights(regions);
    // clicking the same badge (or a painted rect) calls
    // clearBboxHighlights, which fires a `bboxhighlights-cleared`
    // custom event on document that cv-chat listens for to reset its
    // toggle tracker. Kept API-compatible with the previous viewer.

    /** Scroll to `regions[0].page_no` and paint a rectangle per region
     *  on the pages they belong to. Multi-page chunks (page-break wrap)
     *  paint on every affected page. Clears any prior highlights first. */
    async showBboxHighlights(regions) {
        if (!Array.isArray(regions) || regions.length === 0) return;
        const pageDivs = this._pdfRenderer?.pdfPageDivs;
        if (!pageDivs || !pageDivs.length) return;

        this._clearAllBboxHighlights();

        // Scroll first so the target page enters the viewport and lazy
        // rendering kicks in for it.
        const firstIdx = (regions[0].page_no || 1) - 1;
        const firstPage = pageDivs[firstIdx];
        const scrollHost = this._innerDiv;
        if (firstPage && scrollHost) {
            const hostRect = scrollHost.getBoundingClientRect();
            const targetRect = firstPage.getBoundingClientRect();
            scrollHost.scrollTop += targetRect.top - hostRect.top;
        }

        this.primePage(regions[0].page_no || 1);

        for (const r of regions) {
            if (Array.isArray(r.bbox) && r.bbox.length === 4) {
                this._paintBboxOnPage(r.page_no, r.bbox);
            } else {
                // pdf_search omits bbox for snippet-style matches (excerpts that
                // begin mid-block) and for cross-page hits. Previously these
                // produced no visual feedback whatsoever. There is no position
                // to point at, so mark the whole page instead of pretending to
                // a precision we do not have.
                this._paintWholePage(r.page_no);
            }
        }
    }

    /** Mark an entire page, for a citation with no positional information. */
    _paintWholePage(pageNo) {
        const pageDivs = this._pdfRenderer?.pdfPageDivs;
        const pageDiv = pageDivs && pageDivs[pageNo - 1];
        if (!pageDiv) return;
        const layer = document.createElement('div');
        layer.className = 'pdf-bbox-highlight-layer';
        layer.style.cssText = 'width:100%;height:100%;transform:none;';
        const rect = document.createElement('div');
        rect.className = 'pdf-bbox-highlight page-level';
        rect.style.cssText = 'left:0;top:0;width:100%;height:100%;';
        rect.addEventListener('click', (ev) => {
            ev.stopPropagation();
            this.clearBboxHighlights();
        });
        layer.appendChild(rect);
        pageDiv.appendChild(layer);
    }

    /** Dismiss every highlight currently painted anywhere in the
     *  document; fires `bboxhighlights-cleared` on document so
     *  cv-chat.js resets its "active citation" state. Public. */
    clearBboxHighlights() {
        this._clearAllBboxHighlights();
        document.dispatchEvent(new CustomEvent('bboxhighlights-cleared'));
    }

    _clearAllBboxHighlights() {
        const pageDivs = this._pdfRenderer?.pdfPageDivs;
        if (!pageDivs) return;
        for (const pd of pageDivs) {
            const olds = pd.querySelectorAll('.pdf-bbox-highlight-layer');
            for (const old of olds) {
                if (old._bboxResizeObserver) old._bboxResizeObserver.disconnect();
                old.remove();
            }
        }
    }

    /** Paint one bbox rect on the given page. Runs whether or not the
     *  page is currently rendered — the highlight layer positions
     *  against the pdf.js viewport (natural page units) and CSS-scales
     *  to fit the pageDiv's displayed size, matched via ResizeObserver
     *  so zoom / column changes keep it aligned. */
    _paintBboxOnPage(pageNo, bbox) {
        if (!pageNo || pageNo < 1 || !Array.isArray(bbox) || bbox.length !== 4) return;
        const pageDivs = this._pdfRenderer?.pdfPageDivs;
        if (!pageDivs) return;
        const pageDiv = pageDivs[pageNo - 1];
        if (!pageDiv || !pageDiv._pdfPage) return;

        const viewport = pageDiv._pdfViewport
            || pageDiv._pdfPage.getViewport({ scale: 1 });

        let left, top, width, height;
        try {
            // pdf-mcp reports bboxes in PyMuPDF convention: origin TOP-left,
            // y increasing downward. Verified against pdf-mcp's own `clip`
            // field, which is documented top-left and equals bbox/page_rect
            // exactly. convertToViewportRectangle expects PDF user space —
            // origin BOTTOM-left, y increasing upward — so handing it the
            // bbox unchanged mirrored every highlight vertically: a citation
            // 12% down the page painted 87% down it.
            //
            // Flip about the page's top edge first, then let pdf.js apply
            // scale and rotation as it normally would. view[3] is used rather
            // than the height so pages with a non-zero origin (cropbox
            // offsets) convert correctly too.
            const view = pageDiv._pdfPage.view || [0, 0, viewport.width, viewport.height];
            const pageTopPdfY = view[3];
            const [bx0, by0, bx1, by1] = bbox;
            const pdfRect = [bx0, pageTopPdfY - by1, bx1, pageTopPdfY - by0];
            const [vx1, vy1, vx2, vy2] = viewport.convertToViewportRectangle(pdfRect);
            left = Math.min(vx1, vx2);
            top = Math.min(vy1, vy2);
            width = Math.abs(vx2 - vx1);
            height = Math.abs(vy2 - vy1);
        } catch {
            return;
        }

        const layer = document.createElement('div');
        layer.className = 'pdf-bbox-highlight-layer';
        layer.style.width = viewport.width + 'px';
        layer.style.height = viewport.height + 'px';
        layer.style.transformOrigin = '0 0';

        const rect = document.createElement('div');
        rect.className = 'pdf-bbox-highlight';
        rect.style.left = left + 'px';
        rect.style.top = top + 'px';
        rect.style.width = width + 'px';
        rect.style.height = height + 'px';
        // Click any painted rect → dismiss every highlight, matching
        // the citation-badge second-click behaviour in cv-chat.js.
        rect.addEventListener('click', (ev) => {
            ev.stopPropagation();
            this.clearBboxHighlights();
        });
        layer.appendChild(rect);
        pageDiv.appendChild(layer);

        const applyScale = () => {
            const dw = pageDiv.clientWidth;
            if (dw > 0) layer.style.transform = `scale(${dw / viewport.width})`;
        };
        applyScale();
        if (typeof ResizeObserver !== 'undefined') {
            const ro = new ResizeObserver(applyScale);
            ro.observe(pageDiv);
            layer._bboxResizeObserver = ro;
        }
    }
}
