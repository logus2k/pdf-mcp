// pdf-controls.js — floating PDF control box (navigation, zoom, columns, rotate)
//
// Drives the static #pdfControlBox markup. Shows on mouse activity over the
// content pane and auto-hides ~2s after movement stops. Talks to LayoutManager
// (columns + zoom) and PdfRenderer (rotation + page navigation).

export class PdfControls {

    constructor({ contentPane, layoutManager, pdfRenderer, getScrollContainer }) {
        this.contentPane = contentPane;
        this.layoutManager = layoutManager;
        this.pdfRenderer = pdfRenderer;
        this.getScrollContainer = getScrollContainer;

        this.box = document.getElementById('pdfControlBox');
        this.el = {
            prev: document.getElementById('pcbPrev'),
            next: document.getElementById('pcbNext'),
            pageInput: document.getElementById('pcbPageInput'),
            pageTotal: document.getElementById('pcbPageTotal'),
            zoomOut: document.getElementById('pcbZoomOut'),
            zoomIn: document.getElementById('pcbZoomIn'),
            zoomValue: document.getElementById('pcbZoomValue'),
            fitWidth: document.getElementById('pcbFitWidth'),
            fitPage: document.getElementById('pcbFitPage'),
            columns: document.getElementById('pcbColumns'),
            columnsValue: document.getElementById('pcbColumnsValue'),
            rotate: document.getElementById('pcbRotate'),
        };

        this._hideTimer = null;
        this._hovering = false;
        this._pageInputFocused = false;
        this._zoomInputFocused = false;
        this._isPdf = false;
        this._scrollContainer = null;
        this._scrollHandler = null;
        this._scrollRaf = 0;
    }

    attach() {
        if (!this.box) return;

        // --- Auto-hide on activity ---
        this.contentPane.addEventListener('mousemove', () => {
            if (this._isPdf) this._show();
        });
        this.box.addEventListener('mouseenter', () => {
            this._hovering = true;
            clearTimeout(this._hideTimer);
        });
        this.box.addEventListener('mouseleave', () => {
            this._hovering = false;
            this._resetHideTimer();
        });

        // --- Page navigation ---
        this.el.prev?.addEventListener('click', () => this._goRelative(-1));
        this.el.next?.addEventListener('click', () => this._goRelative(1));
        this.el.pageInput?.addEventListener('focus', () => { this._pageInputFocused = true; });
        this.el.pageInput?.addEventListener('blur', () => { this._pageInputFocused = false; this._commitPageInput(); });
        this.el.pageInput?.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') { e.preventDefault(); this.el.pageInput.blur(); return; }
            // ArrowUp/Down = +1 / -1 page. Direction follows native
            // number-input semantics (Up increments the value, Down
            // decrements) rather than scroll semantics. Applied
            // immediately so the user can hammer arrows through the doc.
            if (e.key === 'ArrowUp' || e.key === 'ArrowDown') {
                e.preventDefault();
                this._goRelative(e.key === 'ArrowUp' ? 1 : -1);
                const cur = this.pdfRenderer.getCurrentPageIndex() + 1;
                this.el.pageInput.value = String(cur);
            }
        });

        // --- Zoom ---
        this.el.zoomOut?.addEventListener('click', () => this.layoutManager.zoomBy(1 / 1.1));
        this.el.zoomIn?.addEventListener('click', () => this.layoutManager.zoomBy(1.1));
        // Editable zoom readout — same pattern as pcbPageInput: focus
        // suppresses syncReadouts overwriting, blur commits the typed
        // percentage, Enter blurs. Accepts "125" or "125%".
        this.el.zoomValue?.addEventListener('focus', () => { this._zoomInputFocused = true; this.el.zoomValue.select?.(); });
        this.el.zoomValue?.addEventListener('blur', () => { this._zoomInputFocused = false; this._commitZoomInput(); });
        this.el.zoomValue?.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') { e.preventDefault(); this.el.zoomValue.blur(); return; }
            // ArrowUp/Down = ±1 percentage point per keystroke. Compute
            // the multiplicative factor needed to move the current
            // rounded % by exactly ±1 so repeated presses give crisp,
            // predictable single-unit steps.
            if (e.key === 'ArrowUp' || e.key === 'ArrowDown') {
                e.preventDefault();
                const currentPct = Math.round((this.layoutManager.pdfZoom || 1) * 100);
                const targetPct = Math.max(10, Math.min(500,
                    currentPct + (e.key === 'ArrowUp' ? 1 : -1)));
                if (targetPct !== currentPct) {
                    this.layoutManager.zoomBy(targetPct / currentPct);
                }
                this.el.zoomValue.value = targetPct + '%';
            }
        });
        this.el.fitWidth?.addEventListener('click', () => this.layoutManager.fitWidth());
        // Fit page: after resizing, snap the current spread's row to the top so
        // you see complete pages rather than a partial row + a slice of the next.
        this.el.fitPage?.addEventListener('click', () => {
            const top = this.pdfRenderer.getTopPageIndex();
            this.layoutManager.fitPage();
            requestAnimationFrame(() => this.pdfRenderer.scrollToPage(top));
        });

        // --- Columns (pages per row) ---
        this.el.columns?.addEventListener('input', (e) => {
            const n = parseInt(e.target.value, 10) || 1;
            if (this.el.columnsValue) this.el.columnsValue.textContent = String(n);
            this.layoutManager.setColumns(n);
        });

        // --- Rotate ---
        this.el.rotate?.addEventListener('click', () => {
            this.pdfRenderer.setRotation(this.pdfRenderer.rotation + 90);
            this.layoutManager.refit();
            this.syncReadouts();
        });
    }

    // Show/reset the box for the active document (PDF) or hide it (non-PDF).
    showForDocument(isPdf) {
        this._isPdf = isPdf;
        if (!this.box) return;

        this._detachScroll();

        if (!isPdf) {
            this.box.classList.remove('visible');
            this.box.style.display = 'none';
            return;
        }

        this.box.style.display = '';
        this._attachScroll();
        this.syncReadouts();
        this._show();
    }

    // Refresh all readouts from the managers (zoom %, columns, page X / N).
    syncReadouts() {
        if (!this.box || !this._isPdf) return;

        if (this.el.zoomValue && !this._zoomInputFocused) {
            // Editable <input> in CV's copy; use .value not .textContent so
            // typing survives readout refreshes and the browser doesn't have
            // to reflow the whole control box on every drag.
            const pct = Math.round(this.layoutManager.pdfZoom * 100) + '%';
            if ('value' in this.el.zoomValue) this.el.zoomValue.value = pct;
            else this.el.zoomValue.textContent = pct;
        }
        if (this.el.columns) {
            this.el.columns.value = String(this.layoutManager.columns);
        }
        if (this.el.columnsValue) {
            this.el.columnsValue.textContent = String(this.layoutManager.columns);
        }

        const total = this.pdfRenderer.pageCount || 0;
        if (this.el.pageTotal) this.el.pageTotal.textContent = String(total);
        if (this.el.pageInput && !this._pageInputFocused) {
            this.el.pageInput.value = String(this.pdfRenderer.getCurrentPageIndex() + 1);
        }
    }

    // --- internals ---

    _goRelative(direction) {
        // Row-based so it works for any column count (adjacent pages share a row).
        this.pdfRenderer.scrollByRow(direction);
    }

    _commitPageInput() {
        const total = this.pdfRenderer.pageCount || 0;
        let n = parseInt(this.el.pageInput.value, 10);
        if (isNaN(n)) n = this.pdfRenderer.getCurrentPageIndex() + 1;
        n = Math.min(Math.max(n, 1), Math.max(total, 1));
        this.el.pageInput.value = String(n);
        this.pdfRenderer.scrollToPage(n - 1);
    }

    // Parse the typed % and set zoom absolutely. Accepts "125", "125%",
    // or a bare fraction "1.25". LayoutManager only exposes zoomBy
    // (multiplicative) — bridge to that by computing target / current.
    // Clamps to [10%, 500%] so a stray keystroke doesn't crash the
    // renderer with an absurd viewport.
    _commitZoomInput() {
        const raw = String(this.el.zoomValue.value || '').trim().replace('%', '');
        let n = parseFloat(raw);
        if (!isFinite(n) || n <= 0) { this.syncReadouts(); return; }
        // Accept fractional (0.5) as a shortcut for 50%.
        if (n < 5) n *= 100;
        n = Math.min(Math.max(n, 10), 500);
        const target = n / 100;
        const current = this.layoutManager.pdfZoom || 1;
        if (Math.abs(target - current) > 0.001) {
            this.layoutManager.zoomBy(target / current);
        }
        this.syncReadouts();
    }

    _attachScroll() {
        const sc = this.getScrollContainer();
        if (!sc) return;
        this._scrollContainer = sc;
        this._scrollHandler = () => {
            if (this._scrollRaf) return;
            this._scrollRaf = requestAnimationFrame(() => {
                this._scrollRaf = 0;
                if (this.el.pageInput && !this._pageInputFocused) {
                    this.el.pageInput.value = String(this.pdfRenderer.getCurrentPageIndex() + 1);
                }
            });
        };
        sc.addEventListener('scroll', this._scrollHandler, { passive: true });
    }

    _detachScroll() {
        if (this._scrollContainer && this._scrollHandler) {
            this._scrollContainer.removeEventListener('scroll', this._scrollHandler);
        }
        if (this._scrollRaf) { cancelAnimationFrame(this._scrollRaf); this._scrollRaf = 0; }
        this._scrollContainer = null;
        this._scrollHandler = null;
    }

    _show() {
        this.box.classList.add('visible');
        this._resetHideTimer();
    }

    _resetHideTimer() {
        clearTimeout(this._hideTimer);
        if (this._hovering) return;
        this._hideTimer = setTimeout(() => this.box.classList.remove('visible'), 2000);
    }
}
