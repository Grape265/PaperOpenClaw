"""Turning a PDF into model input.

Requirement FR-3.1 is explicit: send the PDF to the model and let it answer.
Two transports exist because provider support differs:

* **file** - the whole PDF as a base64 ``file`` content part. This is the
  literal reading of the requirement, and it is what OpenAI, DashScope
  (qwen-vl) and Moonshot accept. It preserves layout, vector text and tables.
* **image** - pages rendered locally with PyMuPDF to PNG image parts. Works
  with any vision model, at the cost of bytes and page-count limits.

``auto`` prefers ``file`` and falls back to ``image`` on a provider error, so a
user who configures an older vision endpoint still gets a working pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from ..core.logging import get_logger
from .base import Attachment

logger = get_logger(__name__)

PDF_MIME = "application/pdf"

# Rendering above this many pages in one request usually exceeds context
# windows, so the caller splits into batches instead.
DEFAULT_MAX_PAGES_PER_REQUEST = 20


@dataclass
class PdfSlice:
    """A page range of one PDF, ready to attach to a message."""

    page_from: int          # 1-based inclusive
    page_to: int            # 1-based inclusive
    attachments: List[Attachment]
    mode: str               # "file" | "image"


def build_file_attachment(pdf_bytes: bytes, filename: str = "document.pdf") -> Attachment:
    """Whole-PDF attachment (FR-3.1)."""
    return Attachment(kind="file", data=pdf_bytes, mime=PDF_MIME, filename=filename)


def render_pages(
    pdf_bytes: bytes,
    page_from: int = 1,
    page_to: int = 0,
    dpi: int = 150,
    max_edge: int = 2200,
) -> List[Attachment]:
    """Render a page range to PNG attachments using PyMuPDF.

    Pages are downscaled when a rendered page would exceed ``max_edge`` pixels
    on its long side: vision models re-encode to a fixed budget anyway, so
    sending more pixels only costs bandwidth.
    """
    try:
        import fitz  # PyMuPDF
    except Exception as exc:  # pragma: no cover - dependency guard
        raise RuntimeError("PyMuPDF 未安装，无法渲染 PDF 页面") from exc

    attachments: List[Attachment] = []
    document = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        total = document.page_count
        first = max(1, int(page_from or 1))
        last = int(page_to or total)
        last = min(max(last, first), total)
        zoom = max(0.2, float(dpi) / 72.0)
        matrix = fitz.Matrix(zoom, zoom)

        for index in range(first - 1, last):
            page = document.load_page(index)
            pixmap = page.get_pixmap(matrix=matrix, alpha=False)
            width, height = pixmap.width, pixmap.height
            longest = max(width, height)
            if max_edge and longest > max_edge:
                scale = max_edge / float(longest)
                pixmap = page.get_pixmap(
                    matrix=fitz.Matrix(zoom * scale, zoom * scale), alpha=False
                )
            attachments.append(
                Attachment(kind="image", data=pixmap.tobytes("png"), mime="image/png",
                           page=index + 1)
            )
    finally:
        document.close()
    return attachments


def pdf_page_count(pdf_bytes: bytes) -> int:
    try:
        import fitz

        document = fitz.open(stream=pdf_bytes, filetype="pdf")
        try:
            return int(document.page_count)
        finally:
            document.close()
    except Exception:  # pragma: no cover - corrupt PDF
        return 0


def plan_slices(
    pdf_bytes: bytes,
    mode: str = "auto",
    batch_pages: int = DEFAULT_MAX_PAGES_PER_REQUEST,
    dpi: int = 150,
    max_pages: int = 400,
    filename: str = "document.pdf",
) -> List[PdfSlice]:
    """Split a PDF into request-sized slices.

    ``mode='file'`` yields a single whole-document slice (providers that accept
    a PDF natively handle paging themselves); ``mode='image'`` and ``auto``
    batch rendered pages so a long paper does not blow the context window.
    """
    total = pdf_page_count(pdf_bytes)
    if total <= 0:
        # Unreadable page count: still try the native file transport.
        return [PdfSlice(1, 0, [build_file_attachment(pdf_bytes, filename)], "file")]

    capped = min(total, max(1, int(max_pages or total)))
    if mode == "file":
        return [PdfSlice(1, capped, [build_file_attachment(pdf_bytes, filename)], "file")]

    batch = max(1, int(batch_pages or DEFAULT_MAX_PAGES_PER_REQUEST))
    slices: List[PdfSlice] = []
    start = 1
    while start <= capped:
        end = min(start + batch - 1, capped)
        attachments = render_pages(pdf_bytes, start, end, dpi=dpi)
        if not attachments:
            break
        slices.append(PdfSlice(start, end, attachments, "image"))
        start = end + 1
    if not slices:  # pragma: no cover - rendering produced nothing
        return [PdfSlice(1, capped, [build_file_attachment(pdf_bytes, filename)], "file")]
    return slices


def slice_labels(slices: Sequence[PdfSlice]) -> List[str]:
    return [f"{item.page_from}-{item.page_to}" if item.page_to else "all" for item in slices]
