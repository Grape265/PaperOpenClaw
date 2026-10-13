"""Cascade retrieval over the A/B/C pyramid (FR-11.2).

The requirement fixes the traversal order, and it is a good one:

    ① locate in **C** (abstracts) which papers are even relevant  -> cheap, tiny
    ② expand down to those papers' **B** units (fishbone dimensions) -> the要点
    ③ only if needed, expand again to **A** units (section digests) -> detail

So the expensive, verbose layer is touched only for papers that already earned
their place. Each result carries the ``trace`` of how it was reached, which is
what the UI shows as "引用路径".

Ranking is hybrid: dense vectors for paraphrase, BM25 for identifiers and
numbers, fused by reciprocal rank because the two score scales are not
comparable (see :func:`paperopenclaw.memory.lexical.reciprocal_rank_fusion`).
"""

from __future__ import annotations

import threading
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from ..config import SettingsStore
from ..core.logging import get_logger
from ..kg.skeleton import SPINE_LABELS, order_index
from ..storage.repos.papers import PaperRepository
from ..storage.repos.rag import RagRepository, parse_unit_id
from .embeddings import EmbeddingProvider
from .lexical import BM25Index, normalize_scores, reciprocal_rank_fusion
from .vector_store import VectorStore, VectorStoreError

logger = get_logger(__name__)

# Multiplier applied to top_k when gathering candidates before fusion: a wider
# net makes the fusion meaningful, since the two rankers rarely agree on the
# top handful.
CANDIDATE_FACTOR = 4
MAX_CANDIDATE_POOL = 400


class _Bm25Cache:
    """Small cache of BM25 indexes keyed by layer and the candidate hash set.

    Rebuilding BM25 means re-tokenising every document, which is the only
    non-trivial cost in the retrieval path. The key includes the unit count so
    that an ingest (which adds or replaces units) invalidates naturally without
    needing explicit event wiring.
    """

    def __init__(self, limit: int = 8) -> None:
        self._lock = threading.RLock()
        self._items: Dict[Tuple[str, Tuple[str, ...], int], BM25Index] = {}
        self._order: List[Tuple[str, Tuple[str, ...], int]] = []
        self._limit = limit

    def get(self, key: Tuple[str, Tuple[str, ...], int]) -> Optional[BM25Index]:
        with self._lock:
            return self._items.get(key)

    def put(self, key: Tuple[str, Tuple[str, ...], int], index: BM25Index) -> None:
        with self._lock:
            self._items[key] = index
            self._order.append(key)
            while len(self._order) > self._limit:
                stale = self._order.pop(0)
                self._items.pop(stale, None)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._order.clear()


class PyramidRetriever:
    """Hybrid, cascading retriever over the pyramid."""

    def __init__(
        self,
        papers: PaperRepository,
        rag: RagRepository,
        vectors: VectorStore,
        embedder: EmbeddingProvider,
        settings: SettingsStore,
    ) -> None:
        self.papers = papers
        self.rag = rag
        self.vectors = vectors
        self.embedder = embedder
        self.settings = settings
        self._bm25 = _Bm25Cache()

    # ------------------------------------------------------------------
    # public entry point
    # ------------------------------------------------------------------
    def search(
        self,
        query: str,
        *,
        hashes: Optional[Sequence[str]] = None,
        layers: Sequence[str] = ("C", "B", "A"),
        top_k: Optional[Dict[str, int]] = None,
        min_score: Optional[float] = None,
        cascade: bool = True,
    ) -> Dict[str, Any]:
        config = self.settings.current.retrieval
        wanted = {str(item).upper() for item in (layers or ("C", "B", "A"))}
        wanted &= {"A", "B", "C"}
        if not wanted:
            wanted = {"C", "B", "A"}

        k_c = (top_k or {}).get("c", config.top_k_c)
        k_b = (top_k or {}).get("b", config.top_k_b)
        k_a = (top_k or {}).get("a", config.top_k_a)
        threshold = config.min_score if min_score is None else float(min_score)
        scope = list(hashes) if hashes else None

        query = (query or "").strip()
        result: Dict[str, Any] = {
            "query": query,
            "candidates": [],
            "evidences": [],
            "layers_used": [],
            "empty": True,
            "threshold": threshold,
        }
        if not query:
            return result

        try:
            query_vector = self.embedder.encode_one(query, is_query=True)
        except Exception as exc:  # noqa: BLE001 - degrade to lexical-only
            logger.warning("查询嵌入失败，退化为词法检索：%s", exc)
            query_vector = []

        # ① apex: which papers are relevant at all
        c_hits: List[Dict[str, Any]] = []
        candidate_hashes: List[str] = []
        if "C" in wanted:
            c_hits = self._hybrid("C", query, query_vector, hashes=scope, top_k=max(k_c, 1))
            if c_hits:
                result["layers_used"].append("C")
                candidate_hashes = [hit["paper_hash"] for hit in c_hits if hit.get("paper_hash")]
                result["candidates"] = [
                    {
                        "hash": hit["paper_hash"],
                        "hash_short": hit["paper_hash"][:8],
                        "title": hit.get("title") or "",
                        "journal": hit.get("journal") or "",
                        "year": hit.get("year"),
                        "score": round(float(hit.get("score") or 0.0), 4),
                        "unit_id": hit["id"],
                    }
                    for hit in c_hits
                ]

        # A hard stop when the apex finds nothing: answering from layer A alone
        # would be answering without knowing whether the paper is on topic.
        best_c = max((hit.get("score") or 0.0) for hit in c_hits) if c_hits else 0.0
        if "C" in wanted and cascade and (not c_hits or best_c < threshold):
            result["empty"] = True
            result["reason"] = "apex_below_threshold"
            return result

        scope_for_lower = scope
        if cascade and candidate_hashes:
            scope_for_lower = candidate_hashes
        elif scope is None:
            scope_for_lower = None

        evidences: List[Dict[str, Any]] = []
        trace = {hit["id"]: [hit["id"]] for hit in c_hits}

        # ② middle: fishbone dimensions of the relevant papers
        b_hits: List[Dict[str, Any]] = []
        if "B" in wanted:
            b_hits = self._hybrid("B", query, query_vector, hashes=scope_for_lower, top_k=max(k_b, 1))
            if b_hits:
                result["layers_used"].append("B")
            for hit in b_hits:
                path = trace.get(
                    f"c:{hit['paper_hash']}",
                    [f"c:{hit['paper_hash']}"] if not cascade else [],
                )
                evidences.append(self._evidence(hit, path))

        # ③ base: section-level detail
        a_hits: List[Dict[str, Any]] = []
        if "A" in wanted and not (cascade and self.settings.current.retrieval.stop_at_b
                                  and self._b_is_sufficient(b_hits, k_b, threshold)):
            a_hits = self._hybrid("A", query, query_vector, hashes=scope_for_lower, top_k=max(k_a, 1))
            if a_hits:
                result["layers_used"].append("A")
            for hit in a_hits:
                parent_b = f"b:{hit['paper_hash']}:{hit.get('section') or ''}"
                path = list(trace.get(parent_b, []))
                if not path:
                    path = [f"c:{hit['paper_hash']}"] if hit.get("paper_hash") else []
                path.append(parent_b)
                evidences.append(self._evidence(hit, path))

        # Order the combined evidence by score but keep layer C context first.
        evidences.sort(key=lambda item: item.get("score", 0.0), reverse=True)
        result["evidences"] = evidences
        result["empty"] = not evidences and not c_hits
        if not result["empty"] and not evidences and c_hits:
            # Only the apex matched; still useful as a定位 result.
            result["evidences"] = [self._evidence(hit, [hit["id"]]) for hit in c_hits]
        return result

    @staticmethod
    def _b_is_sufficient(b_hits: Sequence[Dict[str, Any]], k_b: int, threshold: float) -> bool:
        """Enough confident middle-layer evidence that layer A adds little."""
        if len(b_hits) < max(1, k_b // 2):
            return False
        strong = [hit for hit in b_hits if (hit.get("score") or 0.0) >= threshold + 0.15]
        return len(strong) >= max(2, k_b // 2)

    # ------------------------------------------------------------------
    # layer search
    # ------------------------------------------------------------------
    def _hybrid(
        self,
        layer: str,
        query: str,
        query_vector: Sequence[float],
        *,
        hashes: Optional[Sequence[str]],
        top_k: int,
    ) -> List[Dict[str, Any]]:
        """Vector + BM25 fusion over one layer, restricted to ``hashes``."""
        config = self.settings.current.retrieval
        pool = min(MAX_CANDIDATE_POOL, max(top_k * CANDIDATE_FACTOR, 20))

        units = self.rag.unit_lookup(hashes=hashes, layers=[layer])
        if not units:
            return []

        vector_scores: Dict[str, float] = {}
        if query_vector:
            try:
                hits = self.vectors.query(layer, query_vector, top_k=pool, hashes=hashes)
            except VectorStoreError as exc:
                logger.warning("向量检索不可用：%s", exc)
                hits = []
            vector_scores = {hit["id"]: float(hit.get("score") or 0.0)
                             for hit in hits if hit["id"] in units}

        lexical_scores = self._lexical_scores(layer, query, units, hashes)

        if vector_scores and lexical_scores:
            fused = reciprocal_rank_fusion(
                [vector_scores, lexical_scores],
                weights=[config.vector_weight, config.lexical_weight],
                k=config.rrf_k,
            )
            similarity = {doc_id: score for doc_id, score in vector_scores.items()}
        elif vector_scores:
            fused = dict(vector_scores)
            similarity = dict(vector_scores)
        elif lexical_scores:
            fused = dict(lexical_scores)
            similarity = {}
        else:
            return []

        ranked = sorted(fused.items(), key=lambda kv: kv[1], reverse=True)[:top_k]
        normalised = normalize_scores(dict(ranked))

        results: List[Dict[str, Any]] = []
        for doc_id, _raw in ranked:
            unit = units.get(doc_id)
            if unit is None:
                continue
            paper_hash = unit.get("paper_hash", "")
            section = unit.get("section", "") or ""
            # Prefer the calibrated vector similarity when available so the
            # configured min_score keeps a stable meaning across layers.
            score = similarity.get(doc_id)
            if score is None:
                score = normalised.get(doc_id, 0.0)
            results.append({
                "id": doc_id,
                "layer": layer,
                "paper_hash": paper_hash,
                "text": unit.get("text", ""),
                "section": section,
                "section_label": SPINE_LABELS.get(section, ""),
                "ordinal": unit.get("ordinal", 0),
                "source_ref": unit.get("source_ref", ""),
                "title": unit.get("title", ""),
                "score": float(score),
                "asset_refs": self._asset_refs(unit),
            })
        return results

    def _lexical_scores(
        self,
        layer: str,
        query: str,
        units: Dict[str, Dict[str, Any]],
        hashes: Optional[Sequence[str]],
    ) -> Dict[str, float]:
        key = (layer, tuple(sorted(hashes or ())), len(units))
        index = self._bm25.get(key)
        if index is None:
            index = BM25Index()
            index.extend((unit_id, unit.get("text", "")) for unit_id, unit in units.items())
            self._bm25.put(key, index)
        return index.score(query)

    @staticmethod
    def _asset_refs(unit: Dict[str, Any]) -> List[str]:
        """Figure/table labels referenced by a unit, if the metadata carries them."""
        meta = unit.get("meta")
        if isinstance(meta, dict):
            assets = meta.get("asset_refs")
            if isinstance(assets, list):
                return [str(item) for item in assets]
        return []

    @staticmethod
    def _evidence(hit: Dict[str, Any], trace: Sequence[str]) -> Dict[str, Any]:
        return {
            "unit_id": hit["id"],
            "layer": hit["layer"],
            "hash": hit.get("paper_hash", ""),
            "hash_short": str(hit.get("paper_hash", ""))[:8],
            "title": hit.get("title", ""),
            "section": hit.get("section", ""),
            "section_label": hit.get("section_label", ""),
            "text": hit.get("text", ""),
            "score": round(float(hit.get("score") or 0.0), 4),
            "trace": list(trace),
            "asset_refs": hit.get("asset_refs", []),
        }

    # ------------------------------------------------------------------
    # helpers used by the agent tools
    # ------------------------------------------------------------------
    def hydrate_evidence(self, evidences: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Fill in paper titles, journal metadata and authors for evidence blocks."""
        hashes = {item.get("hash") or item.get("paper_hash") for item in evidences}
        hashes.discard("")
        if not hashes:
            return list(evidences)
        titles: Dict[str, Dict[str, Any]] = {}
        for paper in self.papers.list(hashes=list(hashes), page_size=200)[0]:
            titles[paper["hash"]] = paper
        # 作者名列表，用于「[n] 作者，论文名，哈希」格式的参考文献。
        authors_by_hash = self.papers.authors_for_many(list(hashes))
        enriched = []
        for item in evidences:
            entry = dict(item)
            paper = titles.get(entry.get("hash", ""))
            if paper:
                entry["paper_title"] = paper.get("title") or ""
                entry["journal"] = paper.get("journal") or ""
                entry["year"] = paper.get("year")
                entry["authors"] = [
                    author.get("name", "")
                    for author in authors_by_hash.get(entry.get("hash", ""), [])
                    if author.get("name")
                ]
            enriched.append(entry)
        return enriched

    def paper_briefs(self, hashes: Sequence[str]) -> List[Dict[str, Any]]:
        """Title + abstract per paper, used to orient the moderator agent."""
        briefs: List[Dict[str, Any]] = []
        for paper_hash in hashes:
            paper = self.papers.get(paper_hash)
            if not paper:
                continue
            briefs.append({
                "hash": paper_hash,
                "title": paper.get("title") or "",
                "abstract": paper.get("abstract") or "",
                "journal": paper.get("journal") or "",
                "year": paper.get("year"),
            })
        return briefs

    def sections_of(self, hashes: Sequence[str]) -> List[str]:
        """Spine dimensions present across the given papers, in paper order."""
        units = self.rag.unit_lookup(hashes=hashes, layers=["B"])
        present: Set[str] = {unit.get("section", "") for unit in units.values()}
        present.discard("")
        return sorted(present, key=order_index)
