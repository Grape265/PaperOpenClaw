"""Building the fishbone graph from the OKF full text (FR-7.1/7.2/7.4).

The fishbone is the seven-dimension skeleton. Its points are extracted from the
faithful OKF record with an LLM call, one node per point, with figures/tables
mounted as URL links. When no LLM is available it falls back to the layer-B
points so the graph is never left empty.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..core.jsonutil import JsonExtractionError, extract_json
from ..core.logging import get_logger
from ..kg.skeleton import SPINE_SLUGS, label_of, order_index
from ..llm.base import Message
from ..llm.prompts import fishbone as fishbone_prompts
from ..storage.repos.fishbone import (
    FishboneRepository,
    asset_key,
    content_key,
    paper_key,
    spine_key,
)
from ..storage.repos.papers import PaperRepository
from ..storage.repos.rag import RagRepository

logger = get_logger(__name__)


def asset_url(hash_value: str, label: str) -> str:
    return f"/api/v1/papers/{hash_value}/assets/{label}"


@dataclass
class FishboneBuildResult:
    nodes: int = 0
    edges: int = 0
    sections: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    source: str = "b"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "nodes": self.nodes,
            "edges": self.edges,
            "sections": self.sections,
            "warnings": self.warnings,
            "source": self.source,
        }


class FishboneBuilder:
    """Turns the OKF record (via LLM, falling back to layer B) into fishbone nodes."""

    def __init__(self, papers: PaperRepository, fishbone: FishboneRepository,
                 rag: RagRepository) -> None:
        self.papers = papers
        self.fishbone = fishbone
        self.rag = rag

    def build(
        self,
        paper_hash: str,
        *,
        title: str = "",
        assets: Sequence[Dict[str, Any]] = (),
        b_units: Optional[Sequence[Dict[str, Any]]] = None,
    ) -> FishboneBuildResult:
        """Build from layer-B points (the no-LLM fallback)."""
        units = list(b_units) if b_units is not None else [
            unit for unit in self.rag.units_of_paper(paper_hash, "B")
        ]
        dimensions: List[Tuple[str, List[Dict[str, Any]], float]] = []
        result = FishboneBuildResult(source="b")
        for unit in sorted(units, key=lambda item: order_index(item.get("section") or "")):
            slug = unit.get("section") or ""
            if slug not in SPINE_SLUGS:
                if not slug:
                    continue
                slug = "background"
            points = (unit.get("meta") or {}).get("points") or []
            if not points:
                points = [{"text": unit.get("text") or "", "assets": []}]
            importance = float((unit.get("meta") or {}).get("importance", 0.5))
            dimensions.append((slug, points, importance))
        return self._build_nodes(paper_hash, title, assets, dimensions, result)

    def build_from_okf(
        self,
        paper_hash: str,
        *,
        title: str = "",
        full_text: str = "",
        assets: Sequence[Dict[str, Any]] = (),
        client: Any = None,
    ) -> FishboneBuildResult:
        """Extract fishbone points from the OKF full text with an LLM.

        Falls back to layer-B points when the client is missing or the call fails.
        """
        result = FishboneBuildResult(source="okf")
        if client is None or not (full_text or "").strip():
            if not (full_text or "").strip():
                result.warnings.append("OKF 全文为空，回退到 B 层要点")
            else:
                result.warnings.append("未配置摘要模型，回退到 B 层要点")
            return self.build(paper_hash, title=title, assets=assets)

        asset_labels = [a.get("label") for a in assets if a.get("label")]
        try:
            response = client.chat(
                [Message.system(fishbone_prompts.fishbone_system_prompt()),
                 Message.user(fishbone_prompts.fishbone_user_prompt(
                     title, full_text, asset_labels))],
                temperature=0.2,
                max_tokens=4096,
            )
            payload = extract_json(response.text, expect="object")
        except (JsonExtractionError, Exception) as exc:  # noqa: BLE001 - degrade
            logger.warning("从 OKF 提取鱼骨失败（%s），回退 B 层要点", exc)
            result.warnings.append(f"从 OKF 提取鱼骨失败，已回退 B 层要点：{exc}")
            fallback = self.build(paper_hash, title=title, assets=assets)
            fallback.warnings = result.warnings + fallback.warnings
            return fallback

        dimensions: List[Tuple[str, List[Dict[str, Any]], float]] = []
        for item in payload.get("sections") or []:
            slug = str(item.get("section") or "").lower()
            if slug not in SPINE_SLUGS:
                continue
            points = []
            for point in item.get("points") or []:
                text = str(point.get("text") or "").strip()
                if not text:
                    continue
                points.append({
                    "text": text,
                    "assets": [str(a) for a in (point.get("assets") or [])],
                })
            if points:
                dimensions.append((slug, points, 0.5))
        return self._build_nodes(paper_hash, title, assets, dimensions, result)

    def _build_nodes(
        self,
        paper_hash: str,
        title: str,
        assets: Sequence[Dict[str, Any]],
        dimensions: Sequence[Tuple[str, List[Dict[str, Any]], float]],
        result: FishboneBuildResult,
    ) -> FishboneBuildResult:
        paper = self.papers.get(paper_hash)
        if paper and not title:
            title = paper.get("title") or ""

        self.fishbone.delete_paper(paper_hash)
        self.fishbone.ensure_spine_nodes()

        nodes: List[Dict[str, Any]] = [{
            "node_key": paper_key(paper_hash),
            "kind": "paper",
            "paper_hash": paper_hash,
            "label": title or paper_hash[:12],
            "detail": (paper or {}).get("abstract", "")[:2000],
            "ordinal": 0,
            "meta": {"journal": (paper or {}).get("journal", ""),
                     "year": (paper or {}).get("year"),
                     "url": f"/api/v1/papers/{paper_hash}"},
        }]
        edges: List[Tuple[str, str, str, float, Optional[Dict[str, Any]]]] = []

        asset_keys: Dict[str, str] = {}
        for asset in assets:
            label = asset.get("label")
            if not label:
                continue
            key = asset_key(paper_hash, label)
            asset_keys[label] = key
            relative = asset.get("file_path") or ""
            nodes.append({
                "node_key": key,
                "kind": "asset",
                "paper_hash": paper_hash,
                "section": asset.get("section") or "",
                "label": label,
                "detail": (asset.get("caption") or "")[:1000],
                "ordinal": int(asset.get("ordinal") or 0),
                "asset_id": asset.get("id"),
                "meta": {
                    "asset_kind": asset.get("kind") or "figure",
                    "path": relative,
                    "url": asset_url(paper_hash, label),
                    "page": asset.get("page") or 0,
                    "missing_file": bool(asset.get("missing_file")),
                },
            })

        present_sections: List[str] = []
        for slug, points, importance in dimensions:
            if slug not in SPINE_SLUGS:
                slug = "background"
            if slug not in present_sections:
                present_sections.append(slug)
            edges.append((paper_key(paper_hash), spine_key(slug), "has_spine", 1.0,
                          {"section": slug}))

            for index, point in enumerate(points, start=1):
                text = str(point.get("text") or "").strip()
                if not text:
                    continue
                key = content_key(paper_hash, slug, index)
                point_assets = [label for label in (point.get("assets") or []) if label in asset_keys]
                nodes.append({
                    "node_key": key,
                    "kind": "content",
                    "paper_hash": paper_hash,
                    "section": slug,
                    "label": text,
                    "detail": text,
                    "ordinal": index,
                    "unit_id": "",
                    "meta": {
                        "section_label": label_of(slug),
                        "asset_refs": point_assets,
                        "importance": importance,
                        "source_layer": "okf" if result.source == "okf" else "B",
                    },
                })
                edges.append((paper_key(paper_hash), key, "has_content", 1.0, {"section": slug}))
                edges.append((key, spine_key(slug), "belongs_to", 1.0, {"section": slug}))
                for label in point_assets:
                    edges.append((key, asset_keys[label], "has_asset", 1.0, {"label": label}))

        result.sections = sorted(present_sections, key=order_index)
        result.nodes = self.fishbone.upsert_nodes(nodes)
        result.edges = self.fishbone.add_edges(edges)
        logger.info("鱼骨图构建完成 %s（来源 %s）：节点 %s，边 %s，维度 %s",
                    paper_hash[:8], result.source, result.nodes, result.edges,
                    ",".join(result.sections) or "无")
        return result
