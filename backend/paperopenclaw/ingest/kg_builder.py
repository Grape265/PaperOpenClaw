"""Building the knowledge graph (keyword graph) from the OKF full text.

The knowledge graph is the floating "bubble" graph rendered Obsidian-style. Its
keywords are extracted from the faithful OKF record and filtered by an LLM so
only research terms remain (通用词 like 影响/作者 are dropped). When no LLM is
available it falls back to a frequency-based extraction from layer A.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..core.jsonutil import JsonExtractionError, extract_json
from ..core.logging import get_logger
from ..core.text import keywords
from ..llm.base import Message
from ..llm.prompts import kg_keywords as kg_prompts
from ..storage.repos.kg import KnowledgeGraphRepository, keyword_key, paper_key
from ..storage.repos.papers import PaperRepository
from ..storage.repos.rag import RagRepository

logger = get_logger(__name__)


@dataclass
class KnowledgeGraphBuildResult:
    nodes: int = 0
    edges: int = 0
    keywords: int = 0
    warnings: List[str] = field(default_factory=list)
    source: str = "a"

    def to_dict(self) -> Dict[str, Any]:
        return {"nodes": self.nodes, "edges": self.edges,
                "keywords": self.keywords, "warnings": self.warnings, "source": self.source}


class KnowledgeGraphBuilder:
    """Turns the OKF record (via LLM, falling back to layer A) into keywords."""

    def __init__(self, papers: PaperRepository, kg: KnowledgeGraphRepository,
                 rag: RagRepository) -> None:
        self.papers = papers
        self.kg = kg
        self.rag = rag

    def build(
        self,
        paper_hash: str,
        *,
        title: str = "",
        a_units: Optional[Sequence[Dict[str, Any]]] = None,
        top_k: int = 16,
    ) -> KnowledgeGraphBuildResult:
        """Fallback: frequency keywords from layer-A digests."""
        units = list(a_units) if a_units is not None else [
            unit for unit in self.rag.units_of_paper(paper_hash, "A")
        ]
        words: List[str] = []
        seen: set = set()
        for unit in units:
            text = " ".join([
                str(unit.get("text") or ""),
                " ".join(str(point.get("text") or "")
                         for point in (unit.get("meta") or {}).get("points") or []),
            ]).strip()
            if not text:
                continue
            for word in keywords(text, top_k=top_k):
                if word not in seen:
                    seen.add(word)
                    words.append(word)
        return self._build_nodes(paper_hash, title, words, KnowledgeGraphBuildResult(source="a"))

    def build_from_okf(
        self,
        paper_hash: str,
        *,
        title: str = "",
        full_text: str = "",
        client: Any = None,
    ) -> KnowledgeGraphBuildResult:
        """Extract research keywords from the OKF full text with an LLM filter.

        The model keeps only research terms and drops common words. Falls back to
        layer-A frequency keywords when the client is missing or the call fails.
        """
        result = KnowledgeGraphBuildResult(source="okf")
        if client is None or not (full_text or "").strip():
            if not (full_text or "").strip():
                result.warnings.append("OKF 全文为空，回退到 A 层词频关键词")
            else:
                result.warnings.append("未配置摘要模型，回退到 A 层词频关键词")
            fallback = self.build(paper_hash, title=title)
            fallback.warnings = result.warnings + fallback.warnings
            return fallback

        try:
            response = client.chat(
                [Message.system(kg_prompts.kg_keywords_system_prompt()),
                 Message.user(kg_prompts.kg_keywords_user_prompt(title, full_text))],
                temperature=0.1,
                max_tokens=1024,
            )
            payload = extract_json(response.text, expect="object")
        except (JsonExtractionError, Exception) as exc:  # noqa: BLE001 - degrade
            logger.warning("从 OKF 提取关键词失败（%s），回退 A 层词频", exc)
            result.warnings.append(f"从 OKF 提取关键词失败，已回退 A 层词频：{exc}")
            fallback = self.build(paper_hash, title=title)
            fallback.warnings = result.warnings + fallback.warnings
            return fallback

        words: List[str] = []
        seen: set = set()
        for word in payload.get("keywords") or []:
            text = str(word).strip()
            if not text or text in seen:
                continue
            seen.add(text)
            words.append(text)
        return self._build_nodes(paper_hash, title, words, result)

    def _build_nodes(self, paper_hash: str, title: str, words: Sequence[str],
                     result: KnowledgeGraphBuildResult) -> KnowledgeGraphBuildResult:
        paper = self.papers.get(paper_hash)
        if paper and not title:
            title = paper.get("title") or ""

        self.kg.delete_paper(paper_hash)

        nodes: List[Dict[str, Any]] = [{
            "node_key": paper_key(paper_hash),
            "kind": "paper",
            "paper_hash": paper_hash,
            "label": title or paper_hash[:12],
            "detail": (paper or {}).get("abstract", "")[:2000],
            "ordinal": 0,
            "meta": {"journal": (paper or {}).get("journal", ""),
                     "year": (paper or {}).get("year")},
        }]
        edges: List[Tuple[str, str, str, float, Optional[Dict[str, Any]]]] = []

        keyword_ordinal = 0
        for word in words:
            keyword_ordinal += 1
            key = keyword_key(word)
            nodes.append({
                "node_key": key,
                "kind": "keyword",
                "paper_hash": "",
                "section": "",
                "label": word,
                "detail": "",
                "ordinal": 0,
                "unit_id": "",
                "meta": {"source_layer": "okf" if result.source == "okf" else "A"},
            })
            edges.append((paper_key(paper_hash), key, "has_keyword", 1.0, {}))

        result.keywords = keyword_ordinal
        result.nodes = self.kg.upsert_nodes(nodes)
        result.edges = self.kg.add_edges(edges)
        logger.info("知识图谱（关键词）构建完成 %s（来源 %s）：关键词 %s，节点 %s，边 %s",
                    paper_hash[:8], result.source, result.keywords, result.nodes, result.edges)
        return result
