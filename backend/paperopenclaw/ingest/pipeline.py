"""Ingest pipeline: PDF bytes in, four artefacts out (FR-1, FR-3 to FR-7).

Stages, each publishing progress on the job's event topic (NFR-5)::

    ① receive    hash the bytes, deduplicate, create the paper row + job
    ② layout     PyMuPDF detects and crops figures/tables into fig/ and tab/
    ③ vision     the parse model reads the PDF and returns structured content
    ④ okf        the faithful record is written as an OKF v0.2 bundle
    ⑤ pyramid    layers A/B/C are generated, linked and vectorised
    ⑥ kg         the fishbone graph is built from layer B
    ⑦ finalize   meta.json, status, job completion

Failure semantics: any stage marks the paper ``failed`` with the error recorded
and leaves already-produced artefacts in place, so "重新解析" can resume from a
later stage instead of starting over.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from ..config import SettingsStore
from ..core.errors import AppError, DuplicatePaper, ParseFailed, ProviderNotConfigured
from ..core.events import bus, job_topic
from ..core.hashing import hash_bytes, short_hash, stable_id
from ..core.logging import get_logger
from ..core.paths import DataPaths
from ..llm.base import LLMError
from ..llm.registry import KIND_PARSE, ProviderRegistry
from ..memory.embeddings import EmbeddingProvider
from ..memory.vector_store import VectorStore, VectorStoreError
from ..storage.db import utc_now
from ..storage.repos.jobs import JobRepository
from ..storage.repos.kg import KnowledgeGraphRepository
from ..storage.repos.fishbone import FishboneRepository
from ..storage.repos.papers import PaperRepository
from ..storage.repos.rag import RagRepository
from .fishbone_builder import FishboneBuilder
from .kg_builder import KnowledgeGraphBuilder
from .okf_writer import OKFWriteResult, assign_asset_sections, write_bundle
from .pdf_layout import LayoutReport, analyse_pdf
from .pyramid import PyramidBuilder, PyramidResult
from .vision_parse import ParsedPaper, VisionParser, local_parse

logger = get_logger(__name__)

STAGE_TOTAL = 7


class IngestPipeline:
    """Orchestrates the seven stages for a single PDF."""

    def __init__(
        self,
        *,
        paths: DataPaths,
        settings: SettingsStore,
        papers: PaperRepository,
        rag: RagRepository,
        kg: KnowledgeGraphRepository,
        fishbone: FishboneRepository,
        jobs: JobRepository,
        registry: ProviderRegistry,
        vectors: VectorStore,
        embedder_provider: Callable[[], EmbeddingProvider],
    ) -> None:
        self.paths = paths
        self.settings = settings
        self.papers = papers
        self.rag = rag
        self.kg = kg
        self.fishbone = fishbone
        self.jobs = jobs
        self.registry = registry
        self.vectors = vectors
        self._embedder_provider = embedder_provider

    @property
    def embedder(self) -> EmbeddingProvider:
        return self._embedder_provider()

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _publish(self, job_id: str, name: str, **payload: Any) -> None:
        bus.publish(job_topic(job_id), name, payload)

    def _stage(self, job_id: str, stage: str, message: str, progress: Optional[float] = None) -> None:
        self.jobs.set_stage(job_id, stage, message, progress)
        self._publish(job_id, "stage", stage=stage, message=message,
                      stage_index=self.jobs.get(job_id).get("stage_index", 0) if self.jobs.get(job_id) else 0,
                      progress=progress)

    def _log(self, job_id: str, message: str, level: str = "info") -> None:
        self.jobs.set_message(job_id, message)
        self._publish(job_id, "log", level=level, message=message)

    def _check_cancelled(self, job_id: str) -> None:
        if self.jobs.is_cancelled(job_id):
            raise _Cancelled()

    # ==================================================================
    # public API
    # ==================================================================
    def ingest_bytes(
        self,
        data: bytes,
        *,
        filename: str = "paper.pdf",
        job_id: Optional[str] = None,
        from_stage: str = "layout",
        force: bool = False,
        use_llm: bool = True,
    ) -> Dict[str, Any]:
        """Run the full pipeline for one PDF and return a summary dict.

        ``use_llm=False`` forces the local PyMuPDF parse and skips the vision
        model entirely (fast, no API call, no metadata extraction).
        """
        content_hash = hash_bytes(data)
        job = self._prepare_job(content_hash, filename, job_id, force=force)
        job_id = job["id"]

        try:
            self.jobs.start(job_id)

            # ---- ② store + layout -------------------------------------
            self._stage(job_id, "receive", f"已计算哈希 {short_hash(content_hash)}")
            self.paths.ensure_paper_dirs(content_hash)
            pdf_path = self.paths.paper_pdf(content_hash)
            if not pdf_path.is_file() or force:
                pdf_path.write_bytes(data)
            self.papers.update(content_hash, file_path=self.paths.to_relative(pdf_path),
                               okf_path=self.paths.to_relative(self.paths.paper_okf_dir(content_hash)),
                               file_size=len(data))
            self.jobs.finish_stage(job_id, "receive", "文件已保存")

            self._check_cancelled(job_id)
            layout = self._run_layout(job_id, content_hash, data, force=force)
            self.jobs.finish_stage(
                job_id, "layout",
                f"图表裁剪完成：图 {sum(1 for a in layout['assets'] if a['kind'] == 'figure')} "
                f"表 {sum(1 for a in layout['assets'] if a['kind'] == 'table')}",
            )

            # ---- ③ vision parse ---------------------------------------
            self._check_cancelled(job_id)
            parsed = self._run_vision(job_id, data, filename, layout, use_llm=use_llm)
            self.jobs.finish_stage(job_id, "vision",
                                   f"解析完成：{len(parsed.sections)} 节正文")

            # ---- ④ OKF ------------------------------------------------
            self._check_cancelled(job_id)
            sections, assets, paper_row, okf = self._run_okf(
                job_id, content_hash, parsed, layout["assets"], layout.get("page_texts") or []
            )
            self.jobs.finish_stage(job_id, "okf", f"OKF 文档 {okf.file_count} 个")

            # ---- ⑤ pyramid --------------------------------------------
            self._check_cancelled(job_id)
            pyramid = self._run_pyramid(job_id, content_hash, paper_row, sections, assets)
            self.jobs.finish_stage(
                job_id, "pyramid",
                f"语义单元 A={pyramid.counts()['A']} B={pyramid.counts()['B']} C={pyramid.counts()['C']}",
            )

            # ---- ⑥ knowledge graph ------------------------------------
            self._check_cancelled(job_id)
            graph = self._run_kg(job_id, content_hash, paper_row, assets, pyramid)
            self.jobs.finish_stage(job_id, "kg",
                                   f"图谱节点 {graph['nodes']}，边 {graph['edges']}")

            # ---- ⑦ finalize -------------------------------------------
            self._finalise(job_id, content_hash, parsed, layout, pyramid, graph, okf)
            self.jobs.finish_stage(job_id, "finalize", "入库完成")
            self.jobs.complete(job_id, "入库完成")
            self._publish(job_id, "done", status="done", hash=content_hash,
                          title=paper_row.get("title", ""))

            return {
                "hash": content_hash,
                "hash_short": short_hash(content_hash),
                "title": paper_row.get("title", ""),
                "job_id": job_id,
                "counts": self.papers.counts_for(content_hash),
            }

        except _Cancelled:
            self.jobs.cancel(job_id)
            self._publish(job_id, "done", status="cancelled", hash=content_hash)
            self.papers.set_status(content_hash, "pending", "任务已取消")
            return {"hash": content_hash, "job_id": job_id, "cancelled": True}
        except (AppError, LLMError, VectorStoreError, Exception) as exc:  # noqa: BLE001
            message = _describe(exc)
            logger.exception("入库失败 %s：%s", short_hash(content_hash), message)
            self.jobs.fail(job_id, message)
            self.papers.set_status(content_hash, "failed", message)
            self._publish(job_id, "error", code=getattr(exc, "code", "INTERNAL_ERROR"),
                          message=message)
            raise

    def ingest_path(self, path: Path, **kwargs: Any) -> Dict[str, Any]:
        return self.ingest_bytes(Path(path).read_bytes(), filename=Path(path).name, **kwargs)

    def reparse(self, hash_value: str, *, from_stage: str = "vision", force: bool = True) -> Dict[str, Any]:
        """Re-run later stages for a paper already on disk."""
        pdf_path = self.paths.paper_pdf(hash_value)
        if not pdf_path.is_file():
            from ..core.errors import PaperNotFound

            raise PaperNotFound(f"论文文件不存在：{hash_value}")
        data = pdf_path.read_bytes()
        paper = self.papers.get(hash_value)
        filename = (paper or {}).get("title") or "paper.pdf"
        return self.ingest_bytes(data, filename=f"{filename}.pdf", force=force,
                                 from_stage=from_stage)

    def delete(self, hash_value: str) -> Dict[str, Any]:
        """Remove a paper from every store (FR-13.5)."""
        removed: Dict[str, Any] = {"hash": hash_value, "vectors": 0, "files": False}
        try:
            removed["vectors"] = self.vectors.delete_paper(hash_value)
        except VectorStoreError as exc:
            logger.warning("删除向量失败（继续）：%s", exc)
        # 图库：关键词节点是全局共享的，不随 FK 级联，需显式清理孤儿节点。
        self.kg.delete_paper(hash_value)
        self.fishbone.delete_paper(hash_value)
        # Relational rows cascade from `papers`.
        removed["deleted"] = self.papers.delete(hash_value)
        directory = self.paths.paper_dir(hash_value)
        if directory.is_dir():
            shutil.rmtree(str(directory), ignore_errors=True)
            removed["files"] = True
        logger.info("已删除论文 %s", short_hash(hash_value))
        return removed

    # ==================================================================
    # stages
    # ==================================================================
    def _prepare_job(self, content_hash: str, filename: str, job_id: Optional[str],
                     force: bool) -> Dict[str, Any]:
        if not force and self.papers.exists(content_hash):
            paper = self.papers.get(content_hash) or {}
            if paper.get("status") == "ready":
                raise DuplicatePaper(content_hash, paper.get("title") or filename)
        identifier = job_id or f"job_{stable_id(content_hash, utc_now(), length=12)}"
        existing = self.jobs.get(identifier)
        if existing is None:
            self.jobs.create(identifier, kind="ingest", hash_value=content_hash,
                             filename=filename, message="任务已创建")
        else:
            self.jobs.set_message(identifier, "任务重新开始")
        self._stage(identifier, "receive", "已接收文件")
        self.papers.create(content_hash, title=filename.rsplit(".", 1)[0],
                           file_path=self.paths.to_relative(self.paths.paper_pdf(content_hash)),
                           okf_path=self.paths.to_relative(self.paths.paper_okf_dir(content_hash)),
                           status="parsing")
        return {"id": identifier}

    def _run_layout(self, job_id: str, content_hash: str, data: bytes,
                    force: bool = False) -> Dict[str, Any]:
        config = self.settings.current.ingest
        self._stage(job_id, "layout", "正在进行版面分析与图表裁剪")
        report: LayoutReport = analyse_pdf(
            data,
            dpi=config.image_dpi,
            min_area_ratio=config.min_asset_area_ratio,
            caption_window=float(config.caption_search_window),
            max_pages=config.max_pages,
        )

        assets: List[Dict[str, Any]] = []
        for cropped in report.assets:
            target = self.paths.asset_file(content_hash, cropped.label)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(cropped.data)
            assets.append(cropped.to_row(self.paths.to_relative(target)))

        self.papers.replace_assets(content_hash, assets)
        stored = self.papers.list_assets(content_hash)
        self.papers.update(content_hash, page_count=report.page_count)

        logger.info("版面分析：%s 页，%s 个图表", report.page_count, len(assets))
        return {
            "page_count": report.page_count,
            "assets": stored,
            "detected": report.detected_summary(),
            "text_excerpt": report.text_excerpt(),
            # 完整逐页正文，供 OKF fulltext.md 原样存储（不删减）。
            "page_texts": report.page_texts,
            "warnings": report.warnings,
        }

    def _run_vision(self, job_id: str, data: bytes, filename: str,
                    layout: Dict[str, Any], use_llm: bool = True) -> ParsedPaper:
        self._stage(job_id, "vision", "正在将 PDF 渲染为 PNG 并调用大模型解析")
        parser = VisionParser(self.registry, self.settings)

        def progress(message: str, ratio: float) -> None:
            self._log(job_id, message)
            self.jobs.set_message(job_id, message, progress=0.25 + 0.4 * min(1.0, ratio))
            self._publish(job_id, "progress", message=message, progress=ratio)

        # 用户在上传页关闭了「使用 LLM 解析」→ 直接本地解析。
        if not use_llm:
            reason = "已关闭 LLM 解析，直接使用本地 PyMuPDF 解析"
            logger.warning(reason)
            self._log(job_id, reason, level="warning")
            self._publish(job_id, "log", level="warning", message=reason)
            return self._local_parse(job_id, data, layout, filename)

        # 显式判断：未配置解析模型 / 模型不支持图像 → 直接本地解析，不再尝试 LLM。
        reason = self._vision_fallback_reason()
        if reason:
            logger.warning(reason)
            self._log(job_id, reason, level="warning")
            self._publish(job_id, "log", level="warning", message=reason)
            return self._local_parse(job_id, data, layout, filename)

        try:
            parsed = parser.parse(
                data,
                filename=filename,
                detected_assets=layout.get("detected") or [],
                text_excerpt=layout.get("text_excerpt") or "",
                progress=progress,
            )
        except Exception as exc:  # noqa: BLE001 - degrade to a local, non-LLM parse
            # PNG -> LLM 失败：回退到本地 PyMuPDF 抽取，保证入库仍能完成。
            logger.warning("视觉模型解析失败（%s），回退到本地 PyMuPDF 解析", exc)
            self._log(job_id, f"视觉模型解析失败，已回退到本地解析：{exc}", level="warning")
            self._publish(job_id, "log", level="warning",
                          message=f"视觉模型解析失败，已回退到本地解析：{exc}")
            return self._local_parse(job_id, data, layout, filename)

        self.jobs.add_usage(job_id, parsed.usage)
        for warning in parsed.warnings:
            self._log(job_id, warning, level="warning")
        return parsed

    def _vision_fallback_reason(self) -> Optional[str]:
        """返回「为何跳过 LLM 解析、直接本地解析」的原因；无需跳转则返回 None。"""
        try:
            route = self.registry.route_config(KIND_PARSE)
        except Exception:  # noqa: BLE001 - defensive
            return "未配置解析模型，直接使用本地 PyMuPDF 解析"
        if not getattr(route, "configured", False):
            return "未配置解析模型，直接使用本地 PyMuPDF 解析"
        provider = self.settings.current.provider(getattr(route, "provider", ""))
        # 纯文本模型读不了 PDF（无论直传文件还是逐页图像），一律直接本地解析。
        if provider is not None and not provider.vision:
            model = getattr(route, "model", "") or getattr(route, "provider", "")
            return f"解析模型 {model} 不支持 PDF/图像输入，直接使用本地 PyMuPDF 解析"
        return None

    def _local_parse(self, job_id: str, data: bytes, layout: Dict[str, Any],
                     filename: str) -> ParsedPaper:
        parsed = local_parse(
            data,
            page_texts=layout.get("page_texts") or [],
            filename=filename,
        )
        self.jobs.add_usage(job_id, parsed.usage)
        for warning in parsed.warnings:
            self._log(job_id, warning, level="warning")
        return parsed

    def _run_okf(
        self,
        job_id: str,
        content_hash: str,
        parsed: ParsedPaper,
        layout_assets: Sequence[Dict[str, Any]],
        full_page_texts: Sequence[str] = (),
    ) -> tuple:
        self._stage(job_id, "okf", "正在写入 OKF 文档")

        sections = _prepare_sections(parsed, layout_assets)
        from .vision_parse import reconcile_assets

        merged_assets = reconcile_assets(layout_assets, parsed.figures, parsed.tables)
        merged_assets = assign_asset_sections(merged_assets, sections)

        # Persist again so the DB holds the model-enriched captions and the
        # section attribution the graph needs.
        self.papers.replace_assets(content_hash, merged_assets)
        stored_assets = self.papers.list_assets(content_hash)

        paper_row = {
            "title": parsed.title,
            "title_original": parsed.title_original,
            "journal": parsed.journal,
            "venue_type": parsed.venue_type,
            "published_at": parsed.published_at,
            "year": parsed.year,
            "doi": parsed.doi,
            "url": parsed.url,
            "abstract": parsed.abstract,
            "keywords": parsed.keywords,
            "language": parsed.language,
            "authors": parsed.authors,
            "affiliations": parsed.affiliations,
            "contributions": parsed.contributions,
            "limitations": parsed.limitations,
            "future_work": parsed.future_work,
        }

        okf: OKFWriteResult = write_bundle(
            self.paths, content_hash,
            paper=paper_row,
            sections=sections,
            assets=stored_assets,
            full_page_texts=full_page_texts,
        )

        self.papers.replace_authors_and_affiliations(
            content_hash, parsed.authors, parsed.affiliations
        )
        self.papers.replace_okf_concepts(content_hash, okf.concepts)
        self.papers.update(
            content_hash,
            title=parsed.title or "",
            title_original=parsed.title_original or "",
            journal=parsed.journal or "",
            venue_type=parsed.venue_type or "",
            published_at=parsed.published_at or "",
            year=parsed.year,
            doi=parsed.doi or "",
            url=parsed.url or "",
            abstract=parsed.abstract or "",
            keywords=parsed.keywords,
            language=parsed.language or "en",
            okf_path=self.paths.to_relative(okf.bundle_root),
            status="indexing",
        )
        return sections, stored_assets, paper_row, okf

    def _run_pyramid(
        self,
        job_id: str,
        content_hash: str,
        paper_row: Dict[str, Any],
        sections: Sequence[Dict[str, Any]],
        assets: Sequence[Dict[str, Any]],
    ) -> PyramidResult:
        self._stage(job_id, "pyramid", "正在构建 A/B/C 金字塔语义库")
        builder = PyramidBuilder(self.registry, self.settings, self.rag,
                                 self.vectors, self.embedder)

        def progress(message: str, ratio: float) -> None:
            self._log(job_id, message)
            self._publish(job_id, "progress", message=message, progress=ratio)

        result = builder.build(
            content_hash,
            title=paper_row.get("title") or "",
            abstract=paper_row.get("abstract") or "",
            sections=sections,
            assets=assets,
            progress=progress,
        )
        builder.persist(content_hash, result)
        self.jobs.add_usage(job_id, result.usage)
        for warning in result.warnings[:20]:
            self._log(job_id, warning, level="warning")
        # Cross-paper C-C links (FR-6.6) are computed once the apex exists.
        try:
            links = builder.link_cross_papers(content_hash)
            if links:
                self._log(job_id, f"已建立跨论文 C 层关联 {links} 条")
        except Exception as exc:  # noqa: BLE001 - never fail ingest over links
            logger.warning("跨论文关联失败：%s", exc)
        return result

    def _run_kg(
        self,
        job_id: str,
        content_hash: str,
        paper_row: Dict[str, Any],
        assets: Sequence[Dict[str, Any]],
        pyramid: PyramidResult,
    ) -> Dict[str, Any]:
        self._stage(job_id, "kg", "正在构建鱼骨图与知识图谱")
        title = paper_row.get("title") or ""

        # ---- 6a. fishbone graph: LLM 从 OKF 全文提取（失败回退 B 层）---
        fishbone_builder = FishboneBuilder(self.papers, self.fishbone, self.rag)
        okf_fulltext = self._read_okf_fulltext(content_hash)
        digest_client = self.registry.try_client("digest") or self.registry.try_client("chat")
        fishbone_result = fishbone_builder.build_from_okf(
            content_hash,
            title=title,
            full_text=okf_fulltext,
            assets=assets,
            client=digest_client,
        )

        # ---- 6b. knowledge graph (keywords) from OKF via LLM -------------
        kg_builder = KnowledgeGraphBuilder(self.papers, self.kg, self.rag)
        kg_result = kg_builder.build_from_okf(
            content_hash,
            title=title,
            full_text=okf_fulltext,
            client=digest_client,
        )

        payload = {
            "fishbone": fishbone_result.to_dict(),
            "keywords": kg_result.to_dict(),
            "nodes": fishbone_result.nodes + kg_result.nodes,
            "edges": fishbone_result.edges + kg_result.edges,
        }
        for warning in (fishbone_result.warnings + kg_result.warnings)[:10]:
            self._log(job_id, warning, level="warning")
        return payload

    def _read_okf_fulltext(self, paper_hash: str) -> str:
        """Read the OKF ``fulltext.md`` body (the faithful per-page record)."""
        from ..memory.okf_fs import load_concept

        path = self.paths.paper_okf_dir(paper_hash) / "fulltext.md"
        if not path.is_file():
            return ""
        try:
            parsed = load_concept(path)
            return str(parsed.get("body") or "")
        except Exception:  # noqa: BLE001 - optional grounding source
            return ""

    def _finalise(
        self,
        job_id: str,
        content_hash: str,
        parsed: ParsedPaper,
        layout: Dict[str, Any],
        pyramid: PyramidResult,
        graph: Dict[str, Any],
        okf: OKFWriteResult,
    ) -> None:
        self._stage(job_id, "finalize", "正在写入解析快照")
        snapshot = {
            "hash": content_hash,
            "generated_at": utc_now(),
            "generator": "paperopenclaw/1.0.0",
            "provider": parsed.provider,
            "model": parsed.model,
            "page_count": layout.get("page_count", 0),
            "layout_warnings": layout.get("warnings", []),
            "parse_warnings": parsed.warnings,
            "counts": {
                "assets": len(layout.get("assets", [])),
                "sections": len(parsed.sections),
                "rag": pyramid.counts(),
                "kg": {"nodes": graph.get("nodes", 0), "edges": graph.get("edges", 0)},
                "okf_files": okf.file_count,
            },
            "contributions": parsed.contributions,
            "limitations": parsed.limitations,
            "future_work": parsed.future_work,
            "raw_responses": parsed.raw_responses[:3],
        }
        meta_path = self.paths.paper_meta(content_hash)
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        meta_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")

        self.papers.set_status(content_hash, "ready", "")
        self.papers.update(content_hash, okf_path=self.paths.to_relative(okf.bundle_root))


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

class _Cancelled(Exception):
    """Internal signal: the job was cancelled between stages."""


def _describe(exc: Exception) -> str:
    if isinstance(exc, AppError):
        return f"{exc.message}"
    if isinstance(exc, LLMError):
        return f"大模型调用失败：{exc}"
    if isinstance(exc, ProviderNotConfigured):
        return str(exc)
    if isinstance(exc, ParseFailed):
        return str(exc)
    return f"{type(exc).__name__}: {exc}"


def _prepare_sections(parsed: ParsedPaper,
                      layout_assets: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Give every section a spine slug and a stable file slug.

    Sections arrive with a rule- or model-assigned ``section``; anything still
    unmapped inherits the nearest preceding dimension so the paper's flow is
    preserved rather than dumped into one bucket (see ``assign_sections``).
    """
    from ..core.text import slugify
    from ..kg.skeleton import assign_sections

    prepared: List[Dict[str, Any]] = []
    for index, section in enumerate(parsed.sections):
        prepared.append({
            "title": section.get("title") or f"第 {index + 1} 节",
            "text": section.get("text") or "",
            "section": section.get("section") or "",
            "ordinal": index,
        })
    prepared = assign_sections(prepared)

    used: Dict[str, int] = {}
    for section in prepared:
        base = slugify(section.get("title") or section.get("section") or "section")
        count = used.get(base, 0)
        used[base] = count + 1
        section["slug"] = base if count == 0 else f"{base}-{count + 1}"

    # Attach a coarse page range so assets can be attributed to sections.
    if prepared and layout_assets:
        total_pages = max((int(item.get("page") or 0) for item in layout_assets), default=0)
        per_section = max(1, (total_pages or len(prepared)) // max(1, len(prepared)))
        for index, section in enumerate(prepared):
            section["page_from"] = index * per_section + 1
            section["page_to"] = (index + 1) * per_section
    return prepared
