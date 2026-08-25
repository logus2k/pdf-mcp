# pdf-mcp Explorer — chat over a PDF.
#
# One image holds both halves: the explorer (FastAPI) and pdf-mcp itself, which
# the app spawns as a child process over stdio. They keep separate dependency
# trees — pdf-mcp in its own virtualenv, the app in system site-packages —
# mirroring the two-venv layout used on the host, so a version bump on one
# cannot drag the other with it.

FROM python:3.12-slim

# tesseract: pdf-mcp's OCR path for scanned pages (pdf_read_pages ocr=true).
# Without it those pages silently return no text.
# libgl/libglib: PyMuPDF's rendering needs them for page rasterisation.
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        libgl1 \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# pdf-mcp in an isolated venv, spawned by the app over stdio.
ENV PDF_MCP_VENV=/opt/pdf-mcp-venv
RUN python -m venv $PDF_MCP_VENV \
    && $PDF_MCP_VENV/bin/pip install --no-cache-dir --upgrade pip \
    && $PDF_MCP_VENV/bin/pip install --no-cache-dir "pdf-mcp==2.2.0"

WORKDIR /app

# Requirements first so image layers cache across code edits.
COPY explorer/requirements.txt /app/explorer/requirements.txt
RUN pip install --no-cache-dir -r /app/explorer/requirements.txt

COPY explorer/ /app/explorer/

# Where pdf-mcp looks for its allow-list. The path is hardcoded in pdf_mcp's
# config module (~/.config/pdf-mcp/config.toml) with no env override, so the
# file has to land exactly here.
COPY docker/pdf-mcp-config.toml /root/.config/pdf-mcp/config.toml

ENV EXPLORER_HOST=0.0.0.0 \
    EXPLORER_PORT=8090 \
    EXPLORER_PDF_MCP_BIN=/opt/pdf-mcp-venv/bin/pdf-mcp \
    PYTHONUNBUFFERED=1

# Documents live here and the allow-list points at this path; uploads write to
# it, so it is a read-write mount rather than baked into the image.
VOLUME ["/documents"]

EXPOSE 8090

# The app's own health endpoint reports the pdf-mcp child's liveness too, so a
# dead MCP subprocess marks the container unhealthy rather than silently
# serving a UI whose every tool call fails.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import json,urllib.request,sys; \
d=json.load(urllib.request.urlopen('http://127.0.0.1:8090/api/health',timeout=4)); \
sys.exit(0 if d.get('status')=='ok' else 1)"

CMD ["python", "/app/explorer/app.py"]
