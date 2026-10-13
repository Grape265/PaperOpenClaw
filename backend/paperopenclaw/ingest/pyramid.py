"""Building the A/B/C RAG pyramid (FR-6).

    A  <- 从原文精简得到的大意，按章节一条
    B  <- 把 A 再精简，按七个鱼骨维度归并
    C  <- abstract 原文（不经过模型）

Two properties of this design are worth stating explicitly:

* **C is not model-generated.** It is the paper's own abstract, so the apex of
  the pyramid is an objective fact rather than a lossy summary. That is what
  makes "search C first" trustworthy (FR-6.3).
* **B is derived from A, not from the raw text.** FR-6.2 says "把大意A再精简",
  and doing it that way also keeps the pyramid monotone: every B unit is
  provably a compression of the A units beneath it, and the ``a_to_b`` links
  record exactly which ones.

Layer B carries structured points because it is also the knowledge graph's
source (FR-7.1) — the points become the content nodes hung on the fishbone.
"""

from __future__ import annotations

import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from ..config import SettingsStore
from ..core.jsonutil import JsonExtractionError, as_str_list, extract_json
from ..core.logging import get_logger
from ..core.text import split_sentences, truncate, word_count
from ..kg.skeleton import SPINE, SPINE_LABELS, SPINE_SLUGS, label_of, order_index
from ..llm.base import LLMError, Message
from ..llm.prompts import digest as digest_prompts
from ..llm.registry import KIND_DIGEST, ProviderRegistry
from ..memory.embeddings import EmbeddingProvider
from ..memory.vector_store import VectorStore, VectorStoreError
from ..storage.repos.rag import (
    RagRepository,
    unit_id_a,
    unit_id_b,
    unit_id_c,
)

logger = get_logger(__name__)

ProgressCallback = Optional[Callable[[str, float], None]]

ASSET_REF_RE = re.compile(r"\b(fig(?:ure)?\.?\s*\d{1,3}|tab(?:le)?\.?\s*\d{1,3}|图\s*\d{1,3}|表\s*\d{1,3})\b",
                          re.IGNORECASE)


def extract_asset_refs(text: str) -> List[str]:
    """Pull figN/tabN labels out of free text so units can link to assets."""
    found: List[str] = []
    for match in ASSET_REF_RE.finditer(text or ""):
        raw = re.sub(r"\s+", "", match.group(1)).lower()
        if raw.startswith(("fig", "图")):
            digits = re.search(r"\d{1,3}", raw)
            if digits:
                label = f"fig{int(digits.group(0))}"
            else:
                continue
        else:
            digits = re.search(r"\d{1,3}", raw)
            if digits:
                label = f"tab{int(digits.group(0))}"
            else:
                continue
        if label not in found:
            found.append(label)
    return found


@dataclass
class PyramidResult:
    """The units and links produced for one paper."""

    units: List[Dict[str, Any]] = field(default_factory=list)
    links: List[Tuple[str, str, str, float, Optional[Dict[str, Any]]]] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    usage: Dict[str, int] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def counts(self) -> Dict[str, int]:
        return {
            "A": sum(1 for unit in self.units if unit["layer"] == "A"),
            "B": sum(1 for unit in self.units if unit["layer"] == "B"),
            "C": sum(1 for unit in self.units if unit["layer"] == "C"),
        }


# ---------------------------------------------------------------------------
# extractive fallbacks
# ---------------------------------------------------------------------------

def extractive_digest(text: str, max_chars: int, min_sentences: int = 3) -> str:
    """Lead-plus-signal sentences, used when no digest model is available.

    Purely positional truncation drops the conclusion of a section, so the
    first couple of sentences are kept for framing and the remaining budget
    goes to sentences carrying numbers or explicit result words.
    """
    cleaned = " ".join((text or "").split())
    if not cleaned:
        return ""
    if len(cleaned) <= max_chars:
        return cleaned

    sentences = split_sentences(cleaned)
    if not sentences:
        return truncate(cleaned, max_chars)

    signal = re.compile(
        r"\d|%|结果|表明|表明|发现|提升|下降|优于|达到|result|outperform|achieve|"
        r"improv|accuracy|f1|bleu|baseline|conclude|我们提出|本文提出|therefore|thus",
        re.IGNORECASE,
    )
    chosen: List[str] = list(sentences[:min_sentences])
    for sentence in sentences[min_sentences:]:
        if len(" ".join(chosen)) >= max_chars:
            break
        if signal.search(sentence):
            chosen.append(sentence)
    body = " ".join(chosen)
    return truncate(body if body else cleaned, max_chars)


def _fallback_points(texts: Sequence[str], section: str, limit: int = 6) -> List[Dict[str, Any]]:
    """Split condensed A text into sentence-level points for the graph."""
    points: List[Dict[str, Any]] = []
    for text in texts:
        for sentence in split_sentences(text):
            sentence = sentence.strip()
            if len(sentence) < 12:
                continue
            points.append({"text": sentence, "assets": extract_asset_refs(sentence)})
            if len(points) >= limit:
                return points
    return points


# ---------------------------------------------------------------------------
# builder
# ---------------------------------------------------------------------------

class PyramidBuilder:
    """Generates and persists the three pyramid layers for one paper."""

    def __init__(
        self,
        registry: ProviderRegistry,
        settings: SettingsStore,
        rag: RagRepository,
        vectors: VectorStore,
        embedder: EmbeddingProvider,
    ) -> None:
        self.registry = registry
        self.settings = settings
        self.rag = rag
        self.vectors = vectors
        self.embedder = embedder

    # ------------------------------------------------------------------
    def build(
        self,
        paper_hash: str,
        *,
        title: str,
        abstract: str,
        sections: Sequence[Dict[str, Any]],
        assets: Sequence[Dict[str, Any]] = (),
        progress: ProgressCallback = None,
    ) -> PyramidResult:
        """Generate layers A, B and C plus their links."""
        config = self.settings.current.ingest
        result = PyramidResult()
        asset_labels = [item["label"] for item in assets if item.get("label")]

        # ---- layer C: the abstract, verbatim (FR-6.3) ------------------
        c_text = (abstract or "").strip()
        if not c_text and sections:
            c_text = extractive_digest(str(sections[0].get("text") or ""), 1200)
            result.warnings.append("原文未抽取到摘要，C 层已回退为首个章节的精简文本")
        if not c_text:
            c_text = title
            result.warnings.append("摘要与正文均为空，C 层仅保留标题")

        c_unit = {
            "id": unit_id_c(paper_hash),
            "layer": "C",
            "ordinal": 0,
            "title": title,
            "text": c_text,
            "section": "",
            "source_ref": "paper",
            "meta": {"asset_refs": extract_asset_refs(c_text), "source": "abstract"},
        }
        result.units.append(c_unit)

        # ---- layer A: per-section detailed digest (FR-6.1) -------------
        if progress:
            progress("正在生成 A 层详细大意", 0.1)
        digest_client, digest_route = self._digest_route()
        target_language = "zh" if self.settings.current.embedding else "zh"

        a_units: List[Dict[str, Any]] = []

        def _digest_one_a(item: Tuple[int, Dict[str, Any]]) -> Tuple[int, Dict[str, Any]]:
            index, section = item
            slug = section.get("section") or "background"
            source_text = str(section.get("text") or "")
            digest_text, used_model = self._digest_a(
                digest_client, digest_route, title, slug, section, source_text,
                config.digest_max_chars_a, target_language, result,
            )
            return index, {
                "id": unit_id_a(paper_hash, index),
                "layer": "A",
                "ordinal": index,
                "title": section.get("title") or label_of(slug),
                "text": digest_text,
                "section": slug,
                "source_ref": f"sections/{section.get('slug') or index}",
                "meta": {
                    "orig_chars": len(source_text),
                    "digest_by": "model" if used_model else "extractive",
                    "asset_refs": extract_asset_refs(digest_text),
                },
            }

        tasks = [
            (index, section) for index, section in enumerate(sections)
            if str(section.get("text") or "").strip()
        ]
        # A 层逐章节调用模型，串行是导入慢的主因之一：并行化（多模态解析回退本地时
        # 也用同一批 A 单元）。
        if digest_client is None or len(tasks) <= 1:
            built = dict(_digest_one_a(item) for item in tasks)
        else:
            built = {}
            workers = min(6, len(tasks))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(_digest_one_a, item): item for item in tasks}
                for future in as_completed(futures):
                    index, unit = future.result()
                    built[index] = unit
        for index, _section in tasks:
            a_units.append(built[index])
            result.units.append(built[index])
            if progress:
                progress(f"正在生成 A 层详细大意（{index + 1}/{len(sections)}）",
                         0.1 + 0.5 * (index + 1) / max(len(sections), 1))

        # ---- layer B: per-dimension condensation (FR-6.2) --------------
        if progress:
            progress("正在生成 B 层要点", 0.65)
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for unit in a_units:
            grouped.setdefault(unit["section"] or "background", []).append(unit)

        def _digest_one_b(slug: str) -> Tuple[str, Dict[str, Any]]:
            members = grouped[slug]
            summary, points, importance, used_model = self._digest_b(
                digest_client, digest_route, title, slug, members, asset_labels,
                config.digest_max_chars_b, target_language, result,
            )
            return slug, {
                "id": unit_id_b(paper_hash, slug),
                "layer": "B",
                "ordinal": order_index(slug),
                "title": label_of(slug),
                "text": summary,
                "section": slug,
                "source_ref": f"sections/{members[0].get('source_ref', '').split('/')[-1]}",
                "meta": {
                    "points": points,
                    "importance": importance,
                    "digest_by": "model" if used_model else "extractive",
                    "asset_refs": sorted({
                        label
                        for point in points for label in (point.get("assets") or [])
                        if label in asset_labels
                    }),
                    "source_units": [member["id"] for member in members],
                },
            }

        b_units: List[Dict[str, Any]] = []
        slugs = sorted(grouped, key=order_index)
        if digest_client is None or len(slugs) <= 1:
            b_built = dict(_digest_one_b(slug) for slug in slugs)
        else:
            b_built = {}
            with ThreadPoolExecutor(max_workers=min(6, len(slugs))) as pool:
                futures = {pool.submit(_digest_one_b, slug): slug for slug in slugs}
                for future in as_completed(futures):
                    slug, unit = future.result()
                    b_built[slug] = unit
        for slug in slugs:
            unit = b_built[slug]
            if not unit["text"] and not (unit["meta"]["points"]):
                continue
            b_units.append(unit)
            result.units.append(unit)

        # ---- links: A -> B (FR-6.5) ------------------------------------
        for unit in a_units:
            slug = unit["section"] or "background"
            target = unit_id_b(paper_hash, slug)
            if any(item["id"] == target for item in b_units):
                weight = min(1.0, max(0.1, (unit["meta"].get("orig_chars") or 0) / 4000.0))
                result.links.append((unit["id"], target, "a_to_b", round(weight, 4),
                                     {"section": slug}))

        # ---- links: B -> C (FR-6.5) ------------------------------------
        for unit in b_units:
            result.links.append((unit["id"], c_unit["id"], "b_to_c", 1.0, {"section": unit["section"]}))

        result.counts()
        logger.info("金字塔构建完成 %s：A=%s B=%s C=%s",
                    paper_hash[:8], *_counts_tuple(paper_hash, result.units))
        return result

    # ------------------------------------------------------------------
    def _digest_route(self) -> Tuple[Optional[Any], Any]:
        """The digest route, falling back to the chat route when unset."""
        client = self.registry.try_client(KIND_DIGEST)
        route = self.registry.route_config(KIND_DIGEST)
        if client is None:
            client = self.registry.try_client("chat")
            route = self.registry.route_config("chat")
        if client is None:
            logger.warning("未配置摘要模型，A/B 层将使用抽取式降级方案")
        return client, route

    def _digest_a(
        self,
        client: Optional[Any],
        route: Any,
        title: str,
        slug: str,
        section: Dict[str, Any],
        source_text: str,
        max_chars: int,
        target_language: str,
        result: PyramidResult,
    ) -> Tuple[str, bool]:
        if client is None:
            return extractive_digest(source_text, max_chars), False
        system = Message.system(digest_prompts.digest_a_system_prompt(target_language))
        user = Message.user(digest_prompts.digest_a_user_prompt(
            title, label_of(slug), str(section.get("title") or ""), source_text, max_chars,
        ))
        try:
            response = client.chat(
                [system, user],
                temperature=getattr(route, "temperature", 0.2),
                max_tokens=getattr(route, "max_tokens", 2048),
            )
            text = (response.text or "").strip()
            self._accumulate(result, response)
            if text:
                return text, True
            result.warnings.append(f"章节「{section.get('title')}」摘要为空，已使用抽取式降级")
        except (LLMError, Exception) as exc:  # noqa: BLE001 - degrade, never fail ingest
            logger.warning("A 层摘要生成失败（%s），使用抽取式降级：%s", section.get("title"), exc)
            result.warnings.append(f"章节「{section.get('title')}」摘要失败，已使用抽取式降级")
        return extractive_digest(source_text, max_chars), False

    def _digest_b(
        self,
        client: Optional[Any],
        route: Any,
        title: str,
        slug: str,
        members: Sequence[Dict[str, Any]],
        asset_labels: Sequence[str],
        max_chars: int,
        target_language: str,
        result: PyramidResult,
    ) -> Tuple[str, List[Dict[str, Any]], float, bool]:
        texts = [str(unit.get("text") or "") for unit in members]
        material = [
            {"title": unit.get("title") or "", "text": unit.get("text") or ""}
            for unit in members
        ]

        if client is not None:
            system = Message.system(digest_prompts.digest_b_system_prompt(target_language))
            user = Message.user(digest_prompts.digest_b_user_prompt(
                title, label_of(slug), material, asset_labels,
            ))
            try:
                response = client.chat(
                    [system, user],
                    temperature=getattr(route, "temperature", 0.2),
                    max_tokens=getattr(route, "max_tokens", 2048),
                )
                self._accumulate(result, response)
                payload = extract_json(response.text or "", expect="object")
                summary = str(payload.get("summary") or "").strip()
                raw_points = payload.get("points") or []
                points: List[Dict[str, Any]] = []
                if isinstance(raw_points, dict):
                    raw_points = [raw_points]
                for entry in raw_points or []:
                    if isinstance(entry, str):
                        text = entry.strip()
                        assets = extract_asset_refs(text)
                    elif isinstance(entry, dict):
                        text = str(entry.get("text") or entry.get("point") or "").strip()
                        assets = as_str_list(entry.get("assets")) or extract_asset_refs(text)
                    else:
                        continue
                    if text:
                        points.append({"text": text, "assets": assets})
                try:
                    importance = float(payload.get("importance") or 0.5)
                except (TypeError, ValueError):
                    importance = 0.5
                importance = min(1.0, max(0.0, importance))
                if summary or points:
                    if not summary and points:
                        summary = truncate(" ".join(item["text"] for item in points), max_chars)
                    return summary, points, importance, True
                result.warnings.append(f"维度「{label_of(slug)}」的 B 层结果为空白，已降级")
            except (JsonExtractionError, LLMError, Exception) as exc:  # noqa: BLE001
                logger.warning("B 层要点生成失败（%s），使用抽取式降级：%s", slug, exc)
                result.warnings.append(f"维度「{label_of(slug)}」要点生成失败，已使用抽取式降级")

        summary = truncate(" ".join(texts), max_chars)
        points = _fallback_points(texts, slug)
        return summary, points, 0.5, False

    @staticmethod
    def _accumulate(result: PyramidResult, response: Any) -> None:
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        with result._lock:
            for key, value in usage.to_dict().items():
                result.usage[key] = int(result.usage.get(key, 0)) + int(value or 0)

    # ------------------------------------------------------------------
    # persistence
    # ------------------------------------------------------------------
    def persist(self, paper_hash: str, result: PyramidResult) -> Dict[str, int]:
        """Write units, links and vectors for one paper."""
        self.rag.replace_units(paper_hash, result.units)
        written_links = self.rag.add_links(result.links)
        indexed = self.index_vectors(paper_hash, result.units)
        logger.info("金字塔落库 %s：单元 %s，关联 %s，向量 %s",
                    paper_hash[:8], len(result.units), written_links, indexed)
        return {"units": len(result.units), "links": written_links, "vectors": indexed}

    def index_vectors(self, paper_hash: str, units: Sequence[Dict[str, Any]]) -> int:
        """Embed and upsert units into their layer collections."""
        if not units:
            return 0
        by_layer: Dict[str, List[Dict[str, Any]]] = {}
        for unit in units:
            by_layer.setdefault(unit["layer"], []).append(unit)

        total = 0
        for layer, items in by_layer.items():
            texts = [self._embedding_text(unit) for unit in items]
            try:
                vectors = self.embedder.encode(texts)
            except Exception as exc:  # noqa: BLE001 - vector indexing is best-effort
                logger.error("向量化失败（层 %s）：%s", layer, exc)
                raise VectorStoreError(f"向量化失败：{exc}") from exc
            metadatas = [
                {
                    "paper_hash": paper_hash,
                    "layer": layer,
                    "section": unit.get("section") or "",
                    "title": unit.get("title") or "",
                    "ordinal": int(unit.get("ordinal") or 0),
                }
                for unit in items
            ]
            total += self.vectors.upsert(
                layer,
                [unit["id"] for unit in items],
                vectors,
                [str(unit.get("text") or "") for unit in items],
                metadatas,
            )
        return total

    @staticmethod
    def _embedding_text(unit: Dict[str, Any]) -> str:
        """Text used for embedding: title adds context the body may lack."""
        title = str(unit.get("title") or "").strip()
        text = str(unit.get("text") or "").strip()
        section = label_of(unit.get("section") or "") if unit.get("section") else ""
        prefix = " ".join(part for part in (section, title) if part)
        if unit.get("layer") == "B":
            points = (unit.get("meta") or {}).get("points") or []
            extras = " ".join(str(item.get("text") or "") for item in points)
            text = f"{text} {extras}".strip()
        return f"{prefix} {text}".strip() if prefix else text

    # ------------------------------------------------------------------
    # cross-paper C-C links (FR-6.6)
    # ------------------------------------------------------------------
    def link_cross_papers(self, paper_hash: str, *, top_k: int = 0, min_score: float = 0.0) -> int:
        """Connect this paper's C unit to the most similar C units of other papers.

        Symmetric edges are written in both directions so traversal never has to
        guess the direction, and per-paper ``top_k`` plus a score floor keep the
        apex from degenerating into a complete graph.
        """
        config = self.settings.current.retrieval
        limit = int(top_k or config.cross_link_top_k)
        floor = float(min_score or config.cross_link_min_score)
        unit = self.rag.get_unit(unit_id_c(paper_hash))
        if unit is None:
            return 0

        self.rag.delete_cross_links(paper_hash)

        try:
            vector = self.embedder.encode_one(str(unit.get("text") or ""), is_query=True)
        except Exception as exc:  # noqa: BLE001 - cross links are optional
            logger.warning("C-C 关联向量化失败：%s", exc)
            return 0
        if not vector:
            return 0

        try:
            hits = self.vectors.query("C", vector, top_k=max(limit * 4, 8))
        except VectorStoreError as exc:
            logger.warning("C-C 关联检索失败：%s", exc)
            return 0

        links: List[Tuple[str, str, str, float, Optional[Dict[str, Any]]]] = []
        for hit in hits:
            other = hit.get("id") or ""
            if other == unit["id"]:
                continue
            score = float(hit.get("score") or 0.0)
            if score < floor:
                continue
            meta = {"score": round(score, 4), "by": "embedding"}
            links.append((unit["id"], other, "c_to_c", round(score, 4), meta))
            links.append((other, unit["id"], "c_to_c", round(score, 4), meta))
            if len(links) >= limit * 2:
                break

        written = self.rag.add_links(links) if links else 0
        if written:
            logger.info("C-C 跨论文关联 %s：%s 条", paper_hash[:8], written)
        return written

    # ------------------------------------------------------------------
    def rebuild_cross_links(self, hashes: Sequence[str] = ()) -> int:
        """Recompute C-C links for a set of papers (used by the rebuild job)."""
        targets = list(hashes) if hashes else [
            row["paper_hash"] for row in self.rag.units_of_layer("C")
        ]
        seen = set()
        total = 0
        for hash_value in targets:
            if hash_value in seen:
                continue
            seen.add(hash_value)
            total += self.link_cross_papers(hash_value)
        return total


def _counts_tuple(paper_hash: str, units: Sequence[Dict[str, Any]]) -> Tuple[int, int, int]:
    return (
        sum(1 for unit in units if unit["layer"] == "A"),
        sum(1 for unit in units if unit["layer"] == "B"),
        sum(1 for unit in units if unit["layer"] == "C"),
    )
