import { TextLayer } from './vendor/pdfjs/pdf.min.mjs';

export class PdfRenderer {

    constructor({ contentContainer, selectionMode }) {
        this.contentContainer = contentContainer;
        this.selectionMode = selectionMode;
        this._pdfDoc = null;
        this._pdfContainer = null;
        this._pdfPageDivs = [];
        this._pdfOverlayEntries = [];
        this._pdfResizeObserver = null;
        this._renderVersion = 0;
        this._rotation = 0;

        // Lazy rendering state
        this._intersectionObserver = null;
        this._unloadObserver = null;
        this._renderQueue = [];
        this._activeRenders = 0;
        this._maxConcurrentRenders = 2;
        this._intersectingIndices = new Set();

        this._dprRefreshTimer = null;
        this._watchDevicePixelRatio();
    }

    // Re-render currently rendered pages when devicePixelRatio changes (browser
    // zoom / moving the window between monitors) so canvases match the new
    // pixel density instead of being up/down-scaled and blurry.
    _watchDevicePixelRatio() {
        if (typeof window === 'undefined' || !window.matchMedia) return;
        const arm = () => {
            const dpr = window.devicePixelRatio || 1;
            const mq = window.matchMedia(`(resolution: ${dpr}dppx)`);
            const onChange = () => {
                clearTimeout(this._dprRefreshTimer);
                this._dprRefreshTimer = setTimeout(() => this._rerenderRenderedPages(), 200);
                arm(); // re-arm for the new ratio (the query is value-specific)
            };
            mq.addEventListener('change', onChange, { once: true });
        };
        arm();
    }

    // Force a re-raster of every currently-rendered page (DPR change).
    _rerenderRenderedPages() {
        if (!this._pdfDoc || !this._pdfContainer) return;
        const renderVersion = this._renderVersion;
        let queued = false;
        for (const pageDiv of this._pdfPageDivs) {
            if (pageDiv._renderState === 'rendered') {
                pageDiv._renderState = 'idle';
                pageDiv._renderedCssWidth = 0;
                this._enqueueRender(pageDiv);
                queued = true;
            }
        }
        if (queued) {
            this._processRenderQueue(this._pdfDoc, this._pdfContainer, renderVersion);
        }
    }

    get pdfPageDivs() {
        return this._pdfPageDivs;
    }

    incrementRenderVersion() {
        this._renderVersion++;
        return this._renderVersion;
    }

    get renderVersion() {
        return this._renderVersion;
    }

    async setupPlaceholders(pdfDoc, container) {
        const numPages = pdfDoc.numPages;
        const scale = 1.5;
        const renderVersion = this._renderVersion;

        this.cleanup();
        this._pdfDoc = pdfDoc;
        this._pdfContainer = container;

        const pages = await Promise.all(
            Array.from({ length: numPages }, (_, i) =>
                pdfDoc.getPage(i + 1).then(
                    page => this._renderVersion === renderVersion ? page : null,
                    e => {
                        if (this._renderVersion === renderVersion) {
                            console.warn(`Failed to get page ${i + 1}:`, e);
                        }
                        return null;
                    }
                )
            )
        );

        // A newer render started while we were fetching pages — discard results
        if (this._renderVersion !== renderVersion) return;

        const pageDivs = [];
        for (let i = 0; i < pages.length; i++) {
            const page = pages[i];
            const pageDiv = document.createElement('div');
            pageDiv.className = 'pdf-page';

            if (page) {
                const viewport = page.getViewport({ scale, rotation: this._rotation });
                pageDiv.style.aspectRatio = `${viewport.width} / ${viewport.height}`;
                pageDiv._pdfPage = page;
                pageDiv._pdfViewport = viewport;
            } else {
                pageDiv.style.aspectRatio = '8.5 / 11';
            }

            // Lazy rendering state per page
            pageDiv._renderState = 'idle';
            pageDiv._pageRenderVersion = 0;
            pageDiv._pageIndex = i;

            container.appendChild(pageDiv);
            pageDivs.push(pageDiv);
        }

        this._pdfPageDivs = pageDivs;

        if (this._pdfResizeObserver) this._pdfResizeObserver.disconnect();
        this._pdfResizeObserver = new ResizeObserver(() => {
            for (const entry of this._pdfOverlayEntries) {
                const pd = entry.div.parentElement;
                if (pd) {
                    const dw = pd.clientWidth;
                    if (dw > 0) {
                        entry.div.style.transform = `scale(${dw / entry.viewport.width})`;
                    }
                }
            }
        });
        this._pdfResizeObserver.observe(container);
    }

    // --- Lazy rendering system ---

    startLazyRendering(pdfDoc, container) {
        const renderVersion = this._renderVersion;
        const pageDivs = this._pdfPageDivs;

        this._disconnectObservers();

        // Render observer: trigger rendering when pages approach viewport
        this._intersectionObserver = new IntersectionObserver((entries) => {
            if (this._renderVersion !== renderVersion) return;

            for (const entry of entries) {
                const pageDiv = entry.target;
                if (entry.isIntersecting) {
                    this._intersectingIndices.add(pageDiv._pageIndex);
                    if (pageDiv._renderState === 'idle' || pageDiv._renderState === 'unloaded') {
                        this._enqueueRender(pageDiv);
                    }
                } else {
                    this._intersectingIndices.delete(pageDiv._pageIndex);
                }
            }

            this._processRenderQueue(pdfDoc, container, renderVersion);
        }, {
            root: container,
            rootMargin: '200% 0px'
        });

        // Unload observer: reclaim memory when pages are far from viewport
        this._unloadObserver = new IntersectionObserver((entries) => {
            if (this._renderVersion !== renderVersion) return;

            for (const entry of entries) {
                if (!entry.isIntersecting && entry.target._renderState === 'rendered') {
                    this._unloadPage(entry.target);
                }
            }
        }, {
            root: container,
            rootMargin: '500% 0px'
        });

        for (const pageDiv of pageDivs) {
            this._intersectionObserver.observe(pageDiv);
            this._unloadObserver.observe(pageDiv);
        }
    }

    _enqueueRender(pageDiv) {
        if (pageDiv._renderState === 'rendering') return;
        if (this._renderQueue.includes(pageDiv)) return;
        this._renderQueue.push(pageDiv);
    }

    _processRenderQueue(pdfDoc, container, renderVersion) {
        while (this._activeRenders < this._maxConcurrentRenders && this._renderQueue.length > 0) {
            if (this._renderVersion !== renderVersion) return;

            // Sort: pages closest to current scroll center first
            const scrollCenter = container.scrollTop + container.clientHeight / 2;
            this._renderQueue.sort((a, b) => {
                const aDist = Math.abs(a.offsetTop + a.offsetHeight / 2 - scrollCenter);
                const bDist = Math.abs(b.offsetTop + b.offsetHeight / 2 - scrollCenter);
                return aDist - bDist;
            });

            const pageDiv = this._renderQueue.shift();

            // Skip if no longer eligible
            if (pageDiv._renderState === 'rendered' || pageDiv._renderState === 'rendering') continue;
            if (!pageDiv._pdfPage) continue;

            this._activeRenders++;
            pageDiv._renderState = 'rendering';

            this._renderSinglePage(pageDiv, pdfDoc, container, renderVersion).then(() => {
                this._activeRenders--;
                this._processRenderQueue(pdfDoc, container, renderVersion);
            });
        }
    }

    async _renderSinglePage(pageDiv, pdfDoc, container, renderVersion) {
        const page = pageDiv._pdfPage;
        const viewport = pageDiv._pdfViewport;
        const pageRenderVersion = ++pageDiv._pageRenderVersion;
        const pageDivs = this._pdfPageDivs;

        if (!page || !viewport) {
            pageDiv._renderState = 'idle';
            return;
        }

        // --- Canvas render (kept in DOM, no JPEG conversion) ---
        // Rasterize at the page's true on-screen width and full device-pixel
        // density (devicePixelRatio) so text stays sharp at any zoom level or
        // display DPI, instead of CSS-upscaling a fixed-resolution bitmap.
        const { viewport: renderViewport, cssWidth } = this._computeRenderViewport(page, pageDiv);

        const canvas = document.createElement('canvas');
        canvas.width = Math.floor(renderViewport.width);
        canvas.height = Math.floor(renderViewport.height);
        const ctx = canvas.getContext('2d');

        try {
            await page.render({
                canvasContext: ctx,
                viewport: renderViewport
            }).promise;
        } catch (e) {
            console.warn(`Failed to render page ${pageDiv._pageIndex + 1}:`, e);
            pageDiv._renderState = pageDiv._renderState === 'rendering' ? 'idle' : pageDiv._renderState;
            return;
        }

        // Staleness checks
        if (this._renderVersion !== renderVersion) return;
        if (pageDiv._pageRenderVersion !== pageRenderVersion) return;
        if (pageDiv._renderState !== 'rendering') return;

        // Swap in the fresh canvas, discarding any previous render (a zoom
        // refresh re-rasterizes an already-rendered page in place).
        this._clearPageContent(pageDiv);
        pageDiv.appendChild(canvas);
        // Fix the box aspect-ratio to the canvas's exact backing-store ratio so
        // the page height is deterministic (never collapses inside a CSS grid
        // row in some browsers) AND the canvas is displayed with no
        // non-proportional stretch (which would soften the image).
        pageDiv.style.aspectRatio = `${canvas.width} / ${canvas.height}`;
        pageDiv._renderedCssWidth = cssWidth;

        // --- Text layer ---
        try {
            const textContent = await page.getTextContent();
            if (this._renderVersion !== renderVersion || pageDiv._pageRenderVersion !== pageRenderVersion) return;

            const displayedWidth = pageDiv.clientWidth || viewport.width;
            const textScale = displayedWidth / page.getViewport({ scale: 1, rotation: this._rotation }).width;
            const textViewport = page.getViewport({ scale: textScale, rotation: this._rotation });

            const textLayerDiv = document.createElement('div');
            textLayerDiv.className = 'textLayer';
            textLayerDiv.style.setProperty('--scale-factor', textScale);
            pageDiv.appendChild(textLayerDiv);

            const textLayer = new TextLayer({
                textContentSource: textContent,
                container: textLayerDiv,
                viewport: textViewport
            });
            await textLayer.render();

            if (this._renderVersion !== renderVersion || pageDiv._pageRenderVersion !== pageRenderVersion) return;

            if (this.selectionMode) {
                this.selectionMode.registerPage(pageDiv, textContent, textScale, textViewport);
            }
        } catch (e) {
            console.warn(`Failed to render text layer for page ${pageDiv._pageIndex + 1}:`, e);
        }

        // --- Context menu (attach once) ---
        if (!pageDiv._hasContextMenu) {
            pageDiv.addEventListener('contextmenu', (e) => {
                e.preventDefault();
                this._showPageContextMenu(e.clientX, e.clientY, pageDiv);
            });
            pageDiv._hasContextMenu = true;
        }

        // --- Annotation overlay (links) ---
        try {
            const annotations = await page.getAnnotations();
            if (this._renderVersion !== renderVersion || pageDiv._pageRenderVersion !== pageRenderVersion) return;

            const linkAnnotations = annotations.filter(a => a.subtype === 'Link' && (a.dest || a.url));
            if (linkAnnotations.length > 0) {
                const annotationDiv = document.createElement('div');
                annotationDiv.className = 'annotationLayer';
                annotationDiv.style.width = viewport.width + 'px';
                annotationDiv.style.height = viewport.height + 'px';
                pageDiv.appendChild(annotationDiv);

                for (const annot of linkAnnotations) {
                    const [x1, y1, x2, y2] = viewport.convertToViewportRectangle(annot.rect);
                    const left = Math.min(x1, x2);
                    const top = Math.min(y1, y2);
                    const width = Math.abs(x2 - x1);
                    const height = Math.abs(y2 - y1);

                    const link = document.createElement('a');
                    link.style.position = 'absolute';
                    link.style.left = left + 'px';
                    link.style.top = top + 'px';
                    link.style.width = width + 'px';
                    link.style.height = height + 'px';

                    if (annot.url) {
                        link.href = annot.url;
                        link.target = '_blank';
                        link.rel = 'noopener noreferrer';
                    } else if (annot.dest) {
                        link.href = '#';
                        link.addEventListener('click', async (e) => {
                            e.preventDefault();
                            try {
                                let dest = annot.dest;
                                if (typeof dest === 'string') {
                                    dest = await pdfDoc.getDestination(dest);
                                }
                                if (!Array.isArray(dest)) return;
                                const ref = dest[0];
                                const pageIndex = typeof ref === 'number' ? ref : await pdfDoc.getPageIndex(ref);
                                const targetDiv = pageDivs[pageIndex];
                                if (targetDiv) {
                                    const containerRect = container.getBoundingClientRect();
                                    const targetRect = targetDiv.getBoundingClientRect();
                                    const offset = targetRect.top - containerRect.top + container.scrollTop;
                                    container.scrollTo({ top: offset, behavior: 'smooth' });
                                }
                            } catch (err) {
                                console.error('PDF link navigation error:', err);
                            }
                        });
                    }

                    annotationDiv.appendChild(link);
                }

                this._pdfOverlayEntries.push({ div: annotationDiv, viewport });
            }
        } catch (e) {
            console.warn(`Failed to process annotations for page ${pageDiv._pageIndex + 1}:`, e);
        }

        pageDiv._renderState = 'rendered';
    }

    // Build a render viewport matched to the page's current on-screen size at
    // full device-pixel density, capped to a sane maximum canvas area so that
    // extreme zoom levels can't allocate runaway amounts of GPU memory.
    _computeRenderViewport(page, pageDiv) {
        const dpr = window.devicePixelRatio || 1;
        const rotation = this._rotation;
        const baseWidth = page.getViewport({ scale: 1, rotation }).width;
        const fallbackWidth = pageDiv._pdfViewport ? pageDiv._pdfViewport.width : baseWidth;
        const cssWidth = pageDiv.clientWidth || fallbackWidth;

        // Rasterize ABOVE device resolution (super-sampling) so the browser's
        // downscale sharpens text/line edges — pdf.js draws with grayscale AA,
        // and oversampling closes much of the gap to a native PDF viewer.
        // 2.5× flat across DPRs — was 1.8/1.5 (DPR<2 / DPR>=2); pushed up to
        // reduce the residual "veil" on standard displays. Memory ceiling
        // (MAX_CANVAS_PIXELS below) clamps runaway cases.
        const SUPERSAMPLE = 2.5;
        let scale = (cssWidth / baseWidth) * dpr * SUPERSAMPLE;

        const MAX_CANVAS_PIXELS = 24_000_000; // memory guard (~96 MB/canvas)
        let vp = page.getViewport({ scale, rotation });
        if (vp.width * vp.height > MAX_CANVAS_PIXELS) {
            scale *= Math.sqrt(MAX_CANVAS_PIXELS / (vp.width * vp.height));
            vp = page.getViewport({ scale, rotation });
        }
        return { viewport: vp, cssWidth };
    }

    // Tear down a page's rendered DOM (canvas, text, annotations) and release
    // its selection registration. Shared by unload and in-place re-render.
    _clearPageContent(pageDiv) {
        const canvas = pageDiv.querySelector('canvas');
        if (canvas) {
            canvas.width = 0;
            canvas.height = 0;
            canvas.remove();
        }

        const textLayer = pageDiv.querySelector('.textLayer');
        if (textLayer) textLayer.remove();

        const annotLayer = pageDiv.querySelector('.annotationLayer');
        if (annotLayer) {
            this._pdfOverlayEntries = this._pdfOverlayEntries.filter(e => e.div !== annotLayer);
            annotLayer.remove();
        }

        if (this.selectionMode) {
            this.selectionMode.unregisterPage(pageDiv);
        }
    }

    _unloadPage(pageDiv) {
        if (pageDiv._renderState !== 'rendered') return;

        // Cancel any lingering async work for this page
        pageDiv._pageRenderVersion++;

        this._clearPageContent(pageDiv);

        // Restore aspect-ratio placeholder
        if (pageDiv._pdfViewport) {
            const vp = pageDiv._pdfViewport;
            pageDiv.style.aspectRatio = `${vp.width} / ${vp.height}`;
        }

        pageDiv._renderState = 'unloaded';
    }

    // Re-rasterize already-rendered pages whose on-screen size has grown (e.g.
    // after a zoom-in), keeping them pixel-sharp rather than CSS-upscaled.
    // Shrinking needs no re-render — downscaling a hi-res canvas stays crisp.
    refreshResolution() {
        if (!this._pdfDoc || !this._pdfContainer) return;
        const renderVersion = this._renderVersion;
        let queued = false;
        for (const pageDiv of this._pdfPageDivs) {
            if (pageDiv._renderState !== 'rendered') continue;
            const cssWidth = pageDiv.clientWidth;
            if (!cssWidth) continue;
            const prev = pageDiv._renderedCssWidth || 0;
            if (prev > 0 && cssWidth <= prev * 1.02) continue;
            pageDiv._renderState = 'idle';
            this._enqueueRender(pageDiv);
            queued = true;
        }
        if (queued) {
            this._processRenderQueue(this._pdfDoc, this._pdfContainer, renderVersion);
        }
    }

    get rotation() {
        return this._rotation;
    }

    get pageCount() {
        return this._pdfPageDivs.length;
    }

    // Rotate every page by `deg` (absolute, normalized to 0/90/180/270),
    // updating placeholder geometry and re-rasterizing rendered pages.
    setRotation(deg) {
        const norm = ((Math.round(deg / 90) * 90) % 360 + 360) % 360;
        if (norm === this._rotation) return;
        this._rotation = norm;

        const renderVersion = this._renderVersion;
        let queued = false;
        for (const pageDiv of this._pdfPageDivs) {
            const page = pageDiv._pdfPage;
            if (!page) continue;
            const vp = page.getViewport({ scale: 1.5, rotation: norm });
            pageDiv._pdfViewport = vp;
            pageDiv.style.aspectRatio = `${vp.width} / ${vp.height}`;
            if (pageDiv._renderState === 'rendered' || pageDiv._renderState === 'rendering') {
                pageDiv._renderState = 'idle';
                pageDiv._renderedCssWidth = 0;
                this._enqueueRender(pageDiv);
                queued = true;
            }
        }
        if (queued && this._pdfDoc && this._pdfContainer) {
            this._processRenderQueue(this._pdfDoc, this._pdfContainer, renderVersion);
        }
    }

    // Index (0-based) of the page whose box currently spans the vertical centre
    // of the scroll viewport; falls back to the nearest page.
    getCurrentPageIndex() {
        const container = this._pdfContainer;
        const pageDivs = this._pdfPageDivs;
        if (!container || pageDivs.length === 0) return 0;

        const containerRect = container.getBoundingClientRect();
        const centerY = containerRect.top + containerRect.height / 2;
        let best = 0;
        let bestDist = Infinity;
        for (let i = 0; i < pageDivs.length; i++) {
            const r = pageDivs[i].getBoundingClientRect();
            if (centerY >= r.top && centerY <= r.bottom) return i;
            const dist = Math.min(Math.abs(r.top - centerY), Math.abs(r.bottom - centerY));
            if (dist < bestDist) { bestDist = dist; best = i; }
        }
        return best;
    }

    // Index of the topmost page still visible at (or below) the viewport top —
    // i.e. the first page in the row currently anchored at the top.
    getTopPageIndex() {
        const container = this._pdfContainer;
        const pageDivs = this._pdfPageDivs;
        if (!container || pageDivs.length === 0) return 0;
        const containerTop = container.getBoundingClientRect().top;
        for (let i = 0; i < pageDivs.length; i++) {
            const r = pageDivs[i].getBoundingClientRect();
            if (r.bottom - containerTop > 6) return i; // first page not scrolled past
        }
        return pageDivs.length - 1;
    }

    // Scroll the page at `index` (0-based) to the top of the scroll viewport.
    scrollToPage(index) {
        const container = this._pdfContainer;
        const pageDivs = this._pdfPageDivs;
        if (!container || pageDivs.length === 0) return;
        const clamped = Math.min(Math.max(index, 0), pageDivs.length - 1);
        const target = pageDivs[clamped];
        if (!target) return;
        const offset = target.getBoundingClientRect().top
            - container.getBoundingClientRect().top + container.scrollTop;
        // Align the row's top exactly to the viewport top. A small negative
        // nudge here would push a full-height page's bottom border out of view.
        container.scrollTo({ top: Math.max(0, Math.round(offset)) });
    }

    // Move the viewport by one row of pages. Column-count agnostic: "next"
    // (direction > 0) aligns the first page that begins below the top edge;
    // "prev" aligns the nearest page that begins above it. The threshold must
    // exceed scrollToPage's small top-alignment gap, otherwise the row already
    // pinned to the top would be re-selected and the view wouldn't advance.
    scrollByRow(direction) {
        const container = this._pdfContainer;
        const pageDivs = this._pdfPageDivs;
        if (!container || pageDivs.length === 0) return;
        const containerTop = container.getBoundingClientRect().top;
        const SLACK = 12; // > scrollToPage's 4px gap, << a page's height

        if (direction > 0) {
            for (let i = 0; i < pageDivs.length; i++) {
                const top = pageDivs[i].getBoundingClientRect().top - containerTop;
                if (top > SLACK) { this.scrollToPage(i); return; }
            }
            container.scrollTo({ top: container.scrollHeight }); // already at last row
        } else {
            for (let i = pageDivs.length - 1; i >= 0; i--) {
                const top = pageDivs[i].getBoundingClientRect().top - containerTop;
                if (top < -SLACK) { this.scrollToPage(i); return; }
            }
            container.scrollTo({ top: 0 }); // already at first row
        }
    }

    _disconnectObservers() {
        if (this._intersectionObserver) {
            this._intersectionObserver.disconnect();
            this._intersectionObserver = null;
        }
        if (this._unloadObserver) {
            this._unloadObserver.disconnect();
            this._unloadObserver = null;
        }
        this._renderQueue = [];
        this._activeRenders = 0;
        this._intersectingIndices.clear();
    }

    // --- Context menu ---

    async _showPageContextMenu(x, y, pageDiv) {
        const page = pageDiv._pdfPage;
        const viewport = pageDiv._pdfViewport;
        if (!page || !viewport) return;

        // Reuse in-DOM canvas if the page is currently rendered
        const existingCanvas = pageDiv.querySelector('canvas');
        if (existingCanvas && pageDiv._renderState === 'rendered') {
            this._showContextMenu(x, y, existingCanvas);
            return;
        }

        // Otherwise render to a temporary canvas at display resolution
        const { viewport: renderViewport } = this._computeRenderViewport(page, pageDiv);
        const canvas = document.createElement('canvas');
        canvas.width = Math.floor(renderViewport.width);
        canvas.height = Math.floor(renderViewport.height);
        const ctx = canvas.getContext('2d');
        try {
            await page.render({
                canvasContext: ctx,
                viewport: renderViewport
            }).promise;
        } catch (e) {
            console.warn('Failed to re-render page for context menu:', e);
            return;
        }
        this._showContextMenu(x, y, canvas);
    }

    _showContextMenu(x, y, canvas) {
        document.querySelector('.pdf-context-menu')?.remove();

        const menu = document.createElement('div');
        menu.className = 'pdf-context-menu';
        menu.style.left = x + 'px';
        menu.style.top = y + 'px';

        const copyItem = document.createElement('div');
        copyItem.className = 'pdf-context-menu-item';
        copyItem.textContent = 'Copy page as image';
        copyItem.addEventListener('click', () => {
            menu.remove();
            canvas.toBlob(async (blob) => {
                try {
                    await navigator.clipboard.write([
                        new ClipboardItem({ 'image/png': blob })
                    ]);
                } catch (err) {
                    console.error('Copy failed:', err);
                }
            });
        });

        const saveItem = document.createElement('div');
        saveItem.className = 'pdf-context-menu-item';
        saveItem.textContent = 'Save page as image';
        saveItem.addEventListener('click', () => {
            menu.remove();
            const link = document.createElement('a');
            link.download = 'page.png';
            link.href = canvas.toDataURL('image/png');
            link.click();
        });

        menu.appendChild(copyItem);
        menu.appendChild(saveItem);
        document.body.appendChild(menu);

        const close = () => {
            menu.remove();
            document.removeEventListener('click', close);
            document.removeEventListener('keydown', onKey);
        };
        const onKey = (e) => { if (e.key === 'Escape') close(); };
        setTimeout(() => {
            document.addEventListener('click', close);
            document.addEventListener('keydown', onKey);
        }, 0);
    }

    // --- Cleanup ---

    cleanup() {
        this._disconnectObservers();

        if (this._pdfResizeObserver) {
            this._pdfResizeObserver.disconnect();
            this._pdfResizeObserver = null;
        }
        for (const pageDiv of this._pdfPageDivs) {
            // Revoke any remaining blob URLs (from pre-migration renders)
            const img = pageDiv.querySelector('img');
            if (img && img.src.startsWith('blob:')) {
                URL.revokeObjectURL(img.src);
            }
            // Release canvas GPU memory
            const canvas = pageDiv.querySelector('canvas');
            if (canvas) {
                canvas.width = 0;
                canvas.height = 0;
            }
        }
        this._pdfOverlayEntries = [];
        this._pdfPageDivs = [];
        this._pdfDoc = null;
        this._pdfContainer = null;
        this._rotation = 0;
    }
}
