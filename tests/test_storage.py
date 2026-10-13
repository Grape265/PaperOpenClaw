"""Storage-layer tests: schema, cascades, repositories, pyramid and fishbone.

    python tests/test_storage.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _harness import run_all  # noqa: E402

from paperopenclaw.core.paths import DataPaths  # noqa: E402
from paperopenclaw.kg import skeleton  # noqa: E402
from paperopenclaw.storage.db import Database, utc_now  # noqa: E402
from paperopenclaw.storage.repos import (  # noqa: E402
    FishboneRepository,
    JobRepository,
    KnowledgeGraphRepository,
    PaperRepository,
    RagRepository,
    SessionRepository,
    SkillRepository,
)
from paperopenclaw.storage.repos.rag import unit_id_a, unit_id_b, unit_id_c  # noqa: E402

HASH_A = "a" * 64
HASH_B = "b" * 64


class Context:
    """Per-test fixture holding a temporary database and every repository."""

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.paths = DataPaths(self._tmp.name).ensure_base()
        self.db = Database(self.paths.sqlite_file).initialise()
        self.papers = PaperRepository(self.db)
        self.rag = RagRepository(self.db)
        self.kg = KnowledgeGraphRepository(self.db)
        self.fishbone = FishboneRepository(self.db)
        self.sessions = SessionRepository(self.db)
        self.skills = SkillRepository(self.db)
        self.jobs = JobRepository(self.db)

    def close(self) -> None:
        self.db.close()
        self._tmp.cleanup()


def _seed_paper(ctx: Context, hash_value: str, title: str, year: int = 2024) -> None:
    ctx.paths.ensure_paper_dirs(hash_value)
    ctx.papers.create(hash_value, title=title, file_path=f"papers/{hash_value}/paper.pdf",
                      okf_path=f"papers/{hash_value}/okf", file_size=1234, status="ready")
    ctx.papers.update(
        hash_value,
        journal="NeurIPS", published_at=f"{year}-06-01", year=year,
        abstract="We propose a novel attention mechanism.",
        keywords=["attention", "transformer"], language="en", page_count=11,
    )


# ---------------------------------------------------------------------------
# schema
# ---------------------------------------------------------------------------

def test_schema_creates_all_tables():
    ctx = Context()
    try:
        names = {
            row["name"]
            for row in ctx.db.query_all("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        expected = {
            "papers", "authors", "affiliations", "author_affiliations", "assets",
            "okf_concepts", "rag_units", "rag_links", "kg_nodes", "kg_edges",
            "skills", "sessions", "agents", "messages", "jobs", "blackboard",
            "embedding_meta",
        }
        missing = expected - names
        assert not missing, f"缺少数据表：{missing}"
        assert ctx.db.integrity_check() == "ok"
    finally:
        ctx.close()


def test_initialise_is_idempotent():
    ctx = Context()
    try:
        ctx.db.initialise()
        ctx.db.initialise()
        assert ctx.db.count("papers") == 0
    finally:
        ctx.close()


def test_foreign_keys_are_enabled():
    ctx = Context()
    try:
        enabled = ctx.db.query_scalar("PRAGMA foreign_keys")
        assert int(enabled) == 1
    finally:
        ctx.close()


# ---------------------------------------------------------------------------
# papers (FR-2)
# ---------------------------------------------------------------------------

def test_paper_crud_and_hash_uniqueness():
    ctx = Context()
    try:
        _seed_paper(ctx, HASH_A, "Attention Is All You Need", 2017)
        assert ctx.papers.exists(HASH_A)

        paper = ctx.papers.get(HASH_A)
        assert paper["title"] == "Attention Is All You Need"
        assert paper["journal"] == "NeurIPS"
        assert paper["year"] == 2017

        # Re-creating must not duplicate: hash is the unique key (FR-2.2).
        ctx.papers.create(HASH_A, title="duplicate")
        assert ctx.db.count("papers") == 1

        assert ctx.papers.delete(HASH_A) is True
        assert ctx.papers.get(HASH_A) is None
    finally:
        ctx.close()


def test_paper_search_and_pagination():
    ctx = Context()
    try:
        _seed_paper(ctx, HASH_A, "Attention Is All You Need", 2017)
        _seed_paper(ctx, HASH_B, "Deep Residual Learning", 2016)
        ctx.papers.replace_authors_and_affiliations(
            HASH_B,
            [{"name": "Kaiming He", "affiliations": ["Microsoft Research"]}],
        )

        items, total = ctx.papers.list(q="residual")
        assert total == 1 and items[0]["hash"] == HASH_B

        items, total = ctx.papers.list(author="Kaiming")
        assert total == 1 and items[0]["hash"] == HASH_B

        items, total = ctx.papers.list(hash_value=HASH_A[:8])
        assert total == 1 and items[0]["hash"] == HASH_A

        items, total = ctx.papers.list(year_from=2017)
        assert total == 1 and items[0]["hash"] == HASH_A

        items, total = ctx.papers.list(page=1, page_size=1)
        assert total == 2 and len(items) == 1
        items, total = ctx.papers.list(page=2, page_size=1)
        assert len(items) == 1
    finally:
        ctx.close()


def test_authors_with_multiple_affiliations():
    ctx = Context()
    try:
        _seed_paper(ctx, HASH_A, "Multi Affiliation Paper")
        ctx.papers.replace_authors_and_affiliations(
            HASH_A,
            [
                {"name": "Alice", "affiliations": ["MIT", {"name": "Google Brain", "country": "US"}],
                 "corresponding": True, "email": "a@mit.edu"},
                {"name": "Bob", "affiliations": ["MIT"]},
            ],
            [{"name": "MIT", "department": "CSAIL", "country": "US"}],
        )
        authors = ctx.papers.list_authors(HASH_A)
        assert len(authors) == 2
        assert authors[0]["name"] == "Alice"
        assert authors[0]["is_corresponding"] == 1
        assert set(authors[0]["affiliations"]) == {"MIT", "Google Brain"}
        assert authors[1]["affiliations"] == ["MIT"]

        affiliations = ctx.papers.list_affiliations(HASH_A)
        assert {a["name"] for a in affiliations} == {"MIT", "Google Brain"}
        # The many-to-many table is shared: two authors, but only one MIT row.
        assert len(affiliations) == 2
    finally:
        ctx.close()


def test_counts_for_many_returns_zero_filled_rows():
    ctx = Context()
    try:
        _seed_paper(ctx, HASH_A, "A")
        _seed_paper(ctx, HASH_B, "B")
        counts = ctx.papers.counts_for_many([HASH_A, HASH_B])
        assert set(counts) == {HASH_A, HASH_B}
        assert counts[HASH_A]["figures"] == 0
        assert counts[HASH_A]["kg_nodes"] == 0
    finally:
        ctx.close()


# ---------------------------------------------------------------------------
# assets and OKF index (FR-4, FR-5)
# ---------------------------------------------------------------------------

def test_assets_stored_under_fig_and_tab_dirs():
    ctx = Context()
    try:
        _seed_paper(ctx, HASH_A, "With Figures")
        assets = [
            {"kind": "figure", "label": "fig1", "ordinal": 0, "page": 2,
             "bbox": [10, 20, 300, 400], "caption": "Architecture",
             "file_path": f"papers/{HASH_A}/fig/fig1.png", "width": 600, "height": 800},
            {"kind": "table", "label": "tab1", "ordinal": 0, "page": 5,
             "bbox": [5, 5, 200, 300], "caption": "Results",
             "file_path": f"papers/{HASH_A}/tab/tab1.png", "width": 400, "height": 600},
        ]
        ctx.papers.replace_assets(HASH_A, assets)

        stored = ctx.papers.list_assets(HASH_A)
        assert len(stored) == 2
        by_label = {item["label"]: item for item in stored}
        assert by_label["fig1"]["file_path"].endswith("/fig/fig1.png")
        assert by_label["tab1"]["file_path"].endswith("/tab/tab1.png")
        assert by_label["fig1"]["bbox"] == [10, 20, 300, 400]

        assert ctx.papers.get_asset(HASH_A, "fig1")["caption"] == "Architecture"
        assert ctx.papers.get_asset(HASH_A, "fig9") is None

        figures = ctx.papers.list_assets(HASH_A, kind="figure")
        assert len(figures) == 1 and figures[0]["label"] == "fig1"

        # Replacing must not leave orphans behind.
        ctx.papers.replace_assets(HASH_A, assets[:1])
        assert ctx.db.count("assets") == 1
    finally:
        ctx.close()


def test_okf_concepts_index():
    ctx = Context()
    try:
        _seed_paper(ctx, HASH_A, "OKF Paper")
        ctx.papers.replace_okf_concepts(HASH_A, [
            {"concept_id": "paper", "type": "Research Paper", "title": "OKF Paper",
             "file_path": f"papers/{HASH_A}/okf/paper.md", "ordinal": 0},
            {"concept_id": "sections/method", "type": "Section", "title": "方法",
             "section": "method", "file_path": f"papers/{HASH_A}/okf/sections/method.md", "ordinal": 1},
        ])
        concepts = ctx.papers.list_okf_concepts(HASH_A)
        assert len(concepts) == 2
        assert ctx.papers.get_okf_concept(HASH_A, "sections/method")["title"] == "方法"
    finally:
        ctx.close()


# ---------------------------------------------------------------------------
# RAG pyramid (FR-6)
# ---------------------------------------------------------------------------

def test_pyramid_units_links_and_cross_links():
    ctx = Context()
    try:
        _seed_paper(ctx, HASH_A, "Paper A")
        _seed_paper(ctx, HASH_B, "Paper B")

        for hash_value, title in ((HASH_A, "Paper A"), (HASH_B, "Paper B")):
            ctx.rag.replace_units(hash_value, [
                {"id": unit_id_c(hash_value), "layer": "C", "text": f"{title} abstract",
                 "ordinal": 0, "title": title},
                {"id": unit_id_b(hash_value, "method"), "layer": "B", "section": "method",
                 "text": f"{title} method digest", "ordinal": 1, "title": "方法"},
                {"id": unit_id_a(hash_value, 0), "layer": "A", "section": "method",
                 "text": f"{title} detailed method", "ordinal": 0, "title": "Method"},
            ])

        # A -> B and B -> C links (FR-6.5).
        assert ctx.rag.add_links([
            (unit_id_a(HASH_A, 0), unit_id_b(HASH_A, "method"), "a_to_b", 1.0, None),
            (unit_id_b(HASH_A, "method"), unit_id_c(HASH_A), "b_to_c", 1.0, None),
            (unit_id_a(HASH_B, 0), unit_id_b(HASH_B, "method"), "a_to_b", 1.0, None),
            (unit_id_b(HASH_B, "method"), unit_id_c(HASH_B), "b_to_c", 1.0, None),
        ]) == 4

        assert ctx.rag.expand([unit_id_a(HASH_A, 0)], "a_to_b") == [unit_id_b(HASH_A, "method")]
        assert ctx.rag.expand([unit_id_b(HASH_A, "method")], "b_to_c") == [unit_id_c(HASH_A)]
        assert ctx.rag.expand_reverse([unit_id_b(HASH_A, "method")], "a_to_b") == [unit_id_a(HASH_A, 0)]

        # C -> C across papers (FR-6.6).
        assert ctx.rag.add_links([
            (unit_id_c(HASH_A), unit_id_c(HASH_B), "c_to_c", 0.72, {"reason": "similar"}),
            (unit_id_c(HASH_B), unit_id_c(HASH_A), "c_to_c", 0.72, {"reason": "similar"}),
        ]) == 2
        cross = ctx.rag.cross_links(unit_id_c(HASH_A))
        assert len(cross) == 1
        assert cross[0]["hash"] == HASH_B
        assert cross[0]["weight"] == 0.72

        pyramid = ctx.rag.pyramid(HASH_A)
        assert pyramid["C"]["unit_id"] == unit_id_c(HASH_A)
        assert len(pyramid["B"]) == 1 and len(pyramid["A"]) == 1
        assert pyramid["C"]["cross_links"][0]["hash"] == HASH_B
        assert pyramid["B"][0]["parents"] == [unit_id_c(HASH_A)]
        assert pyramid["B"][0]["children"] == [unit_id_a(HASH_A, 0)]
        assert pyramid["counts"] == {"A": 1, "B": 1, "C": 1}
    finally:
        ctx.close()


def test_replace_units_clears_old_links():
    ctx = Context()
    try:
        _seed_paper(ctx, HASH_A, "Rebuild Paper")
        ctx.rag.replace_units(HASH_A, [
            {"id": unit_id_c(HASH_A), "layer": "C", "text": "abstract"},
            {"id": unit_id_b(HASH_A, "method"), "layer": "B", "section": "method", "text": "digest"},
            {"id": unit_id_a(HASH_A, 0), "layer": "A", "section": "method", "text": "detail"},
        ])
        ctx.rag.add_links([(unit_id_b(HASH_A, "method"), unit_id_c(HASH_A), "b_to_c", 1.0, None)])
        assert ctx.db.count("rag_links") == 1

        ctx.rag.replace_units(HASH_A, [
            {"id": unit_id_c(HASH_A), "layer": "C", "text": "abstract v2"},
        ])
        assert ctx.db.count("rag_units") == 1
        assert ctx.db.count("rag_links") == 0
    finally:
        ctx.close()


def test_unit_lookup_filters_by_layer_and_hash():
    ctx = Context()
    try:
        _seed_paper(ctx, HASH_A, "A")
        _seed_paper(ctx, HASH_B, "B")
        ctx.rag.replace_units(HASH_A, [{"id": unit_id_c(HASH_A), "layer": "C", "text": "x"}])
        ctx.rag.replace_units(HASH_B, [{"id": unit_id_c(HASH_B), "layer": "C", "text": "y"}])
        assert len(ctx.rag.unit_lookup()) == 2
        assert len(ctx.rag.unit_lookup(hashes=[HASH_A])) == 1
        assert len(ctx.rag.unit_lookup(layers=["B"])) == 0
    finally:
        ctx.close()


# ---------------------------------------------------------------------------
# knowledge graph (FR-7)
# ---------------------------------------------------------------------------

def test_fishbone_spine_and_asset_links():
    ctx = Context()
    try:
        _seed_paper(ctx, HASH_A, "Graph Paper")
        ctx.papers.replace_assets(HASH_A, [
            {"kind": "figure", "label": "fig1", "ordinal": 0, "page": 3,
             "file_path": f"papers/{HASH_A}/fig/fig1.png", "caption": "Overview"},
        ])
        asset = ctx.papers.get_asset(HASH_A, "fig1")

        assert ctx.fishbone.ensure_spine_nodes() == 7
        from paperopenclaw.storage.repos.fishbone import asset_key, content_key, paper_key, spine_key

        ctx.fishbone.upsert_nodes([
            {"node_key": paper_key(HASH_A), "kind": "paper", "paper_hash": HASH_A,
             "label": "Graph Paper", "ordinal": 0},
            {"node_key": content_key(HASH_A, "method", 1), "kind": "content", "paper_hash": HASH_A,
             "section": "method", "label": "Transformer encoder", "detail": "详细内容",
             "ordinal": 1, "unit_id": unit_id_b(HASH_A, "method")},
            {"node_key": asset_key(HASH_A, "fig1"), "kind": "asset", "paper_hash": HASH_A,
             "section": "method", "label": "fig1", "detail": "Overview", "ordinal": 0,
             "asset_id": asset["id"],
             "meta": {"path": asset["file_path"], "asset_kind": "figure",
                      "url": f"/api/v1/papers/{HASH_A}/assets/fig1"}},
        ])
        ctx.fishbone.add_edges([
            (paper_key(HASH_A), spine_key("method"), "has_spine", 1.0, None),
            (content_key(HASH_A, "method", 1), spine_key("method"), "belongs_to", 1.0, None),
            (paper_key(HASH_A), content_key(HASH_A, "method", 1), "has_content", 1.0, None),
            (content_key(HASH_A, "method", 1), asset_key(HASH_A, "fig1"), "has_asset", 1.0, None),
        ])

        spine = ctx.fishbone.fishbone(HASH_A)
        assert len(spine) == 7
        assert [item["slug"] for item in spine] == skeleton.SPINE_SLUGS
        assert [item["label"] for item in spine] == ["背景", "数据", "方法", "实验", "结论", "讨论", "不足与展望"]

        method = next(item for item in spine if item["slug"] == "method")
        assert len(method["nodes"]) == 1
        node = method["nodes"][0]
        assert node["label"] == "Transformer encoder"
        # FR-7.4: the asset is mounted as a path link, not copied bytes.
        assert len(node["assets"]) == 1
        assert node["assets"][0]["url"].endswith(f"/papers/{HASH_A}/assets/fig1")
        assert node["assets"][0]["kind"] == "figure"

        empty = next(item for item in spine if item["slug"] == "data")
        assert empty["nodes"] == []
    finally:
        ctx.close()


def test_kg_subgraph_and_search():
    ctx = Context()
    try:
        _seed_paper(ctx, HASH_A, "Sub")
        _seed_paper(ctx, HASH_B, "Sub2")
        from paperopenclaw.storage.repos.kg import keyword_key, paper_key

        ctx.kg.upsert_nodes([
            {"node_key": paper_key(HASH_A), "kind": "paper", "paper_hash": HASH_A, "label": "Sub"},
            {"node_key": paper_key(HASH_B), "kind": "paper", "paper_hash": HASH_B, "label": "Sub2"},
            {"node_key": keyword_key("attention"), "kind": "keyword",
             "paper_hash": "", "label": "attention"},
            {"node_key": keyword_key("graph"), "kind": "keyword",
             "paper_hash": "", "label": "graph"},
        ])
        ctx.kg.add_edges([
            (paper_key(HASH_A), keyword_key("attention"), "has_keyword", 1.0, None),
            (paper_key(HASH_B), keyword_key("graph"), "has_keyword", 1.0, None),
        ])

        graph_all = ctx.kg.subgraph()
        assert len(graph_all["nodes"]) == 4
        assert len(graph_all["edges"]) == 2

        graph_a = ctx.kg.subgraph(hashes=[HASH_A])
        assert len(graph_a["nodes"]) == 2  # paper + its keyword
        assert len(graph_a["edges"]) == 1

        hits = ctx.kg.search("attention")
        assert len(hits) == 1 and hits[0]["label"] == "attention"
        assert hits[0]["kind"] == "keyword"
    finally:
        ctx.close()


def test_kg_delete_paper_removes_isolated_nodes():
    ctx = Context()
    try:
        _seed_paper(ctx, HASH_A, "Doomed")
        from paperopenclaw.storage.repos.kg import keyword_key, paper_key

        ctx.kg.upsert_nodes([
            {"node_key": paper_key(HASH_A), "kind": "paper", "paper_hash": HASH_A, "label": "Doomed"},
            {"node_key": keyword_key("alpha"), "kind": "keyword",
             "paper_hash": "", "label": "alpha"},
        ])
        ctx.kg.add_edges([(paper_key(HASH_A), keyword_key("alpha"),
                           "has_keyword", 1.0, None)])
        assert ctx.kg.stats()["edges"] == 1

        ctx.kg.delete_paper(HASH_A)
        assert ctx.kg.stats()["edges"] == 0
        assert ctx.kg.stats()["nodes"] == 0
    finally:
        ctx.close()


# ---------------------------------------------------------------------------
# sessions and agents (FR-10)
# ---------------------------------------------------------------------------

def test_session_agent_isolation_and_blackboard():
    ctx = Context()
    try:
        ctx.sessions.create("s1", kind="brainstorm", topic="Topic", paper_hashes=[HASH_A, HASH_B])
        session = ctx.sessions.get("s1")
        assert session["paper_hashes"] == [HASH_A, HASH_B]

        ctx.sessions.add_agent("s1", "methodologist", name="方法学家", persona="关注方法",
                               tools=["rag_search"], paper_hashes=[HASH_A], workspace="agents/s1/methodologist")
        ctx.sessions.add_agent("s1", "skeptic", name="怀疑论者", persona="质疑",
                               tools=["rag_search", "okf_read"], paper_hashes=[HASH_B],
                               workspace="agents/s1/skeptic")
        agents = ctx.sessions.list_agents("s1")
        assert len(agents) == 2
        assert agents[0]["tools"] == ["rag_search"]
        assert agents[1]["paper_hashes"] == [HASH_B]

        # Histories are isolated per agent (FR-10.2).
        ctx.sessions.add_message("s1", role="assistant", content="方法观点", agent_id="methodologist", round_index=1)
        ctx.sessions.add_message("s1", role="assistant", content="怀疑观点", agent_id="skeptic", round_index=1)
        assert len(ctx.sessions.list_messages("s1")) == 2
        assert len(ctx.sessions.list_messages("s1", agent_id="methodologist")) == 1
        assert len(ctx.sessions.history_for_agent("s1", "methodologist")) == 1

        # The blackboard is the shared channel (FR-10.5).
        ctx.sessions.add_blackboard("s1", round_index=1, content="共享摘要", entry_type="summary")
        assert len(ctx.sessions.list_blackboard("s1")) == 1
        assert ctx.sessions.list_blackboard("s1", round_index=2) == []

        # Deleting the session cascades to agents, messages and blackboard.
        assert ctx.sessions.delete("s1") is True
        assert ctx.db.count("agents") == 0
        assert ctx.db.count("messages") == 0
        assert ctx.db.count("blackboard") == 0
    finally:
        ctx.close()


# ---------------------------------------------------------------------------
# skills (FR-9)
# ---------------------------------------------------------------------------

def test_skill_registry():
    ctx = Context()
    try:
        ctx.skills.upsert("pdf-tools", name="PDF 工具", description="裁剪图表",
                          version="1.0.0", path="skills/pdf-tools",
                          tools=["crop_figure"], instructions="使用说明",
                          frontmatter={"name": "pdf-tools"})
        ctx.skills.upsert("stats", name="统计", path="skills/stats", tools=["describe"])
        assert len(ctx.skills.list()) == 2
        assert len(ctx.skills.list(enabled_only=True)) == 2
        assert set(ctx.skills.declared_tools()) == {"crop_figure", "describe"}

        ctx.skills.set_enabled("stats", False)
        assert len(ctx.skills.list(enabled_only=True)) == 1
        assert ctx.skills.declared_tools() == ["crop_figure"]

        assert ctx.skills.delete("stats") is True
        assert ctx.skills.get("stats") is None
    finally:
        ctx.close()


# ---------------------------------------------------------------------------
# jobs (FR-1.5)
# ---------------------------------------------------------------------------

def test_job_lifecycle_and_usage_accumulation():
    ctx = Context()
    try:
        ctx.jobs.create("j1", kind="ingest", hash_value=HASH_A, filename="a.pdf")
        job = ctx.jobs.get("j1")
        assert job["status"] == "pending" and job["stage_total"] == 7

        ctx.jobs.start("j1")
        ctx.jobs.set_stage("j1", "layout", "开始裁剪")
        ctx.jobs.finish_stage("j1", "layout", "图 5 表 3")
        ctx.jobs.add_usage("j1", {"prompt_tokens": 100, "completion_tokens": 20})
        ctx.jobs.add_usage("j1", {"prompt_tokens": 50, "completion_tokens": 10})

        job = ctx.jobs.get("j1")
        assert job["status"] == "running"
        assert job["stage"] == "layout"
        assert job["usage"]["prompt_tokens"] == 150
        assert len(job["stages"]) == 1
        assert job["stages"][0]["finished_at"] is not None
        assert job["progress"] > 0

        ctx.jobs.complete("j1")
        job = ctx.jobs.get("j1")
        assert job["status"] == "done" and job["progress"] == 1.0

        items, total = ctx.jobs.list(status="done")
        assert total == 1 and items[0]["id"] == "j1"
    finally:
        ctx.close()


def test_job_failure_and_stale_cleanup():
    ctx = Context()
    try:
        ctx.jobs.create("j1")
        ctx.jobs.create("j2")
        ctx.jobs.start("j1")
        assert len(ctx.jobs.running_jobs()) == 1

        changed = ctx.jobs.mark_stale_running_as_failed()
        assert changed == 2  # one running, one pending
        assert ctx.jobs.get("j1")["status"] == "failed"
        assert ctx.jobs.get("j1")["error"]

        ctx.jobs.create("j3")
        ctx.jobs.cancel("j3")
        assert ctx.jobs.is_cancelled("j3") is True
        assert ctx.jobs.is_cancelled("j1") is False
    finally:
        ctx.close()


def test_job_prune_finished():
    ctx = Context()
    try:
        for index in range(5):
            ctx.jobs.create(f"j{index}")
            ctx.jobs.complete(f"j{index}")
        assert ctx.db.count("jobs") == 5
        removed = ctx.jobs.prune_finished(keep=2)
        assert removed == 3
        assert ctx.db.count("jobs") == 2
    finally:
        ctx.close()


# ---------------------------------------------------------------------------
# cross-store integrity (FR-13)
# ---------------------------------------------------------------------------

def test_deleting_paper_cascades_across_tables():
    ctx = Context()
    try:
        _seed_paper(ctx, HASH_A, "Cascade")
        ctx.papers.replace_authors_and_affiliations(
            HASH_A, [{"name": "A", "affiliations": ["Inst"]}])
        ctx.papers.replace_assets(HASH_A, [
            {"kind": "figure", "label": "fig1", "file_path": f"papers/{HASH_A}/fig/fig1.png"}])
        ctx.papers.replace_okf_concepts(HASH_A, [
            {"concept_id": "paper", "type": "Research Paper", "file_path": "x.md"}])
        ctx.rag.replace_units(HASH_A, [{"id": unit_id_c(HASH_A), "layer": "C", "text": "a"}])

        from paperopenclaw.storage.repos.kg import paper_key
        from paperopenclaw.storage.repos.fishbone import content_key
        ctx.kg.upsert_nodes([{"node_key": paper_key(HASH_A), "kind": "paper",
                              "paper_hash": HASH_A, "label": "Cascade"}])
        ctx.fishbone.upsert_nodes([{"node_key": content_key(HASH_A, "method", 1),
                                    "kind": "content", "paper_hash": HASH_A,
                                    "section": "method", "label": "n"}])

        assert ctx.db.count("authors") == 1
        assert ctx.db.count("assets") == 1
        assert ctx.db.count("okf_concepts") == 1
        assert ctx.db.count("rag_units") == 1
        assert ctx.db.count("kg_nodes") == 1
        assert ctx.db.count("fishbone_nodes") == 1

        ctx.papers.delete(HASH_A)

        for table in ("authors", "affiliations", "author_affiliations", "assets",
                      "okf_concepts", "rag_units", "kg_nodes", "fishbone_nodes"):
            assert ctx.db.count(table) == 0, f"{table} 未被级联清理"
    finally:
        ctx.close()


# ---------------------------------------------------------------------------
# runner
# ---------------------------------------------------------------------------

def _run_all() -> int:
    return run_all(globals(), verbose=True)


if __name__ == "__main__":
    raise SystemExit(_run_all())
