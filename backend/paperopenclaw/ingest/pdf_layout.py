"""Local layout analysis: finding and cropping figures and tables (FR-4).

This stage is deliberately model-free. Geometry is a deterministic problem, and
asking a vision model for bounding boxes produces boxes that are *plausible*
rather than *correct*. So the division of labour is:

    PyMuPDF   decides WHERE a figure/table is and writes figN.png / tabN.png
    the model decides WHAT it means (caption text, description)

Detection combines three signals, because no single one is sufficient:

1. **embedded images** - raster figures;
2. **vector drawing clusters** - charts, and table rules drawn as lines/rects;
3. **caption anchors** ("Figure 1", "表 2", "TABLE I") - which also supply the
   kind and the ordering, and rescue tables that are pure text plus rules.

Numbering follows order of appearance per kind, which is what makes the filenames
match what a reader would call them (FR-4.2).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from ..core.logging import get_logger

logger = get_logger(__name__)

# Longest alternatives first: 附图/附表 must win over 图/表.
CAPTION_RE = re.compile(
    r"^\s*(?P<kind>附图|附表|figure|fig\.?|table|tab\.?|图|表)\s*"
    r"(?P<num>\d{1,3}|[IVXLC]{1,6})\s*(?:[:：.、\-\)]\s*)?(?P<rest>.{0,600})$",
    re.IGNORECASE | re.DOTALL,
)

KIND_BY_PREFIX = {
    "figure": "figure", "fig": "figure", "fig.": "figure", "图": "figure", "附图": "figure",
    "table": "table", "tab": "table", "tab.": "table", "表": "table", "附表": "table",
}

# Body prose that *references* a figure/table rather than captions it. A line like
# "Figure 1 shows our pipeline ..." is a sentence about the figure, not its caption,
# and must never spawn a crop. The check keys off these leading verbs: a real caption
# names the object ("Figure 1: Overview of ..."), it does not make a claim about it.
BODY_REFERENCE_VERBS = (
    "shows", "show", "illustrates", "illustrate", "depicts", "depict", "presents",
    "present", "describes", "describe", "summarizes", "summarises", "compares",
    "compare", "reports", "report", "lists", "list", "outlines", "outline",
    "highlights", "highlight", "provides", "provide", "gives", "give", "is", "are",
    "was", "were", "indicates", "indicate", "demonstrates", "demonstrate",
    "显示了", "展示", "给出", "表明", "列出", "描述", "说明", "概述", "呈现",
    "对比", "比较", "归纳", "总结了", "反映了", "说明了", "示意", "体现",
)

# A graphic must cover at least this fraction of the page to be considered a
# figure/table rather than a logo, rule or inline glyph cluster.
DEFAULT_MIN_AREA_RATIO = 0.012
# Vertical search window (in PDF points) when pairing a caption with a graphic.
DEFAULT_CAPTION_WINDOW = 90.0


@dataclass
class Caption:
    """A detected caption anchor."""

    kind: str            # "figure" | "table"
    number: str
    text: str
    page: int            # 0-based
    bbox: Tuple[float, float, float, float]
    block_index: int = 0

    @property
    def top(self) -> float:
        return self.bbox[1]

    @property
    def bottom(self) -> float:
        return self.bbox[3]

    @property
    def center_y(self) -> float:
        return (self.bbox[1] + self.bbox[3]) / 2.0


@dataclass
class GraphicCandidate:
    """A region that might be a figure or a table body."""

    page: int
    bbox: Tuple[float, float, float, float]
    source: str          # "image" | "drawing" | "text_block"
    caption: Optional[Caption] = None

    @property
    def area(self) -> float:
        width = max(0.0, self.bbox[2] - self.bbox[0])
        height = max(0.0, self.bbox[3] - self.bbox[1])
        return width * height


@dataclass
class CroppedAsset:
    """One cropped figure or table, ready to be persisted."""

    label: str
    kind: str
    ordinal: int
    page: int                      # 1-based page number
    bbox: Tuple[float, float, float, float]
    caption: str
    caption_source: str
    width: int
    height: int
    data: bytes = b""
    extension: str = "png"

    def to_row(self, relative_path: str) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "label": self.label,
            "ordinal": self.ordinal,
            "page": self.page,
            "bbox": list(self.bbox),
            "caption": self.caption,
            "caption_source": self.caption_source,
            "content_text": "",
            # The fishbone dimension is decided later, once sections are known.
            "section": "",
            "file_path": relative_path,
            "width": self.width,
            "height": self.height,
        }


@dataclass
class LayoutReport:
    """Everything the layout stage learned, including per-page text."""

    page_count: int = 0
    page_texts: List[str] = field(default_factory=list)
    captions: List[Caption] = field(default_factory=list)
    assets: List[CroppedAsset] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def detected_summary(self) -> List[Dict[str, Any]]:
        """Compact listing handed to the model so labels stay consistent."""
        return [
            {"label": item.label, "kind": item.kind, "page": item.page, "caption": item.caption[:200]}
            for item in self.assets
        ]

    def text_excerpt(self, max_chars: int = 6000) -> str:
        """Head of the document, used to help the model find section headings."""
        joined = "\n".join(
            f"--- 第 {index + 1} 页 ---\n{text}" for index, text in enumerate(self.page_texts)
        )
        return joined[:max_chars]


# ---------------------------------------------------------------------------
# geometry helpers
# ---------------------------------------------------------------------------

def _rect_tuple(rect: Any) -> Tuple[float, float, float, float]:
    """Normalise a PyMuPDF ``Rect`` or a plain ``(x0, y0, x1, y1)`` sequence.

    ``page.get_text("dict")`` reports block bounding boxes as plain tuples while
    ``get_drawings()`` reports ``Rect`` objects, so both forms reach this helper.
    """
    if hasattr(rect, "x0"):
        return (float(rect.x0), float(rect.y0), float(rect.x1), float(rect.y1))
    values = list(rect)
    if len(values) < 4:
        return (0.0, 0.0, 0.0, 0.0)
    return (float(values[0]), float(values[1]), float(values[2]), float(values[3]))


def _union(boxes: Sequence[Tuple[float, float, float, float]]) -> Tuple[float, float, float, float]:
    return (
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    )


def _overlap_ratio(a: Tuple[float, float, float, float],
                   b: Tuple[float, float, float, float]) -> float:
    """Intersection over the smaller box's area."""
    width = min(a[2], b[2]) - max(a[0], b[0])
    height = min(a[3], b[3]) - max(a[1], b[1])
    if width <= 0 or height <= 0:
        return 0.0
    intersection = width * height
    area_a = max(1e-6, (a[2] - a[0]) * (a[3] - a[1]))
    area_b = max(1e-6, (b[2] - b[0]) * (b[3] - b[1]))
    return intersection / min(area_a, area_b)


def _vertical_gap(a: Tuple[float, float, float, float],
                  b: Tuple[float, float, float, float]) -> float:
    """Distance between two boxes along y (0 when they overlap vertically)."""
    if a[3] < b[1]:
        return b[1] - a[3]
    if b[3] < a[1]:
        return a[1] - b[3]
    return 0.0


def merge_boxes(
    boxes: Sequence[Tuple[float, float, float, float]],
    threshold: float = 0.25,
    gap: float = 0.0,
) -> List[Tuple[float, float, float, float]]:
    """Merge overlapping (or nearly touching) boxes until stable.

    Vector-drawn charts emit one rectangle per bar or segment, so a chart arrives
    as dozens of boxes and merging is what turns them back into one figure.
    ``gap`` grows each box before the overlap test, which is what lets a chart's
    axis lines connect to its plot area; ``threshold`` controls how much two
    boxes must overlap to be considered the same object.
    """
    if gap > 0:
        inflated = [
            (box[0] - gap, box[1] - gap, box[2] + gap, box[3] + gap) for box in boxes
        ]
        # Cluster by expanded-rectangle intersection, then union the originals.
        clusters: List[List[int]] = []
        for index, box in enumerate(inflated):
            placed = False
            for cluster in clusters:
                if any(_rects_intersect(box, inflated[other]) for other in cluster):
                    cluster.append(index)
                    placed = True
                    break
            if not placed:
                clusters.append([index])
        # Union-find style second pass: a box may bridge two clusters.
        merged_any = True
        while merged_any:
            merged_any = False
            for first in range(len(clusters)):
                for second in range(first + 1, len(clusters)):
                    if any(_rects_intersect(inflated[a], inflated[b])
                           for a in clusters[first] for b in clusters[second]):
                        clusters[first].extend(clusters[second])
                        clusters.pop(second)
                        merged_any = True
                        break
                if merged_any:
                    break
        return [_union([boxes[index] for index in cluster]) for cluster in clusters]

    remaining = list(boxes)
    merged = True
    while merged:
        merged = False
        output: List[Tuple[float, float, float, float]] = []
        for box in remaining:
            target = None
            for index, existing in enumerate(output):
                if _overlap_ratio(box, existing) >= threshold:
                    target = index
                    break
            if target is None:
                output.append(box)
            else:
                output[target] = _union([output[target], box])
                merged = True
        remaining = output
    return remaining


def _rects_intersect(a: Tuple[float, float, float, float],
                     b: Tuple[float, float, float, float]) -> bool:
    return not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])


# ---------------------------------------------------------------------------
# detection
# ---------------------------------------------------------------------------

def parse_caption(text: str) -> Optional[Tuple[str, str, str]]:
    """Return ``(kind, number, caption_text)`` when ``text`` starts a caption.

    Two false-positive guards live here:

    * a caption must *name* the object, not make a claim about it — text that
      continues with a body verb ("Figure 1 shows …") is prose, not a caption;
    * the caller additionally requires the block to be short and standalone, so
      a "fig1" mention buried in a paragraph never reaches this point.
    """
    match = CAPTION_RE.match((text or "").strip())
    if not match:
        return None
    prefix = match.group("kind").strip().lower()
    kind = KIND_BY_PREFIX.get(prefix)
    if kind is None:
        return None
    number = match.group("num").strip().upper()
    rest = re.sub(r"\s+", " ", match.group("rest") or "").strip()
    if _starts_with_body_verb(rest):
        return None
    return kind, number, rest


def _starts_with_body_verb(text: str) -> bool:
    """True when ``text`` begins with a verb that reads as body prose."""
    lowered = text.lower()
    for verb in BODY_REFERENCE_VERBS:
        if lowered.startswith(verb):
            return True
    return False


def _page_captions(page: Any, page_index: int) -> List[Caption]:
    """Find caption anchors on one page from its text blocks.

    Only standalone, short blocks are considered: a genuine caption sits in its
    own block (usually one line, two when it wraps), while body text that merely
    mentions "fig1" lives inside a multi-line paragraph block. Capping the block
    at two lines is the single most effective way to stop body references from
    being treated as captions.
    """
    captions: List[Caption] = []
    try:
        blocks = page.get_text("dict").get("blocks", [])
    except Exception:  # pragma: no cover - malformed page
        return captions

    for block_index, block in enumerate(blocks):
        if block.get("type") != 0:
            continue
        lines = block.get("lines") or []
        if not lines:
            continue
        if len(lines) > 2:
            # A multi-line paragraph, not a standalone caption.
            continue
        first_line = "".join(span.get("text", "") for span in lines[0].get("spans", []))
        parsed = parse_caption(first_line)
        if not parsed:
            continue
        kind, number, _rest = parsed
        full_text = " ".join(
            "".join(span.get("text", "") for span in line.get("spans", []))
            for line in lines
        )
        full_text = re.sub(r"\s+", " ", full_text).strip()
        captions.append(Caption(
            kind=kind,
            number=number,
            text=full_text,
            page=page_index,
            bbox=_rect_tuple(block.get("bbox") or (0, 0, 0, 0)),
            block_index=block_index,
        ))
    return captions


def _page_graphics(page: Any, page_index: int, page_rect: Any,
                   min_area: float) -> List[GraphicCandidate]:
    """Collect table, image and vector-drawing candidates on one page.

    Tables come from PyMuPDF's own table finder rather than from clustering the
    ruling lines: clustering cannot reliably tell one table from two adjacent
    ones, whereas the finder uses text alignment and is far more stable. Line
    clustering remains as the fallback for drawings that are not tables (charts,
    diagrams, plots).
    """
    candidates: List[GraphicCandidate] = []

    # 1. Tables, via the built-in finder (PyMuPDF >= 1.23).
    table_boxes: List[Tuple[float, float, float, float]] = []
    try:
        finder = page.find_tables()
        for table in getattr(finder, "tables", []) or []:
            bbox = getattr(table, "bbox", None)
            if not bbox:
                continue
            box = (float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3]))
            table_boxes.append(box)
            candidates.append(GraphicCandidate(page_index, box, "table"))
    except Exception as exc:  # noqa: BLE001 - finder is heuristic and may fail
        logger.debug("第 %s 页表格检测跳过：%s", page_index + 1, exc)

    # 2. Embedded raster images.
    try:
        for info in page.get_image_info():
            bbox = info.get("bbox")
            if not bbox:
                continue
            box = (float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3]))
            area = (box[2] - box[0]) * (box[3] - box[1])
            if area >= min_area:
                candidates.append(GraphicCandidate(page_index, box, "image"))
    except Exception as exc:  # noqa: BLE001 - older PyMuPDF or odd page
        logger.debug("第 %s 页图像检测跳过：%s", page_index + 1, exc)

    # 3. Vector drawings, clustered. Zero-extent line segments are kept here:
    #    a chart is often nothing but lines.
    drawing_boxes: List[Tuple[float, float, float, float]] = []
    try:
        for drawing in page.get_drawings():
            rect = drawing.get("rect")
            if rect is None:
                continue
            box = _rect_tuple(rect)
            if (box[2] - box[0]) <= 0 and (box[3] - box[1]) <= 0:
                continue
            drawing_boxes.append(box)
    except Exception:  # pragma: no cover - defensive
        drawing_boxes = []

    if drawing_boxes:
        # 等高线/矢量地图页会有上万条路径（实测单页可到 1.3 万+），逐对聚类是
        # O(n²) 且结果只是把整张地图糊成一个大图，毫无提取价值——超过阈值直接跳过。
        if len(drawing_boxes) > 2000:
            logger.info("第 %s 页矢量路径过多（%s），跳过矢量图聚类",
                        page_index + 1, len(drawing_boxes))
        else:
            page_area = max(1.0, float(page_rect.width) * float(page_rect.height))
            for box in merge_boxes(drawing_boxes, gap=3.0):
                area = (box[2] - box[0]) * (box[3] - box[1])
                if area < min_area:
                    continue
                if area / page_area > 0.92:
                    continue
                # A table's rules are drawings too; skip them so the table is not
                # reported twice with two different bounding boxes.
                if any(_overlap_ratio(box, table_box) >= 0.7 for table_box in table_boxes):
                    continue
                candidates.append(GraphicCandidate(page_index, box, "drawing"))

    # 4. Merge overlapping candidates into single regions.
    if not candidates:
        return []
    priority = {"table": 3, "image": 2, "drawing": 1}
    merged: List[GraphicCandidate] = []
    for candidate in sorted(candidates, key=lambda item: (priority.get(item.source, 0), item.area),
                            reverse=True):
        for existing in merged:
            if _overlap_ratio(candidate.bbox, existing.bbox) >= 0.5:
                if priority.get(candidate.source, 0) > priority.get(existing.source, 0):
                    existing.source = candidate.source
                existing.bbox = _union([existing.bbox, candidate.bbox])
                break
        else:
            merged.append(GraphicCandidate(candidate.page, candidate.bbox, candidate.source))
    return merged


def _pair_captions(
    captions: Sequence[Caption],
    graphics: Sequence[GraphicCandidate],
    window: float,
) -> Tuple[List[GraphicCandidate], List[Caption]]:
    """Attach captions to graphics; return paired graphics and leftover captions.

    Figure captions conventionally sit *below* the graphic and table captions
    *above* it, so the pairing prefers that arrangement but accepts the reverse
    when nothing else is close enough — templates vary more than conventions do.
    """
    unmatched_graphics = list(graphics)
    leftovers: List[Caption] = []

    for caption in sorted(captions, key=lambda item: item.center_y):
        same_page = [item for item in unmatched_graphics if item.page == caption.page]
        if not same_page:
            leftovers.append(caption)
            continue

        preferred = []
        for item in same_page:
            gap = _vertical_gap(item.bbox, caption.bbox)
            if gap > window:
                continue
            # Prefer a candidate whose detector agrees with the caption kind:
            # a "Table N" caption should bind to the table finder's region, not
            # to a nearby chart that happens to be closer.
            if caption.kind == "table":
                affinity = 0 if item.source == "table" else 1
            else:
                affinity = 0 if item.source in ("image", "drawing") else 1
            if caption.kind == "figure":
                # Graphic above the caption.
                above = item.bbox[3] <= caption.bbox[1] + window * 0.5
                side = 0 if above else 1
            else:
                # Table below the caption.
                below = item.bbox[1] >= caption.bbox[3] - window * 0.5
                side = 0 if below else 1
            preferred.append((affinity, side, gap, item))
        if not preferred:
            leftovers.append(caption)
            continue

        preferred.sort(key=lambda entry: (entry[0], entry[1], entry[2]))
        chosen = preferred[0][3]
        chosen.caption = caption
        unmatched_graphics = [item for item in unmatched_graphics if item is not chosen]

    return unmatched_graphics, leftovers


def _region_below(caption: Caption, page_height: float,
                  other_captions: Sequence[Caption]) -> Tuple[float, float, float, float]:
    """Fallback crop for a caption whose body is text plus rules, not a graphic."""
    top = min(page_height, caption.bottom + 4.0)
    bottom = page_height * 0.97
    for other in other_captions:
        if other is caption or other.page != caption.page:
            continue
        if other.top > caption.bottom + 8.0:
            bottom = min(bottom, other.top - 4.0)
    if bottom - top < 24.0:
        top = max(0.0, caption.top - 4.0)
        bottom = min(page_height, caption.bottom + 240.0)
    return (caption.bbox[0], top, caption.bbox[2], bottom)


# ---------------------------------------------------------------------------
# main entry point
# ---------------------------------------------------------------------------

def analyse_pdf(
    pdf_bytes: bytes,
    *,
    dpi: int = 200,
    min_area_ratio: float = DEFAULT_MIN_AREA_RATIO,
    caption_window: float = DEFAULT_CAPTION_WINDOW,
    max_pages: int = 400,
) -> LayoutReport:
    """Detect pages, captions and graphic regions; crop every asset.

    Returns the crops as PNG bytes plus per-page text. Nothing is written to
    disk here so the caller controls the content-addressed layout (FR-4.3).
    """
    import fitz  # PyMuPDF

    report = LayoutReport()
    document = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        report.page_count = int(document.page_count)
        limit = min(report.page_count, max(1, int(max_pages or report.page_count)))

        all_captions: List[Caption] = []
        all_graphics: List[GraphicCandidate] = []
        page_heights: Dict[int, float] = {}

        for page_index in range(limit):
            page = document.load_page(page_index)
            page_heights[page_index] = float(page.rect.height)
            try:
                text = page.get_text("text") or ""
            except Exception:  # pragma: no cover - malformed page
                text = ""
            report.page_texts.append(text)

            page_area = max(1.0, float(page.rect.width) * float(page.rect.height))
            min_area = page_area * float(min_area_ratio)

            captions = _page_captions(page, page_index)
            all_captions.extend(captions)
            all_graphics.extend(_page_graphics(page, page_index, page.rect, min_area))

        report.captions = all_captions

        unpaired, leftover_captions = _pair_captions(all_captions, all_graphics, caption_window)

        # A graphic with no caption nearby is still a figure; unlabelled
        # graphics are common in preprints and worth keeping.
        paired: List[Tuple[str, Optional[Caption], Tuple[float, float, float, float]]] = []
        for item in all_graphics:
            if item.caption is not None:
                paired.append((item.caption.kind, item.caption, item.bbox))

        for caption in leftover_captions:
            if caption.kind != "table":
                # A figure caption with no graphic nearby cannot be cropped
                # reliably; guessing a region from the caption alone is exactly
                # the kind of false positive the user asked to eliminate.
                report.warnings.append(
                    f"{caption.text[:60]}：未找到邻近图形，已跳过图片提取"
                )
                continue
            page_index = caption.page
            box = _region_below(caption, page_heights.get(page_index, 792.0), all_captions)
            paired.append((caption.kind, caption, box))

        for item in unpaired:
            if item.caption is not None:
                continue
            # Only sizeable, image-backed regions become uncaptioned figures.
            if item.source == "image" or item.area >= (page_heights.get(item.page, 792.0) * 100.0):
                paired.append(("figure", None, item.bbox))

        # Order by page then vertical position, then number per kind (FR-4.2).
        paired.sort(key=lambda entry: (entry[2][1], entry[2][0]))
        counters = {"figure": 0, "table": 0}
        zoom = max(0.5, float(dpi) / 72.0)
        matrix = fitz.Matrix(zoom, zoom)

        for kind, caption, box in paired:
            page_index = caption.page if caption is not None else _page_of(box, all_graphics)
            counters[kind] = counters.get(kind, 0) + 1
            ordinal = counters[kind]
            label = f"{'fig' if kind == 'figure' else 'tab'}{ordinal}"

            include_caption = caption is not None
            clip = box
            if include_caption and caption is not None:
                clip = _union([box, caption.bbox])
            clip = _pad(clip, page_index, document, 6.0)

            try:
                page = document.load_page(page_index)
                pixmap = page.get_pixmap(matrix=matrix, clip=fitz.Rect(*clip), alpha=False)
                data = pixmap.tobytes("png")
                width, height = pixmap.width, pixmap.height
            except Exception as exc:  # noqa: BLE001 - keep going, report it
                report.warnings.append(f"{label} 裁剪失败：{exc}")
                logger.warning("%s 裁剪失败：%s", label, exc)
                continue

            # Ignore degenerate crops (a few pixels of rule line).
            if width < 24 or height < 16:
                counters[kind] -= 1
                continue

            report.assets.append(CroppedAsset(
                label=label,
                kind=kind,
                ordinal=ordinal,
                page=page_index + 1,
                bbox=clip,
                caption=(caption.text if caption else ""),
                caption_source=("local" if caption else "none"),
                width=width,
                height=height,
                data=data,
            ))

        # Renumber per kind so labels are contiguous even if one crop failed.
        for kind in ("figure", "table"):
            prefix = "fig" if kind == "figure" else "tab"
            selected = [item for item in report.assets if item.kind == kind]
            for index, item in enumerate(sorted(selected, key=lambda entry: (entry.page, entry.bbox[1])), start=1):
                item.ordinal = index
                item.label = f"{prefix}{index}"

        logger.info(
            "版面分析完成：%s 页，图表 %s 个（图 %s，表 %s）",
            report.page_count, len(report.assets),
            sum(1 for item in report.assets if item.kind == "figure"),
            sum(1 for item in report.assets if item.kind == "table"),
        )
        return report
    finally:
        document.close()


def _page_of(box: Tuple[float, float, float, float],
             graphics: Sequence[GraphicCandidate]) -> int:
    for item in graphics:
        if item.bbox == box:
            return item.page
    return 0


def _pad(box: Tuple[float, float, float, float], page_index: int, document: Any,
         padding: float) -> Tuple[float, float, float, float]:
    try:
        rect = document.load_page(page_index).rect
        return (
            max(0.0, box[0] - padding),
            max(0.0, box[1] - padding),
            min(float(rect.width), box[2] + padding),
            min(float(rect.height), box[3] + padding),
        )
    except Exception:  # pragma: no cover - defensive
        return box


def extract_text_pages(pdf_bytes: bytes, max_pages: int = 400) -> List[str]:
    """Per-page plain text, used for the model prompt and as a text fallback."""
    import fitz

    document = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        pages: List[str] = []
        for index in range(min(document.page_count, max_pages)):
            try:
                pages.append(document.load_page(index).get_text("text") or "")
            except Exception:  # pragma: no cover - malformed page
                pages.append("")
        return pages
    finally:
        document.close()


def page_count(pdf_bytes: bytes) -> int:
    import fitz

    document = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        return int(document.page_count)
    finally:
        document.close()
