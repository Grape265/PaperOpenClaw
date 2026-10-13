"""Writing the OKF v0.2 bundle for one paper (FR-5).

Bundle shape::

    okf/
      index.md              okf_version + progressive-disclosure listing
      log.md                creation history
      paper.md              type: Research Paper  (metadata + abstract + links)
      references/
        index.md
        pdf.md              type: Source  (the original PDF, spec 6.3)
      sections/
        index.md
        <slug>.md           type: Section     (faithful full text)
      figures/
        index.md
        fig1.md             type: Figure      (caption + link to ../fig/fig1.png)
      tables/
        index.md
        tab1.md             type: Table

Two deliberate design points:

* **OKF holds the faithful record; the RAG pyramid holds the distillation.**
  Section bodies are the extracted original text, not summaries, so the bundle
  remains a real archive and a human can read it without the model.
* **Figure/table assets live outside the bundle**, in ``papers/<hash>/fig`` and
  ``papers/<hash>/tab``, because FR-4.3 requires exactly that. Concept files
  therefore use *relative* links (``../../fig/fig1.png``), a form the OKF spec
  explicitly permits (section 6.2).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from ..core.logging import get_logger
from ..core.paths import DataPaths
from ..core.text import slugify
from ..kg.skeleton import SPINE, SPINE_BY_SLUG, label_of, order_index
from ..memory.okf_fs import build_index_markdown, build_log_markdown, dump_concept
from ..storage.db import utc_now

logger = get_logger(__name__)

OKF_VERSION = "0.2"
GENERATOR = "paperopenclaw/1.0.0"

TYPE_PAPER = "Research Paper"
TYPE_SECTION = "Section"
TYPE_FIGURE = "Figure"
TYPE_TABLE = "Table"
TYPE_SOURCE = "Source"
TYPE_FULLTEXT = "FullText"
TYPE_INDEX = "Directory"


@dataclass
class OKFWriteResult:
    bundle_root: Path
    concepts: List[Dict[str, Any]] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    file_count: int = 0


def _actor() -> Dict[str, str]:
    return {"by": GENERATOR, "at": utc_now()}


def _source_entry(relative_pdf: str, title: str) -> List[Dict[str, Any]]:
    return [{
        "id": "pdf",
        "resource": relative_pdf,
        "title": title or "原始 PDF",
        "author": "human:unknown",
    }]


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# individual document builders
# ---------------------------------------------------------------------------

def _paper_frontmatter(paper: Dict[str, Any], hash_value: str,
                       relative_pdf: str) -> Dict[str, Any]:
    return {
        "type": TYPE_PAPER,
        "title": paper.get("title") or "未命名论文",
        "description": _one_line(paper.get("abstract")) or "论文结构化记录",
        "resource": relative_pdf,
        "tags": [tag for tag in (paper.get("keywords") or [])][:12],
        "okf_version": OKF_VERSION,
        "paper_hash": hash_value,
        "title_original": paper.get("title_original") or "",
        "journal": paper.get("journal") or "",
        "venue_type": paper.get("venue_type") or "",
        "published_at": paper.get("published_at") or "",
        "year": paper.get("year"),
        "doi": paper.get("doi") or "",
        "url": paper.get("url") or "",
        "language": paper.get("language") or "en",
        "authors": [
            {
                "name": item.get("name", ""),
                "affiliations": [
                    entry.get("name") if isinstance(entry, dict) else str(entry)
                    for entry in (item.get("affiliations") or [])
                ],
                "corresponding": bool(item.get("is_corresponding") or item.get("corresponding")),
                "email": item.get("email", ""),
            }
            for item in (paper.get("authors") or [])
        ],
        "affiliations": [
            {
                "name": item.get("name", ""),
                "department": item.get("department", ""),
                "country": item.get("country", ""),
            }
            for item in (paper.get("affiliations") or [])
        ],
        "status": "stable",
        "generated": _actor(),
        "sources": _source_entry(relative_pdf, paper.get("title") or ""),
    }


def _paper_body(paper: Dict[str, Any], sections: Sequence[Dict[str, Any]],
                figures: Sequence[Dict[str, Any]], tables: Sequence[Dict[str, Any]]) -> str:
    lines: List[str] = []

    lines.append("# 摘要\n")
    lines.append(paper.get("abstract") or "（原文未提供摘要）")
    lines.append("")

    if paper.get("authors"):
        lines.append("# 作者与单位\n")
        for index, author in enumerate(paper["authors"], start=1):
            names = [
                entry.get("name") if isinstance(entry, dict) else str(entry)
                for entry in (author.get("affiliations") or [])
            ]
            suffix = f"（{'；'.join(name for name in names if name)}）" if names else ""
            marker = "，通讯作者" if author.get("is_corresponding") else ""
            lines.append(f"{index}. {author.get('name', '')}{suffix}{marker}")
        lines.append("")

    if paper.get("keywords"):
        lines.append("# 关键词\n")
        lines.append("、".join(paper["keywords"]))
        lines.append("")

    if sections:
        lines.append("# 章节\n")
        for section in sections:
            link = f"/sections/{section['slug']}.md"
            lines.append(f"* [{section.get('title') or section['slug']}]({link})")
        lines.append("")

    if figures or tables:
        lines.append("# 图表\n")
        for item in figures:
            lines.append(f"* [{item['label']}](/figures/{item['label']}.md) - {_one_line(item.get('caption'))}")
        for item in tables:
            lines.append(f"* [{item['label']}](/tables/{item['label']}.md) - {_one_line(item.get('caption'))}")
        lines.append("")

    if paper.get("contributions"):
        lines.append("# 主要贡献\n")
        lines.extend(f"* {item}" for item in paper["contributions"])
        lines.append("")

    if paper.get("limitations"):
        lines.append("# 不足与局限\n")
        lines.extend(f"* {item}" for item in paper["limitations"])
        lines.append("")

    if paper.get("future_work"):
        lines.append("# 未来工作\n")
        lines.extend(f"* {item}" for item in paper["future_work"])
        lines.append("")

    lines.append("# 溯源\n")
    lines.append("正文与元数据抽取自 [原始 PDF](../paper.pdf)。")
    return "\n".join(lines).strip() + "\n"


def _section_frontmatter(section: Dict[str, Any], hash_value: str,
                         relative_pdf: str) -> Dict[str, Any]:
    slug = section["slug"]
    return {
        "type": TYPE_SECTION,
        "title": section.get("title") or label_of(slug),
        "description": _one_line(section.get("text")),
        "section": slug,
        "section_label": label_of(slug),
        "order": order_index(slug),
        "paper_hash": hash_value,
        "tags": [slug],
        "status": "stable",
        "generated": _actor(),
        "sources": _source_entry(relative_pdf, "原始 PDF"),
    }


def _section_body(section: Dict[str, Any], assets: Sequence[Dict[str, Any]]) -> str:
    lines: List[str] = [f"# {section.get('title') or label_of(section['slug'])}\n"]
    lines.append(section.get("text") or "（本节未抽取到正文）")
    lines.append("")
    if assets:
        lines.append("# 本节图表\n")
        for item in assets:
            folder = "figures" if item["kind"] == "figure" else "tables"
            caption = _one_line(item.get("caption")) or item["label"]
            lines.append(f"* [{item['label']}](/{folder}/{item['label']}.md) - {caption}")
        lines.append("")
    lines.append("# 说明\n")
    lines.append(
        f"本节在知识图谱中挂载到「{label_of(section['slug'])}」维度，"
        "对应 RAG 语义库的 B 层要点与 A 层详细大意。"
    )
    return "\n".join(lines).strip() + "\n"


def _asset_frontmatter(asset: Dict[str, Any], hash_value: str,
                       relative_asset: str) -> Dict[str, Any]:
    kind = asset["kind"]
    label = asset["label"]
    frontmatter: Dict[str, Any] = {
        "type": TYPE_FIGURE if kind == "figure" else TYPE_TABLE,
        "title": label,
        "description": _one_line(asset.get("caption")) or f"{label}（未识别到图注）",
        "resource": relative_asset,
        "section": asset.get("section") or "",
        "section_label": label_of(asset.get("section") or "") if asset.get("section") else "",
        "paper_hash": hash_value,
        "asset_label": label,
        "asset_kind": kind,
        "page": asset.get("page") or 0,
        "tags": [kind, label],
        "status": "stable",
        "generated": _actor(),
    }
    if asset.get("caption_source"):
        frontmatter["caption_source"] = asset["caption_source"]
    return frontmatter


def _asset_body(asset: Dict[str, Any], asset_relative: str, file_exists: bool) -> str:
    lines: List[str] = []
    lines.append("# 图注\n")
    lines.append(asset.get("caption") or "（未识别到图注）")
    lines.append("")
    if asset.get("content_text"):
        lines.append("# 内容\n")
        lines.append(asset["content_text"])
        lines.append("")
    lines.append("# 文件\n")
    if file_exists:
        lines.append(f"[{asset['label']}]({asset_relative})")
    else:
        lines.append(f"该{('图' if asset['kind'] == 'figure' else '表')}的裁剪文件缺失，仅保留模型描述。")
    lines.append("")
    if asset.get("section"):
        lines.append("# 关联章节\n")
        lines.append(f"属于「{label_of(asset['section'])}」维度。")
    return "\n".join(lines).strip() + "\n"


def _one_line(text: Optional[str], max_length: int = 160) -> str:
    if not text:
        return ""
    flat = " ".join(str(text).split())
    return flat if len(flat) <= max_length else flat[: max_length - 1] + "…"


def _fulltext_body(page_texts: Sequence[str]) -> str:
    """Render the full per-page text verbatim, page-marked, nothing removed.

    Only the page separators are added; the text itself is written exactly as
    PyMuPDF returned it (whitespace at the ends trimmed so the bundle stays
    readable, but no words are dropped).
    """
    lines: List[str] = ["# 全文\n", "> 以下正文由 PyMuPDF 从 PDF 逐页抽取，未做删减或改写。\n"]
    numbered = [text for text in page_texts if (text or "").strip()]
    if not numbered:
        return "\n".join(lines) + "\n（未抽取到正文）\n"
    for index, text in enumerate(numbered, start=1):
        lines.append(f"## 第 {index} 页\n")
        lines.append(text.strip())
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# bundle assembly
# ---------------------------------------------------------------------------

def write_bundle(
    paths: DataPaths,
    hash_value: str,
    *,
    paper: Dict[str, Any],
    sections: Sequence[Dict[str, Any]],
    assets: Sequence[Dict[str, Any]],
    bundle_root: Optional[Path] = None,
    full_page_texts: Sequence[str] = (),
) -> OKFWriteResult:
    """Write the complete OKF bundle for one paper.

    ``sections`` must already carry a ``slug`` and ``section`` (spine) value;
    ``assets`` must carry ``label``, ``kind`` and optionally ``section``.

    ``full_page_texts`` is the complete per-page text extracted by PyMuPDF. It is
    stored verbatim as ``fulltext.md`` — OKF holds the *faithful* record, never a
    digest — while the model only contributes structure (section split, metadata,
    figure/table descriptions). Passing the raw text means no content is dropped.
    """
    root = Path(bundle_root) if bundle_root else paths.paper_okf_dir(hash_value)
    root.mkdir(parents=True, exist_ok=True)
    result = OKFWriteResult(bundle_root=root)

    # Relative path from the bundle root to the sibling PDF.
    relative_pdf = "../paper.pdf"

    # ---- sections ------------------------------------------------------
    section_entries: List[Tuple[str, str, str]] = []
    written_sections: List[Dict[str, Any]] = []
    slug_counts: Dict[str, int] = {}

    for section in sections:
        base_slug = section.get("slug") or slugify(section.get("title") or "section")
        # Two headings can slugify identically ("3.1 Method" / "Method"); suffix
        # rather than overwrite, so no content is lost.
        count = slug_counts.get(base_slug, 0)
        slug_counts[base_slug] = count + 1
        slug = base_slug if count == 0 else f"{base_slug}-{count + 1}"

        section_assets = [
            item for item in assets
            if item.get("section") and _section_matches(item.get("section"), section.get("section"))
        ]
        payload = dict(section)
        payload["slug"] = slug
        path = root / "sections" / f"{slug}.md"
        _write(path, dump_concept(
            _section_frontmatter(payload, hash_value, "../../paper.pdf"),
            _section_body(payload, section_assets),
        ))
        written_sections.append(payload)
        section_entries.append((
            payload.get("title") or label_of(payload.get("section") or slug),
            f"/sections/{slug}.md",
            _one_line(payload.get("text"), 80),
        ))
        result.concepts.append({
            "concept_id": f"sections/{slug}",
            "type": TYPE_SECTION,
            "title": payload.get("title") or label_of(payload.get("section") or slug),
            "description": _one_line(payload.get("text")),
            "section": payload.get("section") or "",
            "file_path": paths.to_relative(path),
            "ordinal": len(result.concepts) + 1,
        })

    _write(root / "sections" / "index.md", build_index_markdown([("章节", section_entries)]))

    # ---- figures -------------------------------------------------------
    figure_entries: List[Tuple[str, str, str]] = []
    for asset in [item for item in assets if item.get("kind") == "figure"]:
        label = asset["label"]
        path = root / "figures" / f"{label}.md"
        relative_asset = f"../../fig/{label}.png"
        file_exists = _asset_file_exists(paths, hash_value, label)
        _write(path, dump_concept(
            _asset_frontmatter(asset, hash_value, relative_asset),
            _asset_body(asset, relative_asset, file_exists),
        ))
        figure_entries.append((label, f"/figures/{label}.md", _one_line(asset.get("caption"), 80)))
        result.concepts.append({
            "concept_id": f"figures/{label}",
            "type": TYPE_FIGURE,
            "title": label,
            "description": _one_line(asset.get("caption")),
            "section": asset.get("section") or "",
            "file_path": paths.to_relative(path),
            "ordinal": len(result.concepts) + 1,
        })
    _write(root / "figures" / "index.md", build_index_markdown([("图", figure_entries)]))

    # ---- tables --------------------------------------------------------
    table_entries: List[Tuple[str, str, str]] = []
    for asset in [item for item in assets if item.get("kind") == "table"]:
        label = asset["label"]
        path = root / "tables" / f"{label}.md"
        relative_asset = f"../../tab/{label}.png"
        file_exists = _asset_file_exists(paths, hash_value, label)
        _write(path, dump_concept(
            _asset_frontmatter(asset, hash_value, relative_asset),
            _asset_body(asset, relative_asset, file_exists),
        ))
        table_entries.append((label, f"/tables/{label}.md", _one_line(asset.get("caption"), 80)))
        result.concepts.append({
            "concept_id": f"tables/{label}",
            "type": TYPE_TABLE,
            "title": label,
            "description": _one_line(asset.get("caption")),
            "section": asset.get("section") or "",
            "file_path": paths.to_relative(path),
            "ordinal": len(result.concepts) + 1,
        })
    _write(root / "tables" / "index.md", build_index_markdown([("表", table_entries)]))

    # ---- full text (faithful, unmodified) -------------------------------
    # OKF stores the full text; the model only supplies structure. The raw text
    # comes straight from PyMuPDF so no content is ever truncated or summarised.
    fulltext_body = _fulltext_body(full_page_texts)
    _write(root / "fulltext.md", dump_concept({
        "type": TYPE_FULLTEXT,
        "title": "全文",
        "description": "论文完整正文，按页标记，来自 PyMuPDF 文本抽取，未做删减或改写",
        "paper_hash": hash_value,
        "page_count": len([t for t in full_page_texts if (t or "").strip()]),
        "tags": ["fulltext"],
        "status": "stable",
        "generated": _actor(),
        "sources": _source_entry(relative_pdf, paper.get("title") or ""),
    }, fulltext_body))
    result.concepts.append({
        "concept_id": "fulltext",
        "type": TYPE_FULLTEXT,
        "title": "全文",
        "description": "论文完整正文（按页标记，未删减）",
        "section": "",
        "file_path": paths.to_relative(root / "fulltext.md"),
        "ordinal": 0,
    })

    # ---- references (spec 6.3) -----------------------------------------
    references_body = (
        "# 原始文件\n\n"
        "[paper.pdf](../../paper.pdf)\n\n"
        "该文件是论文的原始 PDF，内容与哈希一一对应，未经修改。"
    )
    _write(root / "references" / "pdf.md", dump_concept({
        "type": TYPE_SOURCE,
        "title": "原始 PDF",
        "description": "论文原始文件，作为全部概念的唯一溯源来源。",
        "resource": "../../paper.pdf",
        "paper_hash": hash_value,
        "status": "stable",
        "generated": _actor(),
    }, references_body))
    _write(root / "references" / "index.md",
           build_index_markdown([("原始材料", [("原始 PDF", "/references/pdf.md", "论文原文")])]))
    result.concepts.append({
        "concept_id": "references/pdf",
        "type": TYPE_SOURCE,
        "title": "原始 PDF",
        "description": "论文原始文件",
        "section": "",
        "file_path": paths.to_relative(root / "references" / "pdf.md"),
        "ordinal": 0,
    })

    # ---- paper ---------------------------------------------------------
    paper_path = root / "paper.md"
    _write(paper_path, dump_concept(
        _paper_frontmatter(paper, hash_value, relative_pdf),
        _paper_body(paper, written_sections, [a for a in assets if a.get("kind") == "figure"],
                    [a for a in assets if a.get("kind") == "table"]),
    ))
    result.concepts.insert(0, {
        "concept_id": "paper",
        "type": TYPE_PAPER,
        "title": paper.get("title") or "未命名论文",
        "description": _one_line(paper.get("abstract")),
        "section": "",
        "file_path": paths.to_relative(paper_path),
        "ordinal": 0,
    })

    # ---- index / log ---------------------------------------------------
    index_sections: List[Tuple[str, Sequence[Tuple[str, str, str]]]] = [
        ("论文", [("总览", "/paper.md", _one_line(paper.get("abstract"), 100))]),
        ("章节", section_entries),
        ("图", figure_entries),
        ("表", table_entries),
        ("原始材料", [("原始 PDF", "/references/pdf.md", "论文原文"),
                     ("全文", "/fulltext.md", "按页标记的完整正文")]),
    ]
    _write(root / "index.md", build_index_markdown(index_sections, okf_version=OKF_VERSION))

    today = utc_now()[:10]
    log_entries: List[Tuple[str, List[Tuple[str, str]]]] = [(today, [
        ("Creation", f"建立论文概念 [总览](/paper.md)，哈希 `{hash_value[:12]}`。"),
        ("Creation", f"抽取章节 {len(written_sections)} 节、图 {len(figure_entries)} 张、表 {len(table_entries)} 张。"),
        ("Creation", "按 OKF v0.2 规范生成 frontmatter 与交叉链接。"),
    ])]
    _write(root / "log.md", build_log_markdown(log_entries))

    result.file_count = len(list(root.rglob("*.md")))
    logger.info("OKF Bundle 写入完成：%s（%s 个文档）", root, result.file_count)
    return result


def _asset_file_exists(paths: DataPaths, hash_value: str, label: str) -> bool:
    try:
        return paths.asset_file(hash_value, label).is_file()
    except Exception:  # pragma: no cover - defensive
        return False


def _section_matches(asset_section: str, section_slug: str) -> bool:
    if not asset_section or not section_slug:
        return False
    return str(asset_section) == str(section_slug)


def assign_asset_sections(
    assets: Sequence[Dict[str, Any]],
    sections: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Attach a spine section to each asset.

    Preference order: an explicit section already present, then the page number
    (the section whose page range covers it), then the nearest preceding
    section. Page-based attribution works because the parser reports the page a
    caption appears on.
    """
    if not sections:
        return [dict(item) for item in assets]

    ranges: List[Tuple[int, int, str]] = []
    ordered = sorted(sections, key=lambda item: int(item.get("ordinal", 0)))
    for index, section in enumerate(ordered):
        start = int(section.get("page_from") or 0)
        end = int(section.get("page_to") or 0)
        if start:
            ranges.append((start, end or start, section.get("section") or ""))

    out: List[Dict[str, Any]] = []
    for asset in assets:
        entry = dict(asset)
        if entry.get("section"):
            out.append(entry)
            continue
        page = int(entry.get("page") or 0)
        chosen = ""
        if page and ranges:
            for start, end, slug in ranges:
                if start <= page <= end and slug:
                    chosen = slug
                    break
        if not chosen and ordered:
            if page:
                preceding = [item for item in ordered if int(item.get("page_from") or 0) <= page]
                if preceding:
                    chosen = preceding[-1].get("section") or ""
            if not chosen:
                chosen = ordered[0].get("section") or ""
        entry["section"] = chosen
        out.append(entry)
    return out
