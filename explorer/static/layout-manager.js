export class LayoutManager {

    constructor({ contentContainer, getPdfPageDivs, onZoomApplied, onStateChanged }) {
        this.contentContainer = contentContainer;
        this.getPdfPageDivs = getPdfPageDivs;
        this.onZoomApplied = onZoomApplied;
        this.onStateChanged = onStateChanged;

        // PDF view state. `columns` = pages per row (1..MAX_COLUMNS). Page
        // sizing is computed in pixels (pageWidthPx) and pushed to CSS; `pdfZoom`
        // is a derived readout where 1 == fit-width. `fitMode` drives how the
        // size recomputes on resize / rotation.
        this.MAX_COLUMNS = 16;
        this.columns = 1;
        this.pdfZoom = 1;
        this.pageWidthPx = 0;
        this.fitMode = 'page'; // 'width' | 'page' | 'custom'
        this.GAP = 6;          // px gap between pages in a row (matches CSS grid gap)
        this.EDGE_RESERVE = 28; // px reserved beside the pages row for the ~10px
                                // gap + the scrollbar (container is narrowed to fit)

        this._layoutResizeObserver = null;
        this._zoomRefreshTimer = null;
        this._splitInstance = null;
        this._tocPixelWidth = null;
        this._rightPanePixelWidth = null;
    }

    initSplitPane() {
        const tocPane = document.getElementById('tocPane');
        const rightPane = document.getElementById('rightPane');

        const tocPct = 16;
        const contentPct = 100 - tocPct;

        this._splitInstance = Split(['#tocPane', '#contentPane', '#rightPane'], {
            sizes: [tocPct, contentPct, 0],
            minSize: [5, 5, 0],
            gutterSize: 6,
            cursor: 'col-resize',
            onDragEnd: () => {
                this._tocPixelWidth = tocPane.getBoundingClientRect().width;
                this._rightPanePixelWidth = rightPane.getBoundingClientRect().width;
            }
        });
        this._tocPixelWidth = tocPane.getBoundingClientRect().width;
        this._rightPanePixelWidth = rightPane.getBoundingClientRect().width;

        window.addEventListener('resize', () => {
            if (!this._splitInstance) return;
            const container = tocPane.parentElement;
            const containerWidth = container.getBoundingClientRect().width;
            if (containerWidth <= 0) return;
            const tocPct = (this._tocPixelWidth / containerWidth) * 100;
            const rightPct = (this._rightPanePixelWidth / containerWidth) * 100;
            const clampedToc = Math.min(Math.max(tocPct, 1), 90);
            const clampedRight = Math.min(Math.max(rightPct, 1), 90);
            const contentPct = 100 - clampedToc - clampedRight;
            this._splitInstance.setSizes([clampedToc, Math.max(contentPct, 1), clampedRight]);
        });
    }

    // --- PDF layout (columns + zoom) ---
    //
    // Page sizing is computed in JS (px) and pushed to CSS via --pdf-page-width;
    // applyColumns sets --pdf-grid-cols to repeat(N, max-content). A centred CSS
    // grid then shows exactly N pages per row, adjacent, with spare width on the
    // outer edges — regardless of how tall/short the pages are.

    _pdfContent() {
        return this.contentContainer.querySelector('.pdf-content');
    }

    applyColumns() {
        const pdfContent = this._pdfContent();
        if (!pdfContent) return;
        pdfContent.classList.add('pdf-cols');
        // N fixed content-sized columns => exactly N pages per row; the grid is
        // centred so spare width sits on the outer edges, not between pages.
        pdfContent.style.setProperty('--pdf-grid-cols', `repeat(${this.columns}, max-content)`);
    }

    _notify() {
        if (this.onStateChanged) this.onStateChanged();
    }

    // Available content box + first page aspect (width / height) + per-column slot.
    _metrics() {
        const pdfContent = this._pdfContent();
        if (!pdfContent) return null;
        const style = getComputedStyle(pdfContent);
        const padY = (parseFloat(style.paddingTop) || 0) + (parseFloat(style.paddingBottom) || 0);
        // Size pages against the STABLE pane width (the parent), not the scroll
        // container's own width — the container gets narrowed to fit the pages
        // (so the scrollbar hugs the page), which would otherwise feed back.
        const parent = pdfContent.parentElement;
        const paneW = parent ? parent.clientWidth : pdfContent.clientWidth;
        const availW = Math.max(1, paneW - this.EDGE_RESERVE);
        const availH = Math.max(1, pdfContent.clientHeight - padY);

        let aspect = 900 / 1165; // width / height fallback
        const firstDiv = this.getPdfPageDivs()[0];
        if (firstDiv && firstDiv._pdfViewport) {
            aspect = firstDiv._pdfViewport.width / firstDiv._pdfViewport.height;
        }
        // Per-column width: the available width split into N columns with a
        // GAP between them, minus 1px slack so rounding never overflows.
        const slot = Math.max(20, Math.floor((availW - (this.columns - 1) * this.GAP) / this.columns) - 1);
        return { availW, availH, aspect, slot };
    }

    // Push a concrete page width (px) to CSS. The grid (N max-content columns,
    // centred) keeps exactly N pages per row, adjacent, with the spare width on
    // the outer edges — so no per-page margin is needed here.
    _applyPageWidth(pageWidth, m) {
        const pdfContent = this._pdfContent();
        if (!pdfContent) return;
        const pw = Math.max(20, Math.floor(pageWidth));
        this.pageWidthPx = pw;
        // Readout: 100% == fit-width (page fills its column).
        this.pdfZoom = pw / Math.max(1, m.slot);

        pdfContent.style.setProperty('--pdf-page-width', pw + 'px');
        // Also expose a unitless `--pdf-zoom` derived from the page width
        // so the CV chrome + Diana launcher (whose CSS uses
        // `calc(Npx * var(--pdf-zoom, 1))`) scale with the page. 900 is
        // the base page width used elsewhere as the "1× zoom" reference
        // in cv-document-viewer.css. Without this the launcher stays
        // at its base 56 px img regardless of page size, so on smaller
        // page tiles the badge becomes disproportionately huge.
        pdfContent.style.setProperty('--pdf-zoom', (pw / 900).toString());
        // Narrow the scroll container to the pages row + a small gutter so the
        // vertical scrollbar sits ~10px from the page's right border.
        const rowWidth = this.columns * pw + (this.columns - 1) * this.GAP;
        pdfContent.style.setProperty('--pdf-content-maxw', (rowWidth + this.EDGE_RESERVE) + 'px');

        if (this.onZoomApplied) {
            clearTimeout(this._zoomRefreshTimer);
            this._zoomRefreshTimer = setTimeout(() => this.onZoomApplied(), 150);
        }
        this._notify();
    }

    _fitWidthPx(m) {
        return m.slot;
    }

    _fitPagePx(m) {
        // Largest page that fits both its column width and the viewport height.
        return Math.max(20, Math.min(m.slot, Math.floor(m.availH * m.aspect)));
    }

    // Called when a PDF is first shown. Default to a two-page spread when the
    // rendering area is wide enough to fit two full-height pages side by side;
    // otherwise a single page. Either way, fit the whole page(s) in view.
    initForDocument() {
        const m = this._metrics();
        const twoUpFits = m && (2 * (m.availH * m.aspect) + this.GAP <= m.availW);
        this.columns = twoUpFits ? 2 : 1;
        this.applyColumns();
        this.fitPage();
        this._setupLayoutResizeObserver();
    }

    setColumns(n) {
        this.columns = Math.min(this.MAX_COLUMNS, Math.max(1, Math.round(n)));
        this.applyColumns();
        this.fitPage(); // each column count defaults to whole-page-visible
    }

    fitWidth() {
        const m = this._metrics();
        if (!m) return;
        this.fitMode = 'width';
        this._applyPageWidth(this._fitWidthPx(m), m);
    }

    fitPage() {
        const m = this._metrics();
        if (!m) return;
        this.fitMode = 'page';
        this._applyPageWidth(this._fitPagePx(m), m);
    }

    zoomBy(factor) {
        const m = this._metrics();
        if (!m) return;
        this.fitMode = 'custom';
        const base = this.pageWidthPx || this._fitPagePx(m);
        this._applyPageWidth(Math.max(20, base * factor), m);
    }

    // Best column count (pages per row) that still shows every page at
    // its full available height. Same heuristic as initForDocument's
    // twoUpFits, generalised to N: N × (H × aspect) + (N-1) × GAP ≤ W.
    // Floors at 1, capped by MAX_COLUMNS.
    _autoColumns(m) {
        const perColW = m.availH * m.aspect;
        const n = Math.floor((m.availW + this.GAP) / (perColW + this.GAP));
        return Math.max(1, Math.min(this.MAX_COLUMNS, n));
    }

    // Re-fit after an external geometry change (rotation, panel-splitter
    // drag, window resize) using the current mode. In 'width' and 'page'
    // fit modes we ALSO recompute the column count from available space
    // so the CV pane's page count tracks the panel resize live. In
    // 'custom' (user zoomed manually) we leave columns alone so the
    // user's manual choice is preserved.
    refit() {
        const m = this._metrics();
        if (!m) return;
        if (this.fitMode !== 'custom') {
            const cols = this._autoColumns(m);
            if (cols !== this.columns) {
                this.columns = cols;
                this.applyColumns();
            }
        }
        if (this.fitMode === 'width') this._applyPageWidth(this._fitWidthPx(m), m);
        else if (this.fitMode === 'custom') this._applyPageWidth(this.pageWidthPx || this._fitPagePx(m), m);
        else this._applyPageWidth(this._fitPagePx(m), m);
    }

    _setupLayoutResizeObserver() {
        if (this._layoutResizeObserver) {
            this._layoutResizeObserver.disconnect();
            this._layoutResizeObserver = null;
        }
        const pdfContent = this._pdfContent();
        if (!pdfContent) return;

        // Observe the PARENT (the pane containing .pdf-content) rather
        // than .pdf-content itself. .pdf-content's width is clamped by
        // --pdf-content-maxw (which _applyPageWidth sets), so as the
        // pane grows/shrinks .pdf-content stays at that clamp and its
        // ResizeObserver never fires — the CV chat-splitter drag would
        // silently miss the resize. The pane's clientWidth is what
        // _metrics actually reads, so observing the pane catches every
        // resize that could change the fit.
        const target = pdfContent.parentElement || pdfContent;
        this._layoutResizeObserver = new ResizeObserver(() => this.refit());
        this._layoutResizeObserver.observe(target);
    }

    disconnectLayoutObserver() {
        if (this._layoutResizeObserver) {
            this._layoutResizeObserver.disconnect();
            this._layoutResizeObserver = null;
        }
    }
}
