"""Sending the PDF to a vision model and validating what comes back (FR-3).

Responsibilities, in order:

1. slice the PDF into request-sized pieces (whole file, or page batches);
2. build the prompt pair and call the routed ``parse`` provider;
3. recover JSON from whatever prose the model wrapped it in;
4. normalise the payload into a canonical shape with every field present;
5. merge slices, then reconcile the model's figure/table descriptions with the
   labels PyMuPDF already assigned;
6. retry with an explicit correction prompt when validation fails.

The last point matters: a model that emits a truncated JSON object usually fixes
it when told exactly what was wrong, which is far cheaper than a whole re-parse.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ..config import SettingsStore
from ..core.errors import ParseFailed, ProviderNotConfigured
from ..core.jsonutil import JsonExtractionError, as_str_list, as_text, extract_json
from ..core.logging import get_logger
from ..core.text import detect_language, first_non_empty, normalize_whitespace, truncate
from ..kg.skeleton import SPINE_SLUGS, classify_text
from ..llm.base import Attachment, LLMError, Message
from ..llm.pdf_input import build_file_attachment, plan_slices, render_pages
from ..llm.prompts import parse as parse_prompts
from ..llm.registry import KIND_PARSE, ProviderRegistry
from .pdf_layout import extract_text_pages

logger = get_logger(__name__)

ProgressCallback = Optional[Callable[[str, float], None]]

VALID_VENUE_TYPES = {"journal", "conference", "preprint", "thesis", "other"}


@dataclass
class ParsedPaper:
    """Canonical parse result, independent of which model produced it."""

    title: str = ""
    title_original: str = ""
    authors: List[Dict[str, Any]] = field(default_factory=list)
    affiliations: List[Dict[str, Any]] = field(default_factory=list)
    journal: str = ""
    venue_type: str = ""
    published_at: str = ""
    year: Optional[int] = None
    doi: str = ""
    url: str = ""
    abstract: str = ""
    keywords: List[str] = field(default_factory=list)
    language: str = "en"
    sections: List[Dict[str, Any]] = field(default_factory=list)
    figures: List[Dict[str, Any]] = field(default_factory=list)
    tables: List[Dict[str, Any]] = field(default_factory=list)
    contributions: List[str] = field(default_factory=list)
    limitations: List[str] = field(default_factory=list)
    future_work: List[str] = field(default_factory=list)
    usage: Dict[str, Any] = field(default_factory=dict)
    raw_responses: List[Dict[str, Any]] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    provider: str = ""
    model: str = ""

    @property
    def full_text(self) -> str:
        return "\n\n".join(
            f"{item.get('title', '')}\n{item.get('text', '')}" for item in self.sections
        )


# ---------------------------------------------------------------------------
# normalisation
# ---------------------------------------------------------------------------

def _coerce_year(value: Any, published_at: str = "") -> Optional[int]:
    if isinstance(value, (int, float)) and 1500 <= int(value) <= 2200:
        return int(value)
    text = as_text(value)
    import re

    match = re.search(r"(1[5-9]\d{2}|20\d{2}|21\d{2})", text or published_at)
    if match:
        return int(match.group(1))
    return None


def _normalise_author(entry: Any) -> Optional[Dict[str, Any]]:
    if isinstance(entry, str):
        name = entry.strip()
        return {"name": name, "affiliations": []} if name else None
    if not isinstance(entry, dict):
        return None
    name = first_non_empty(
        as_text(entry.get("name")), as_text(entry.get("name_en")),
        as_text(entry.get("author")), as_text(entry.get("姓名")),
    )
    if not name:
        return None
    raw_affiliations = entry.get("affiliations") or entry.get("affiliation") \
        or entry.get("institutions") or entry.get("单位")
    affiliations: List[Any] = []
    if isinstance(raw_affiliations, dict):
        affiliations = [raw_affiliations]
    elif isinstance(raw_affiliations, (list, tuple)):
        affiliations = list(raw_affiliations)
    elif isinstance(raw_affiliations, str) and raw_affiliations.strip():
        affiliations = [raw_affiliations]
    return {
        "name": name,
        "name_en": as_text(entry.get("name_en")),
        "affiliations": affiliations,
        "corresponding": bool(entry.get("corresponding") or entry.get("is_corresponding")),
        "email": as_text(entry.get("email")),
        "orcid": as_text(entry.get("orcid")),
    }


def _normalise_affiliation(entry: Any) -> Optional[Dict[str, str]]:
    if isinstance(entry, str):
        name = entry.strip()
        return {"name": name, "department": "", "country": ""} if name else None
    if not isinstance(entry, dict):
        return None
    name = first_non_empty(
        as_text(entry.get("name")), as_text(entry.get("institution")),
        as_text(entry.get("org")), as_text(entry.get("单位")),
    )
    if not name:
        return None
    return {
        "name": name,
        "department": as_text(entry.get("department") or entry.get("dept") or entry.get("院系")),
        "country": as_text(entry.get("country") or entry.get("国家")),
        "raw": as_text(entry.get("raw")),
    }


def _normalise_section(entry: Any, ordinal: int) -> Optional[Dict[str, Any]]:
    if isinstance(entry, str):
        text = entry.strip()
        return {"title": "", "text": text, "ordinal": ordinal, "mapped_by": "rule"} if text else None
    if not isinstance(entry, dict):
        return None
    title = first_non_empty(as_text(entry.get("title")), as_text(entry.get("heading")),
                            as_text(entry.get("章节")))
    text = first_non_empty(as_text(entry.get("text")), as_text(entry.get("content")),
                           as_text(entry.get("body")))
    if not title and not text:
        return None
    section = as_text(entry.get("section")).strip().lower()
    mapped_by = "model"
    if section not in SPINE_SLUGS:
        # The model either omitted it or invented a value; fall back to rules.
        section = classify_text(title, text) or ""
        mapped_by = "rule" if section else ""
    return {
        "title": title or f"第 {ordinal + 1} 节",
        "text": normalize_whitespace(text),
        "section": section,
        "ordinal": ordinal,
        "mapped_by": mapped_by,
    }


def _normalise_asset(entry: Any, kind: str, ordinal: int) -> Optional[Dict[str, Any]]:
    if isinstance(entry, str):
        return {"label": "", "caption": entry.strip(), "page": 0, "content": "",
                "ordinal": ordinal, "kind": kind}
    if not isinstance(entry, dict):
        return None
    label = as_text(entry.get("label") or entry.get("id") or entry.get("编号")).strip()
    caption = first_non_empty(as_text(entry.get("caption")), as_text(entry.get("title")),
                              as_text(entry.get("图注")), as_text(entry.get("表注")))
    content = first_non_empty(as_text(entry.get("content")), as_text(entry.get("description")),
                              as_text(entry.get("text")), as_text(entry.get("内容")))
    if not label and not caption and not content:
        return None
    return {
        "label": label,
        "kind": kind,
        "caption": caption,
        "content": content,
        "page": _coerce_page(entry.get("page")),
        "ordinal": ordinal,
    }


def _coerce_page(value: Any) -> int:
    if isinstance(value, (int, float)) and value >= 0:
        return int(value)
    text = as_text(value)
    import re

    match = re.search(r"\d+", text)
    return int(match.group(0)) if match else 0


def normalise_payload(payload: Any) -> ParsedPaper:
    """Coerce a model payload into :class:`ParsedPaper`.

    Every field is optional in practice — models omit fields, return ``null``,
    or use a synonym — so the normaliser fills defaults rather than failing.
    Only a payload with nothing usable at all raises.
    """
    if not isinstance(payload, dict):
        raise ParseFailed("解析结果不是 JSON 对象")

    result = ParsedPaper()
    result.title = first_non_empty(as_text(payload.get("title")), as_text(payload.get("标题")))
    result.title_original = first_non_empty(
        as_text(payload.get("title_original")), as_text(payload.get("original_title")),
        as_text(payload.get("原标题")),
    )

    raw_authors = payload.get("authors") or payload.get("作者") or []
    if isinstance(raw_authors, dict):
        raw_authors = [raw_authors]
    if isinstance(raw_authors, str):
        raw_authors = [raw_authors]
    for entry in raw_authors or []:
        author = _normalise_author(entry)
        if author:
            result.authors.append(author)

    raw_affiliations = payload.get("affiliations") or payload.get("institutions") \
        or payload.get("单位") or []
    if isinstance(raw_affiliations, dict):
        raw_affiliations = [raw_affiliations]
    if isinstance(raw_affiliations, str):
        raw_affiliations = [raw_affiliations]
    for entry in raw_affiliations or []:
        affiliation = _normalise_affiliation(entry)
        if affiliation:
            result.affiliations.append(affiliation)

    result.journal = first_non_empty(
        as_text(payload.get("journal")), as_text(payload.get("venue")),
        as_text(payload.get("conference")), as_text(payload.get("期刊")),
        as_text(payload.get("会议")),
    )
    result.venue_type = as_text(payload.get("venue_type")).strip().lower()
    if result.venue_type not in VALID_VENUE_TYPES:
        result.venue_type = _guess_venue_type(result.journal)
    result.published_at = _normalise_date(
        first_non_empty(as_text(payload.get("published_at")), as_text(payload.get("date")),
                        as_text(payload.get("刊发时间")), as_text(payload.get("year")))
    )
    result.year = _coerce_year(payload.get("year"), result.published_at)
    result.doi = as_text(payload.get("doi")).strip()
    result.url = as_text(payload.get("url") or payload.get("link")).strip()
    result.abstract = normalize_whitespace(
        first_non_empty(as_text(payload.get("abstract")), as_text(payload.get("摘要")))
    )
    result.keywords = as_str_list(payload.get("keywords") or payload.get("关键词"))
    result.language = as_text(payload.get("language")).strip().lower()
    if result.language not in ("zh", "en", "mixed"):
        result.language = ""

    raw_sections = payload.get("sections") or payload.get("章节") or []
    if isinstance(raw_sections, dict):
        raw_sections = [raw_sections]
    for index, entry in enumerate(raw_sections or []):
        section = _normalise_section(entry, index)
        if section:
            result.sections.append(section)

    for kind, key in (("figure", "figures"), ("table", "tables")):
        raw_assets = payload.get(key) or payload.get(f"{kind}s") or []
        if isinstance(raw_assets, dict):
            raw_assets = [raw_assets]
        bucket: List[Dict[str, Any]] = []
        for index, entry in enumerate(raw_assets or []):
            asset = _normalise_asset(entry, kind, index)
            if asset:
                bucket.append(asset)
        if kind == "figure":
            result.figures = bucket
        else:
            result.tables = bucket

    result.contributions = as_str_list(payload.get("contributions") or payload.get("贡献"))
    result.limitations = as_str_list(payload.get("limitations") or payload.get("不足"))
    result.future_work = as_str_list(payload.get("future_work") or payload.get("未来工作"))

    if not result.sections and not result.abstract and not result.title:
        raise ParseFailed("解析结果中缺少标题、摘要与正文，无法入库")

    if not result.language:
        sample = result.abstract or result.full_text or result.title
        result.language = detect_language(sample)
    if not result.year:
        result.year = _coerce_year(None, result.published_at)
    return result


def _guess_venue_type(journal: str) -> str:
    lowered = (journal or "").lower()
    if not lowered:
        return ""
    if "arxiv" in lowered or "preprint" in lowered or "biorxiv" in lowered:
        return "preprint"
    for token in ("conference", "proceedings", "symposium", "workshop", "meeting",
                  "neurips", "nips", "icml", "iclr", "cvpr", "acl", "emnlp", "aaai", "ijcai",
                  "kdd", "sigir", "www", "会议"):
        if token in lowered:
            return "conference"
    for token in ("journal", "transactions", "letters", "review", "学报", "期刊"):
        if token in lowered:
            return "journal"
    return "other"


def _normalise_date(value: str) -> str:
    """Best-effort ISO date: accepts 2024, 2024-05, 2024-05-01, 2024/5/1."""
    import re

    text = (value or "").strip()
    if not text:
        return ""
    match = re.search(r"(1[5-9]\d{2}|20\d{2}|21\d{2})(?:[-/年.](\d{1,2}))?(?:[-/月.](\d{1,2}))?", text)
    if not match:
        return text[:32]
    year = match.group(1)
    month = match.group(2)
    day = match.group(3)
    if month and day:
        return f"{year}-{int(month):02d}-{int(day):02d}"
    if month:
        return f"{year}-{int(month):02d}"
    return year


# ---------------------------------------------------------------------------
# slice merging
# ---------------------------------------------------------------------------

def merge_slices(parts: Sequence[ParsedPaper]) -> ParsedPaper:
    """Combine per-slice results, keeping the first slice authoritative for metadata."""
    if not parts:
        raise ParseFailed("没有任何解析片段")
    if len(parts) == 1:
        return parts[0]

    merged = ParsedPaper()
    for part in parts:
        for field_name in ("title", "title_original", "journal", "venue_type",
                           "published_at", "doi", "url", "abstract", "language"):
            if not getattr(merged, field_name) and getattr(part, field_name):
                setattr(merged, field_name, getattr(part, field_name))
        if merged.year is None and part.year is not None:
            merged.year = part.year

        if part.authors and not merged.authors:
            merged.authors = list(part.authors)
        if part.affiliations and not merged.affiliations:
            merged.affiliations = list(part.affiliations)
        if part.keywords and not merged.keywords:
            merged.keywords = list(part.keywords)

        merged.sections.extend(part.sections)
        merged.figures.extend(part.figures)
        merged.tables.extend(part.tables)
        merged.contributions.extend(part.contributions)
        merged.limitations.extend(part.limitations)
        merged.future_work.extend(part.future_work)
        merged.raw_responses.extend(part.raw_responses)
        merged.warnings.extend(part.warnings)
        if part.provider:
            merged.provider = merged.provider or part.provider
        if part.model:
            merged.model = merged.model or part.model

    # Re-index sections so ordinals stay contiguous across slices.
    for index, section in enumerate(merged.sections):
        section["ordinal"] = index
    merged.figures = _dedupe_assets(merged.figures, "figure")
    merged.tables = _dedupe_assets(merged.tables, "table")
    merged.contributions = _dedupe_strings(merged.contributions)
    merged.limitations = _dedupe_strings(merged.limitations)
    merged.future_work = _dedupe_strings(merged.future_work)
    merged.usage = _sum_usage([part.usage for part in parts])
    return merged


def _dedupe_assets(items: Sequence[Dict[str, Any]], kind: str) -> List[Dict[str, Any]]:
    seen_labels: set = set()
    out: List[Dict[str, Any]] = []
    for item in items:
        label = (item.get("label") or "").strip().lower()
        if label and label in seen_labels:
            continue
        if label:
            seen_labels.add(label)
        out.append(item)
    for index, item in enumerate(out):
        item["ordinal"] = index
    return out


def _dedupe_strings(items: Sequence[str]) -> List[str]:
    seen: set = set()
    out: List[str] = []
    for item in items:
        key = (item or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def _sum_usage(usages: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    total: Dict[str, int] = {}
    for usage in usages:
        for key, value in (usage or {}).items():
            if isinstance(value, (int, float)):
                total[key] = int(total.get(key, 0)) + int(value)
    return total


# ---------------------------------------------------------------------------
# reconciling model output with locally cropped assets
# ---------------------------------------------------------------------------

def reconcile_assets(
    local_assets: Sequence[Dict[str, Any]],
    model_figures: Sequence[Dict[str, Any]],
    model_tables: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Attach model captions/descriptions to the locally cropped files.

    The local pass owns labels and geometry (FR-4.2); the model owns meaning.
    Matching is by label when the model echoed ours, otherwise by position, and
    any surplus model entries are kept as caption-only rows so no information is
    silently dropped.
    """
    result: List[Dict[str, Any]] = []
    used_model: set = set()

    for kind, model_items in (("figure", model_figures), ("table", model_tables)):
        local_of_kind = [item for item in local_assets if item.get("kind") == kind]
        by_label = {}
        for index, item in enumerate(model_items):
            label = (item.get("label") or "").strip().lower()
            if label:
                by_label[label] = index

        for position, local in enumerate(local_of_kind):
            entry = dict(local)
            model_entry: Optional[Dict[str, Any]] = None
            label = (local.get("label") or "").strip().lower()
            index = by_label.get(label)
            if index is None and position < len(model_items):
                index = position
            if index is not None and index not in used_model:
                model_entry = model_items[index]
                used_model.add(index)

            if model_entry:
                if model_entry.get("caption"):
                    entry["caption"] = model_entry["caption"]
                    entry["caption_source"] = "model" if not local.get("caption") else "merged"
                entry["content_text"] = model_entry.get("content", "")
                if model_entry.get("page"):
                    entry.setdefault("model_page", model_entry["page"])
            result.append(entry)

        # Model entries with no local counterpart: keep the text, no file.
        for index, item in enumerate(model_items):
            if index in used_model:
                continue
            result.append({
                "kind": kind,
                "label": (item.get("label") or f"{'fig' if kind == 'figure' else 'tab'}"
                          f"{len(local_of_kind) + index + 1}"),
                "ordinal": len(local_of_kind) + index,
                "page": item.get("page", 0),
                "bbox": [],
                "caption": item.get("caption", ""),
                "caption_source": "model",
                "content_text": item.get("content", ""),
                "file_path": "",
                "width": 0,
                "height": 0,
                "missing_file": True,
            })
    return result


# ---------------------------------------------------------------------------
# the parser
# ---------------------------------------------------------------------------

class VisionParser:
    """Drives the ``parse`` route: PDF bytes in, :class:`ParsedPaper` out."""

    def __init__(self, registry: ProviderRegistry, settings: SettingsStore) -> None:
        self.registry = registry
        self.settings = settings

    def parse(
        self,
        pdf_bytes: bytes,
        *,
        filename: str = "paper.pdf",
        detected_assets: Optional[Sequence[Dict[str, Any]]] = None,
        text_excerpt: str = "",
        progress: ProgressCallback = None,
    ) -> ParsedPaper:
        config = self.settings.current.ingest
        client = self.registry.client(KIND_PARSE)
        route = self.registry.route_config(KIND_PARSE)
        provider = self.settings.current.provider(route.provider)

        mode = (config.pdf_mode or "image").lower()
        if mode == "auto":
            # PNG images are the preferred transport; only fall back to the
            # native PDF file when the provider cannot accept images at all.
            mode = "file" if (provider is not None and not provider.vision) else "image"

        # 纯文本模型读不了图：渲染整篇 PDF 成 PNG 纯属浪费（又慢又必然失败）。
        # 直接抛错让流水线回退本地 PyMuPDF 解析，跳过昂贵的渲染 + 失败重试。
        if mode == "image" and provider is not None and not provider.vision:
            raise ProviderNotConfigured(
                f"解析模型 {route.model or route.provider} 不支持图像输入，已跳过 PNG 渲染并回退本地解析",
                kind="parse",
            )

        slices = plan_slices(
            pdf_bytes,
            mode="file" if mode == "file" else "image",
            batch_pages=config.vision_batch_pages,
            dpi=config.render_dpi,
            max_pages=config.max_pages,
            filename=filename,
        )

        parts: List[ParsedPaper] = []
        total = len(slices)
        last_error: Optional[str] = None

        for index, chunk in enumerate(slices, start=1):
            if progress:
                progress(f"正在解析第 {index}/{total} 批（第 {chunk.page_from}-{chunk.page_to} 页）"
                         if chunk.page_to else f"正在解析第 {index}/{total} 批", index / max(total, 1))
            try:
                part = self._parse_slice(
                    client,
                    slice_attachments=chunk.attachments,
                    page_from=chunk.page_from,
                    page_to=chunk.page_to,
                    part_index=index,
                    part_total=total,
                    filename=filename,
                    detected_assets=detected_assets if index == 1 else None,
                    text_excerpt=text_excerpt if index == 1 else "",
                    temperature=route.temperature,
                    max_tokens=route.max_tokens,
                    use_json_mode=config.use_json_mode,
                    max_retry=config.max_retry,
                )
            except LLMError as exc:
                if mode == "file" and total == 1:
                    # Native PDF input rejected: retry the whole document as images.
                    logger.warning("原生 PDF 输入被拒绝（%s），改用页面图像重试", exc)
                    return self._parse_as_images(
                        client, pdf_bytes, filename, detected_assets, text_excerpt,
                        route, config, progress,
                    )
                raise
            part.provider = route.provider
            part.model = route.model
            parts.append(part)

        merged = merge_slices(parts)
        merged.provider = route.provider
        merged.model = route.model
        if not merged.title:
            merged.title = _guess_title(detected_assets, text_excerpt, filename)
            merged.warnings.append("模型未返回标题，已回退到文本抽取结果")
        return merged

    def _parse_as_images(
        self,
        client: Any,
        pdf_bytes: bytes,
        filename: str,
        detected_assets: Optional[Sequence[Dict[str, Any]]],
        text_excerpt: str,
        route: Any,
        config: Any,
        progress: ProgressCallback,
    ) -> ParsedPaper:
        """Fallback path: render every page batch and parse as images."""
        pages = render_pages(pdf_bytes, 1, 0, dpi=config.render_dpi,
                             max_edge=config.max_image_edge)
        batch = max(1, int(config.vision_batch_pages or 20))
        parts: List[ParsedPaper] = []
        total = max(1, (len(pages) + batch - 1) // batch)
        for index in range(total):
            window = pages[index * batch : (index + 1) * batch]
            if not window:
                continue
            if progress:
                progress(f"改用图像解析第 {index + 1}/{total} 批", (index + 1) / total)
            parts.append(self._parse_slice(
                client,
                slice_attachments=window,
                page_from=window[0].page or 1,
                page_to=window[-1].page or 0,
                part_index=index + 1,
                part_total=total,
                filename=filename,
                detected_assets=detected_assets if index == 0 else None,
                text_excerpt=text_excerpt if index == 0 else "",
                temperature=route.temperature,
                max_tokens=route.max_tokens,
                use_json_mode=config.use_json_mode,
                max_retry=config.max_retry,
            ))
        merged = merge_slices(parts)
        merged.warnings.append("原生 PDF 输入不被支持，已改用页面图像解析")
        return merged

    def _parse_slice(
        self,
        client: Any,
        *,
        slice_attachments: Sequence[Attachment],
        page_from: int,
        page_to: int,
        part_index: int,
        part_total: int,
        filename: str,
        detected_assets: Optional[Sequence[Dict[str, Any]]],
        text_excerpt: str,
        temperature: float,
        max_tokens: int,
        use_json_mode: bool,
        max_retry: int,
    ) -> ParsedPaper:
        system = Message.system(parse_prompts.parse_system_prompt())
        user_text = parse_prompts.parse_user_prompt(
            page_from=page_from, page_to=page_to, part_index=part_index, part_total=part_total,
            filename=filename, detected_assets=detected_assets, text_excerpt=text_excerpt,
        )
        messages = [system, Message.user(user_text, list(slice_attachments))]
        response_format = {"type": "json_object"} if use_json_mode else None

        attempts = max(0, int(max_retry)) + 1
        last_error: Optional[str] = None
        previous_text = ""

        for attempt in range(attempts):
            result = client.chat(
                messages,
                temperature=temperature if attempt == 0 else 0.0,
                max_tokens=max_tokens,
                response_format=response_format,
            )
            previous_text = result.text or ""
            try:
                payload = extract_json(previous_text, expect="object")
                parsed = normalise_payload(payload)
                parsed.usage = result.usage.to_dict()
                parsed.raw_responses.append({
                    "part": part_index,
                    "page_from": page_from,
                    "page_to": page_to,
                    "model": result.model,
                    "text": truncate(previous_text, 20000),
                })
                return parsed
            except (JsonExtractionError, ParseFailed) as exc:
                last_error = str(exc)
                logger.warning("解析结果校验失败（第 %s/%s 次）：%s", attempt + 1, attempts, exc)
                if attempt + 1 >= attempts:
                    break
                # Re-ask with the concrete error so the model can self-correct.
                messages = messages[:2] + [
                    Message.assistant(truncate(previous_text, 4000)),
                    Message.user(parse_prompts.repair_prompt(last_error, previous_text)),
                ]
        raise ParseFailed(f"大模型解析结果无法通过校验：{last_error}")


_HEADER_LINE_HINTS = (
    "original article", "original research", "research article", "review article",
    "original paper", "article", "case report", "editorial", "letter",
    "received", "accepted", "published", "doi", "corresponding author",
    "journal of", "elsevier", "springer", "wiley",
)


def _guess_title(detected_assets: Optional[Sequence[Dict[str, Any]]],
                 text_excerpt: str, filename: str) -> str:
    """Last-resort title: the first substantial line of the extracted text.

    Journal headers ("ORIGINAL ARTICLE", "Received: …") are skipped so the real
    paper title is not mistaken for layout furniture.
    """
    for line in (text_excerpt or "").splitlines():
        stripped = line.strip()
        if len(stripped) < 12 or stripped.startswith("---"):
            continue
        lowered = stripped.lower()
        if lowered.startswith(("abstract", "摘要", "keywords", "关键词")):
            continue
        if any(hint in lowered for hint in _HEADER_LINE_HINTS):
            continue
        return truncate(stripped, 200)
    return filename.rsplit(".", 1)[0] or "未命名论文"


# ---------------------------------------------------------------------------
# local (non-LLM) fallback
# ---------------------------------------------------------------------------

_HEADING_RE = re.compile(
    r"^(?:\d+(?:\.\d+)*\.?\s+|[IVXLC]+\.\s+)?"
    r"(?:abstract|introduction|background|motivation|related work|prior work|"
    r"method|methods|methodology|approach|model|architecture|framework|algorithm|"
    r"experiment|experiments|evaluation|results?|ablation|"
    r"discussion|analysis|conclusion|conclusions|future work|limitations?|"
    r"references|acknowledg|"
    r"摘要|引言|前言|背景|动机|相关工作|研究现状|方法|方法论|模型|架构|框架|算法|"
    r"实验|评估|结果|消融|讨论|分析|结论|总结|不足|局限|展望|未来工作|参考文献|致谢)"
    r"\b",
    re.IGNORECASE,
)

_ABSTRACT_HEADINGS = ("abstract", "摘要")
_REFERENCE_HEADINGS = ("references", "参考文献", "acknowledg", "致谢")


def local_parse(
    pdf_bytes: bytes,
    *,
    page_texts: Optional[Sequence[str]] = None,
    filename: str = "paper.pdf",
) -> ParsedPaper:
    """Extract a :class:`ParsedPaper` locally with PyMuPDF, no model call.

    Used as the last-resort fallback when the PNG -> vision-LLM path fails. It is
    deliberately honest about its limits: metadata (authors, journal, DOI) is
    left empty rather than guessed, and the text is kept verbatim so nothing is
    fabricated.
    """
    pages = list(page_texts) if page_texts else extract_text_pages(pdf_bytes)
    full = "\n".join(pages)
    cleaned_lines = [line.strip() for line in full.splitlines()]

    result = ParsedPaper()
    result.provider = "local"
    result.model = "pymupdf-fallback"
    result.language = detect_language(full)
    result.warnings.append("视觉模型解析失败，已回退到本地 PyMuPDF 文本抽取（无作者/期刊/DOI 元数据）")

    # ---- title: first substantial non-heading line ---------------------
    result.title = _guess_title(None, full, filename)
    if result.title == filename.rsplit(".", 1)[0]:
        result.title = filename.rsplit(".", 1)[0]

    # ---- split into sections by heading lines --------------------------
    sections: List[Dict[str, Any]] = []
    current_title = ""
    current_lines: List[str] = []

    def flush() -> None:
        body = normalize_whitespace(" ".join(current_lines))
        if body:
            sections.append({
                "title": current_title or "正文",
                "text": body,
                "section": classify_text(current_title, body),
                "ordinal": len(sections),
                "mapped_by": "rule",
            })

    for line in cleaned_lines:
        stripped = line.strip()
        is_heading = (
            len(stripped) <= 90
            and bool(_HEADING_RE.match(stripped))
            and not _looks_like_abstract_body(stripped)
        )
        if is_heading:
            flush()
            current_title = stripped
            current_lines = []
        else:
            current_lines.append(line)
    flush()

    result.sections = [s for s in sections if s["text"]]
    if not result.sections:
        # No headings found: treat the whole text as one section.
        result.sections = [{
            "title": "全文",
            "text": normalize_whitespace(full),
            "section": classify_text("", full) or "background",
            "ordinal": 0,
            "mapped_by": "fallback",
        }]

    # ---- abstract -------------------------------------------------------
    result.abstract = _extract_abstract(cleaned_lines)

    return result


def _looks_like_abstract_body(text: str) -> bool:
    """A heading line should not be a full sentence that happens to start with a
    keyword (e.g. "Methods were compared using ...")."""
    lowered = text.lower()
    return len(text) > 60 and any(
        verb in lowered for verb in ("is ", "are ", "was ", "were ", " we ", "本研究", "本文", "提出")
    )


def _extract_abstract(lines: Sequence[str]) -> str:
    """Grab the paragraph after an abstract heading until the next heading."""
    collecting = False
    buffer: List[str] = []
    for line in lines:
        stripped = line.strip()
        lower = stripped.lower()
        if not collecting:
            if any(lower.startswith(heading) for heading in _ABSTRACT_HEADINGS):
                collecting = True
                # Strip the heading itself ("Abstract: ..." -> "...") when inline.
                remainder = stripped
                for heading in _ABSTRACT_HEADINGS:
                    if lower.startswith(heading):
                        remainder = stripped[len(heading):].lstrip(":：. ")
                        break
                if remainder:
                    buffer.append(remainder)
                continue
            continue
        # Stop at the next heading or at the references block.
        if _HEADING_RE.match(stripped) and len(stripped) <= 90:
            break
        if any(lower.startswith(heading) for heading in _REFERENCE_HEADINGS):
            break
        if stripped:
            buffer.append(stripped)
    abstract = normalize_whitespace(" ".join(buffer))
    return abstract[:4000]
