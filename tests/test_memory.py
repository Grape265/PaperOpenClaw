"""Memory-layer tests: OKF I/O, lexical ranking, vector store, cascade retrieval.

The retriever test drives a real ChromaDB instance in a temporary directory with
a deterministic stub embedder, so the C->B->A cascade and its threshold
behaviour are exercised end to end rather than mocked away.

    python tests/test_memory.py
"""

from __future__ import annotations

import math
import sys
from pathlib import Path
from typing import Any, Dict, List, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _harness import TempWorkspace, run_all  # noqa: E402

from paperopenclaw.config import Settings, SettingsStore  # noqa: E402
from paperopenclaw.core.paths import DataPaths  # noqa: E402
from paperopenclaw.core.text import tokenize  # noqa: E402
from paperopenclaw.memory import (  # noqa: E402
    BM25Index,
    EmbeddingProvider,
    LexicalEmbedding,
    OKFError,
    VectorStore,
    build_embedding_provider,
    build_index_markdown,
    build_log_markdown,
    build_tree,
    dump_concept,
    extract_links,
    hashed_embedding,
    parse_concept,
    reciprocal_rank_fusion,
    split_frontmatter,
    validate_bundle,
)
from paperopenclaw.memory.retriever import PyramidRetriever  # noqa: E402
from paperopenclaw.storage.db import Database  # noqa: E402
from paperopenclaw.storage.repos import PaperRepository, RagRepository  # noqa: E402
from paperopenclaw.storage.repos.rag import unit_id_a, unit_id_b, unit_id_c  # noqa: E402

HASH_A = "a" * 64
HASH_B = "b" * 64


# ---------------------------------------------------------------------------
# a deterministic embedder: bag of words over a fixed vocabulary
# ---------------------------------------------------------------------------

class StubEmbedder(EmbeddingProvider):
    """Hash-free bag-of-words vectors so similarity is fully predictable."""

    name = "stub"

    def __init__(self, dimension: int = 256) -> None:
        self._dim = dimension
        self.model = "stub-bow"

    @property
    def dimension(self) -> int:
        return self._dim

    def encode(self, texts: Sequence[str], *, is_query: bool = False) -> List[List[float]]:
        import hashlib

        out: List[List[float]] = []
        for text in texts:
            vector = [0.0] * self._dim
            for token in tokenize(text):
                digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
                vector[int.from_bytes(digest, "big") % self._dim] += 1.0
            norm = math.sqrt(sum(v * v for v in vector))
            out.append([v / norm for v in vector] if norm else vector)
        return out


# ---------------------------------------------------------------------------
# OKF
# ---------------------------------------------------------------------------

def test_split_frontmatter_handles_missing_and_present():
    yaml_text, body = split_frontmatter("---\ntype: X\n---\n\n# Body\n")
    assert yaml_text.strip() == "type: X"
    assert body.strip() == "# Body"

    yaml_text, body = split_frontmatter("# No frontmatter\n")
    assert yaml_text == ""
    assert body == "# No frontmatter\n"


def test_parse_concept_requires_type():
    parsed = parse_concept("---\ntype: Research Paper\ntitle: T\n---\n\nbody")
    assert parsed["frontmatter"]["type"] == "Research Paper"
    assert parsed["body"] == "body"

    for bad in ("no frontmatter at all", "---\ntitle: no type\n---\n\nx", "---\ntype: ''\n---\n\nx"):
        try:
            parse_concept(bad)
        except OKFError:
            continue
        raise AssertionError(f"应当拒绝缺少 type 的文档：{bad!r}")


def test_dump_concept_roundtrip_and_key_order():
    frontmatter = {
        "title": "论文标题", "type": "Research Paper",
        "paper_hash": HASH_A, "tags": ["nlp", "attention"],
        "generated": {"by": "paperopenclaw/1.0.0", "at": "2026-01-01T00:00:00Z"},
    }
    text = dump_concept(frontmatter, "# 正文\n\n内容。")
    assert text.startswith("---\n")
    # `type` is written first because it is the spec's required key.
    assert text.splitlines()[1].startswith("type:")

    parsed = parse_concept(text)
    assert parsed["frontmatter"]["type"] == "Research Paper"
    assert parsed["frontmatter"]["tags"] == ["nlp", "attention"]
    assert parsed["frontmatter"]["generated"]["by"].startswith("paperopenclaw/")
    assert parsed["body"].strip() == "# 正文\n\n内容。"


def test_dump_concept_rejects_missing_type():
    try:
        dump_concept({"title": "x"})
    except OKFError:
        return
    raise AssertionError("应当拒绝缺少 type 的 frontmatter")


def test_extract_links_ignores_external_and_anchors():
    body = ("See [a](/sections/method.md) and [b](./figures/fig1.md#x) "
            "and [c](https://example.com) and [d](/tables/tab1.md).")
    links = extract_links(body)
    assert links == ["/sections/method.md", "./figures/fig1.md", "https://example.com",
                     "/tables/tab1.md"]


def test_index_and_log_markdown_follow_spec():
    index = build_index_markdown(
        [("章节", [("方法", "/sections/method.md", "模型与算法")])],
        okf_version="0.2",
    )
    assert 'okf_version: "0.2"' in index
    assert "* [方法](/sections/method.md) - 模型与算法" in index

    log = build_log_markdown([("2026-01-01", [("Creation", "建立 [论文](/paper.md)")])])
    assert "## 2026-01-01" in log
    assert "* **Creation**" in log


def test_validate_bundle_detects_errors_and_tolerates_broken_links():
    with TempWorkspace() as tmp:
        root = Path(tmp)
        (root / "sections").mkdir()
        (root / "index.md").write_text(
            build_index_markdown([("S", [("P", "/paper.md", "d")])], okf_version="0.2"),
            encoding="utf-8")
        (root / "log.md").write_text(build_log_markdown([("2026-01-01", [("Creation", "x")])]),
                                     encoding="utf-8")
        (root / "paper.md").write_text(
            dump_concept({"type": "Research Paper", "title": "P", "description": "d"},
                         "见 [方法](/sections/method.md)。"),
            encoding="utf-8")
        (root / "sections" / "method.md").write_text(
            dump_concept({"type": "Section", "title": "方法", "description": "d"},
                         "见 [不存在的](/sections/nope.md)。"),
            encoding="utf-8")

        report = validate_bundle(root)
        assert report["conformant"] is True
        assert report["files"] == 2  # index.md and log.md are not concepts
        assert report["errors"] == []
        # Broken links are tolerated by the spec but surfaced as warnings.
        assert any("nope.md" in item["message"] for item in report["warnings"]), report["warnings"]

        # A concept without `type` must break conformance.
        (root / "bad.md").write_text("---\ntitle: no type\n---\n\nx", encoding="utf-8")
        report = validate_bundle(root)
        assert report["conformant"] is False
        assert any(item["file"] == "bad.md" for item in report["errors"])


def test_build_tree_skips_reserved_files():
    with TempWorkspace() as tmp:
        root = Path(tmp)
        (root / "sections").mkdir()
        (root / "index.md").write_text("# Index\n", encoding="utf-8")
        (root / "paper.md").write_text(
            dump_concept({"type": "Research Paper", "title": "P", "description": "d"}), encoding="utf-8")
        (root / "sections" / "method.md").write_text(
            dump_concept({"type": "Section", "title": "方法", "description": "d", "section": "method"}),
            encoding="utf-8")

        tree = build_tree(root)
        ids = [node["concept_id"] for node in tree]
        assert "paper" in ids
        assert "index" not in ids and "log" not in ids
        section_dir = next(node for node in tree if node["type"] == "Directory")
        assert section_dir["children"][0]["concept_id"] == "sections/method"
        assert section_dir["children"][0]["section"] == "method"


# ---------------------------------------------------------------------------
# lexical ranking
# ---------------------------------------------------------------------------

def test_bm25_ranks_term_matches_first():
    index = BM25Index()
    index.extend([
        ("d1", "attention mechanism transformer encoder decoder"),
        ("d2", "convolutional neural network image classification"),
        ("d3", "graph neural network node embedding"),
    ])
    ranked = index.rank("attention transformer")
    assert ranked, "应当命中至少一个文档"
    assert ranked[0][0] == "d1"
    assert index.rank("quantum chromodynamics") == []


def test_bm25_handles_chinese():
    index = BM25Index()
    index.extend([
        ("d1", "本文提出了一种基于自注意力机制的模型"),
        ("d2", "本文研究了图像分类中的卷积网络结构"),
    ])
    ranked = index.rank("自注意力机制")
    assert ranked and ranked[0][0] == "d1"


def test_hashed_embedding_is_deterministic_and_normalised():
    first = hashed_embedding("自注意力机制 attention")
    second = hashed_embedding("自注意力机制 attention")
    assert first == second
    assert len(first) == 384
    norm = math.sqrt(sum(v * v for v in first))
    assert abs(norm - 1.0) < 1e-6

    other = hashed_embedding("图像分类卷积网络")
    dot = sum(a * b for a, b in zip(first, other))
    self_dot = sum(a * b for a, b in zip(first, first))
    assert dot < self_dot, "不同文本的相似度应低于自身"


def test_hashed_embedding_handles_empty():
    vector = hashed_embedding("")
    assert len(vector) == 384
    assert all(v == 0.0 for v in vector)


def test_reciprocal_rank_fusion_prefers_agreement():
    ranking_a = {"x": 0.9, "y": 0.8, "z": 0.1}
    ranking_b = {"y": 12.0, "z": 5.0, "x": 0.5}
    fused = reciprocal_rank_fusion([ranking_a, ranking_b])
    # `y` is near the top of both lists, so it must win.
    assert max(fused, key=fused.get) == "y"
    assert reciprocal_rank_fusion([]) == {}


# ---------------------------------------------------------------------------
# vector store（real ChromaDB）
# ---------------------------------------------------------------------------

def test_vector_store_upsert_query_delete():
    with TempWorkspace() as tmp:
        store = VectorStore(Path(tmp) / "vectors")
        store.upsert(
            "C",
            ["c:aaa", "c:bbb", "c:ccc"],
            [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
            ["abstract a", "abstract b", "abstract c"],
            [{"paper_hash": "aaa", "title": "A"}, {"paper_hash": "bbb", "title": "B"},
             {"paper_hash": "ccc", "title": "C"}],
        )
        assert store.count("C")["C"] == 3

        hits = store.query("C", [1.0, 0.05, 0.0], top_k=2)
        assert hits[0]["id"] == "c:aaa"
        assert hits[0]["score"] > hits[1]["score"]
        assert hits[0]["metadata"]["paper_hash"] == "aaa"

        # Metadata filtering restricted to a single paper.
        filtered = store.query("C", [0.0, 0.0, 1.0], top_k=3, hashes=["ccc"])
        assert [hit["id"] for hit in filtered] == ["c:ccc"]

        multi = store.query("C", [1.0, 0.0, 0.0], top_k=3, hashes=["bbb", "ccc"])
        assert {hit["id"] for hit in multi} == {"c:bbb", "c:ccc"}

        store.delete_paper("bbb")
        assert store.count("C")["C"] == 2

        store.delete_ids("C", ["c:ccc"])
        assert store.count("C")["C"] == 1


def test_vector_store_rejects_dimension_change():
    with TempWorkspace() as tmp:
        store = VectorStore(Path(tmp) / "vectors")
        store.upsert("A", ["a1"], [[1.0, 0.0]], ["x"])
        try:
            store.upsert("A", ["a2"], [[1.0, 0.0, 0.0]], ["y"])
        except Exception as exc:
            assert "维度" in str(exc)
            return
        raise AssertionError("维度变化应当被拒绝")


def test_vector_store_layers_are_independent():
    with TempWorkspace() as tmp:
        store = VectorStore(Path(tmp) / "vectors")
        store.upsert("A", ["a1"], [[1.0, 0.0]], ["a"])
        store.upsert("B", ["b1"], [[1.0, 0.0]], ["b"])
        store.upsert("C", ["c1"], [[1.0, 0.0]], ["c"])
        counts = store.count()
        assert counts == {"A": 1, "B": 1, "C": 1}
        assert store.query("A", [1.0, 0.0], top_k=5)[0]["id"] == "a1"
        store.reset("B")
        assert store.count()["B"] == 0
        assert store.count()["A"] == 1


# ---------------------------------------------------------------------------
# cascade retrieval
# ---------------------------------------------------------------------------

class RetrieverFixture:
    """Builds a two-paper pyramid and wires a retriever over it."""

    def __init__(self) -> None:
        self._tmp = TempWorkspace()
        self.paths = DataPaths(str(self._tmp.path)).ensure_base()
        self.db = Database(self.paths.sqlite_file).initialise()
        self.papers = PaperRepository(self.db)
        self.rag = RagRepository(self.db)
        self.vectors = VectorStore(self.paths.vectors_dir)
        self.embedder = StubEmbedder()

        settings = Settings(
            retrieval={"top_k_c": 3, "top_k_b": 5, "top_k_a": 5, "min_score": 0.15,
                       "cross_link_top_k": 3, "cross_link_min_score": 0.2, "stop_at_b": False},
        )
        self.store = SettingsStore(self.paths, settings)
        self.retriever = PyramidRetriever(self.papers, self.rag, self.vectors,
                                          self.embedder, self.store)
        self._seed()

    def _seed(self) -> None:
        papers = [
            (HASH_A, "Attention Is All You Need",
             "We propose the Transformer, a model architecture relying entirely on self-attention "
             "to draw global dependencies between input and output.",
             "Transformer self-attention encoder decoder machine translation"),
            (HASH_B, "Convolutional Image Classification",
             "We study convolutional neural networks for image classification on large datasets.",
             "convolutional image classification dataset benchmark accuracy"),
        ]
        for hash_value, title, abstract, extra in papers:
            self.papers.create(hash_value, title=title, status="ready")
            self.papers.update(hash_value, journal="NeurIPS", year=2017,
                               abstract=abstract, language="en")

        self.rag.replace_units(HASH_A, [
            {"id": unit_id_c(HASH_A), "layer": "C", "title": "Attention Is All You Need",
             "text": "We propose the Transformer, a model architecture relying entirely on "
                     "self-attention to draw global dependencies between input and output."},
            {"id": unit_id_b(HASH_A, "method"), "layer": "B", "section": "method", "ordinal": 1,
             "title": "方法",
             "text": "Transformer self-attention encoder decoder multi-head scaled dot-product."},
            {"id": unit_id_b(HASH_A, "experiment"), "layer": "B", "section": "experiment",
             "ordinal": 2, "title": "实验",
             "text": "Machine translation WMT14 BLEU 28.4 training cost."},
            {"id": unit_id_a(HASH_A, 0), "layer": "A", "section": "method", "ordinal": 0,
             "title": "Model Architecture",
             "text": "The encoder stacks six identical layers; each has a multi-head "
                     "self-attention sublayer and a position-wise feed-forward network."},
            {"id": unit_id_a(HASH_A, 1), "layer": "A", "section": "experiment", "ordinal": 1,
             "title": "Results",
             "text": "On WMT 2014 English-to-German translation the big model reaches 28.4 BLEU."},
        ])
        self.rag.replace_units(HASH_B, [
            {"id": unit_id_c(HASH_B), "layer": "C", "title": "Convolutional Image Classification",
             "text": "We study convolutional neural networks for image classification on large datasets."},
            {"id": unit_id_b(HASH_B, "method"), "layer": "B", "section": "method", "ordinal": 1,
             "title": "方法", "text": "convolutional neural network image classification layers."},
            {"id": unit_id_a(HASH_B, 0), "layer": "A", "section": "method", "ordinal": 0,
             "title": "Network", "text": "The convolutional network has five layers and pooling."},
        ])

        self.rag.add_links([
            (unit_id_a(HASH_A, 0), unit_id_b(HASH_A, "method"), "a_to_b", 1.0, None),
            (unit_id_a(HASH_A, 1), unit_id_b(HASH_A, "experiment"), "a_to_b", 1.0, None),
            (unit_id_b(HASH_A, "method"), unit_id_c(HASH_A), "b_to_c", 1.0, None),
            (unit_id_b(HASH_A, "experiment"), unit_id_c(HASH_A), "b_to_c", 1.0, None),
            (unit_id_a(HASH_B, 0), unit_id_b(HASH_B, "method"), "a_to_b", 1.0, None),
            (unit_id_b(HASH_B, "method"), unit_id_c(HASH_B), "b_to_c", 1.0, None),
        ])

        for layer, units in (
            ("C", [u for u in self.rag.units_of_layer("C")]),
            ("B", [u for u in self.rag.units_of_layer("B")]),
            ("A", [u for u in self.rag.units_of_layer("A")]),
        ):
            self.vectors.upsert(
                layer,
                [u["id"] for u in units],
                self.embedder.encode([u["text"] for u in units]),
                [u["text"] for u in units],
                [{"paper_hash": u["paper_hash"], "section": u.get("section") or "",
                  "title": u.get("title") or "", "layer": layer} for u in units],
            )

    def close(self) -> None:
        self.vectors.close()
        self.db.close()
        self._tmp.cleanup()


def test_cascade_returns_c_b_and_a_layers():
    fixture = RetrieverFixture()
    try:
        result = fixture.retriever.search(
            "Transformer self-attention encoder decoder",
            top_k={"c": 3, "b": 5, "a": 5},
        )
        assert result["empty"] is False
        assert result["candidates"], "应当在 C 层定位到论文"
        assert result["candidates"][0]["hash"] == HASH_A
        layers = {item["layer"] for item in result["evidences"]}
        assert "B" in layers, "应当下钻到 B 层"
        assert result["layers_used"][0] == "C"

        # Every evidence item carries a trace back to the apex.
        for item in result["evidences"]:
            assert isinstance(item["trace"], list)
        b_items = [item for item in result["evidences"] if item["layer"] == "B"]
        assert any(item["hash"] == HASH_A for item in b_items)
    finally:
        fixture.close()


def test_cascade_respects_hash_scope():
    fixture = RetrieverFixture()
    try:
        scoped = fixture.retriever.search("Transformer self-attention encoder decoder",
                                          hashes=[HASH_B])
        assert all(item["hash"] == HASH_B for item in scoped["evidences"])
        assert all(item["hash"] == HASH_B for item in scoped["candidates"])
    finally:
        fixture.close()


def test_cascade_reports_empty_below_threshold():
    fixture = RetrieverFixture()
    try:
        result = fixture.retriever.search("quantum chromodynamics lattice gauge theory",
                                          min_score=0.99)
        assert result["empty"] is True
        assert result["evidences"] == []
        assert result.get("reason") == "apex_below_threshold"
    finally:
        fixture.close()


def test_cascade_stops_at_b_when_sufficient():
    fixture = RetrieverFixture()
    try:
        fixture.store.update({"retrieval": {"stop_at_b": True, "top_k_b": 2, "min_score": 0.05}})
        result = fixture.retriever.search("Transformer self-attention encoder decoder multi-head")
        layers = {item["layer"] for item in result["evidences"]}
        assert "B" in layers
    finally:
        fixture.close()


def test_cascade_ignores_layers_not_requested():
    fixture = RetrieverFixture()
    try:
        result = fixture.retriever.search("Transformer self-attention encoder decoder",
                                          layers=["C"])
        assert all(item["layer"] == "C" for item in result["evidences"])
        assert "B" not in result["layers_used"]
    finally:
        fixture.close()


def test_paper_briefs_and_sections_of():
    fixture = RetrieverFixture()
    try:
        briefs = fixture.retriever.paper_briefs([HASH_A, "missing"])
        assert len(briefs) == 1
        assert briefs[0]["title"] == "Attention Is All You Need"
        assert "Transformer" in briefs[0]["abstract"]

        sections = fixture.retriever.sections_of([HASH_A, HASH_B])
        assert sections == ["method", "experiment"]
    finally:
        fixture.close()


def test_hydrate_evidence_adds_paper_metadata():
    fixture = RetrieverFixture()
    try:
        evidence = [{"unit_id": "b:1", "hash": HASH_A, "layer": "B", "text": "x"}]
        enriched = fixture.retriever.hydrate_evidence(evidence)
        assert enriched[0]["paper_title"] == "Attention Is All You Need"
        assert enriched[0]["journal"] == "NeurIPS"
    finally:
        fixture.close()


# ---------------------------------------------------------------------------
# embedding provider factory
# ---------------------------------------------------------------------------

def test_embedding_factory_falls_back_to_lexical():
    from paperopenclaw.config import EmbeddingConfig

    with TempWorkspace() as tmp:
        provider = build_embedding_provider(
            EmbeddingConfig(provider="auto", model="does-not-exist"),
            models_dir=Path(tmp),
        )
        assert provider.name == "lexical"
        vectors = provider.encode(["测试文本"])
        assert len(vectors) == 1
        assert len(vectors[0]) == provider.dimension


def test_lexical_embedding_provider_is_stable():
    provider = LexicalEmbedding(dimension=128)
    assert provider.dimension == 128
    first = provider.encode_one("self attention")
    second = provider.encode_one("self attention")
    assert first == second
    assert len(first) == 128


def test_model_discovery_finds_plain_and_hub_layouts():
    """A model must be found whether it sits in a plain dir or an HF hub cache."""
    from paperopenclaw.config import EmbeddingConfig
    from paperopenclaw.memory.embeddings import _candidate_model_paths, _resolve_model_dir

    with TempWorkspace() as tmp:
        root = Path(tmp)
        model = "bge-small-zh-v1.5"

        # 1. Plain directory layout: <models>/<model>/
        plain = root / "models" / model
        plain.mkdir(parents=True)
        (plain / "config.json").write_text("{}", encoding="utf-8")
        (plain / "tokenizer.json").write_text("{}", encoding="utf-8")
        assert _resolve_model_dir(str(plain)) == str(plain)

        config = EmbeddingConfig(provider="auto", model=model)
        candidates = _candidate_model_paths(config, [root / "models"])
        assert str(plain) in candidates

        # 2. Hub cache layout: <hub>/models--<org>--<name>/snapshots/<rev>/
        snapshot = root / "hub" / f"models--BAAI--{model}" / "snapshots" / "abc123"
        snapshot.mkdir(parents=True)
        (snapshot / "config.json").write_text("{}", encoding="utf-8")
        (snapshot / "tokenizer.json").write_text("{}", encoding="utf-8")

        hub_root = root / "hub" / f"models--BAAI--{model}"
        resolved = _resolve_model_dir(str(hub_root))
        assert resolved == str(snapshot), f"未解析到快照目录：{resolved}"

        candidates = _candidate_model_paths(config, [root / "hub"])
        assert str(snapshot) in candidates, candidates

        # 3. An explicit path always wins.
        explicit = _candidate_model_paths(
            EmbeddingConfig(provider="local", model=model, model_path=str(plain)),
            [root / "hub"],
        )
        assert explicit[0] == str(plain)


def test_embedding_factory_degrades_when_model_is_incomplete():
    """A directory that looks like a model but cannot load must not break startup."""
    from paperopenclaw.config import EmbeddingConfig

    with TempWorkspace() as tmp:
        broken = Path(tmp) / "models" / "bge-small-zh-v1.5"
        broken.mkdir(parents=True)
        # config.json present but no weights: LocalTransformerEmbedding will fail.
        (broken / "config.json").write_text('{"hidden_size": 8}', encoding="utf-8")
        (broken / "tokenizer.json").write_text("{}", encoding="utf-8")

        provider = build_embedding_provider(
            EmbeddingConfig(provider="auto", model="bge-small-zh-v1.5"),
            models_dirs=[Path(tmp) / "models"],
        )
        assert provider.name == "lexical", f"应降级为词法方案，实际 {provider.name}"


# ---------------------------------------------------------------------------
# runner
# ---------------------------------------------------------------------------

def _run_all() -> int:
    return run_all(globals(), verbose=True)


if __name__ == "__main__":
    raise SystemExit(_run_all())
