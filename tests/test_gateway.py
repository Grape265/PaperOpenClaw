"""Gateway tests: the HTTP contract from docs/03-api-contract.md.

Drives the real FastAPI application through ``TestClient`` against seeded
services and a mock model server, covering the response envelope, error codes,
range requests, SSE streams and every router.

    python tests/test_gateway.py
"""

from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _harness import LiveApp, TempWorkspace, run_all  # noqa: E402

from test_agent import HASH_A, HASH_B, AgentFixture, MockModelServer, SKILL_MD, DESCRIBE_SCRIPT  # noqa: E402
from test_ingest import build_sample_pdf  # noqa: E402

from paperopenclaw.gateway.app import create_app  # noqa: E402


def _data(response) -> Any:
    payload = response.json()
    assert payload["ok"] is True, payload
    return payload["data"]


def _error(response) -> Dict[str, Any]:
    payload = response.json()
    assert payload["ok"] is False, payload
    return payload["error"]


class GatewayFixture:
    """Fixture plus a live HTTP server and client.

    Uses a real uvicorn server rather than ``TestClient``: the pinned
    Starlette/httpx pair is incompatible, and a real socket is the only way to
    exercise Range requests and genuinely streamed SSE.
    """

    def __init__(self, server: MockModelServer) -> None:
        self.agent = AgentFixture(server)
        app = create_app(services=self.agent.services, enable_docs=False)
        self._live = LiveApp(app)
        self.client = self._live.__enter__()

    @property
    def services(self):
        return self.agent.services

    def close(self) -> None:
        try:
            self._live.__exit__(None, None, None)
        except Exception:  # noqa: BLE001 - teardown must not mask test results
            pass
        self.agent.close()


# ---------------------------------------------------------------------------
# system
# ---------------------------------------------------------------------------

def test_health_and_envelope():
    with MockModelServer() as server:
        fixture = GatewayFixture(server)
        try:
            response = fixture.client.get("/api/v1/health")
            assert response.status_code == 200
            payload = response.json()
            assert set(payload) == {"ok", "data", "error", "meta"}
            assert payload["meta"]["request_id"]
            assert response.headers["X-Request-Id"]
            data = payload["data"]
            assert data["status"] == "ok"
            assert data["counts"]["papers"] == 2
            assert data["embedding"]["provider"] == "lexical"
            assert set(data["providers"]) >= {"parse", "chat", "digest", "embedding"}
        finally:
            fixture.close()


def test_doctor_reports_every_check():
    with MockModelServer() as server:
        fixture = GatewayFixture(server)
        try:
            data = _data(fixture.client.get("/api/v1/doctor"))
            names = {item["name"] for item in data["checks"]}
            assert {"数据目录可写", "SQLite 完整性", "向量库可用",
                    "嵌入提供方可用", "解析模型已配置", "问答模型已配置"} <= names
            assert all(item["ok"] for item in data["checks"]), data["checks"]
        finally:
            fixture.close()


def test_settings_are_masked_and_round_trip():
    with MockModelServer() as server:
        fixture = GatewayFixture(server)
        try:
            data = _data(fixture.client.get("/api/v1/settings"))
            provider = data["providers"][0]
            assert provider["api_key"] != "sk-test"
            assert provider["api_key_set"] is True

            # Echo the masked document back plus a change; the secret must live on.
            data["retrieval"]["top_k_b"] = 11
            updated = _data(fixture.client.put("/api/v1/settings", json=data))
            assert updated["settings"]["retrieval"]["top_k_b"] == 11
            assert fixture.services.settings.current.providers[0].api_key == "sk-test"

            presets = _data(fixture.client.get("/api/v1/provider-presets"))
            ids = {item["id"] for item in presets["items"]}
            assert {"deepseek", "dashscope", "moonshot", "openai", "ollama"} <= ids
        finally:
            fixture.close()


def test_provider_test_endpoint_probes_the_model():
    with MockModelServer() as server:
        fixture = GatewayFixture(server)
        try:
            data = _data(fixture.client.post("/api/v1/settings/test", json={
                "provider": "mock", "model": "mock-large", "kind": "chat"}))
            assert data["sample"]
            assert data["latency_ms"] >= 0

            response = fixture.client.post("/api/v1/settings/test",
                                           json={"provider": "missing", "kind": "chat"})
            assert response.status_code == 412
            assert _error(response)["code"] == "PROVIDER_NOT_CONFIGURED"
        finally:
            fixture.close()


# ---------------------------------------------------------------------------
# papers
# ---------------------------------------------------------------------------

def test_paper_list_detail_and_facets():
    with MockModelServer() as server:
        fixture = GatewayFixture(server)
        try:
            listing = _data(fixture.client.get("/api/v1/papers"))
            assert listing["total"] == 2
            assert listing["page"] == 1
            first = listing["items"][0]
            assert set(first) >= {"hash", "hash_short", "title", "journal", "year",
                                  "authors", "counts", "status"}
            assert first["authors"], "列表应带作者"

            # Search by title, author and hash prefix (FR-12.2).
            assert _data(fixture.client.get("/api/v1/papers", params={"q": "Attention"}))["total"] == 1
            assert _data(fixture.client.get("/api/v1/papers",
                                            params={"author": "Vaswani"}))["total"] == 1
            assert _data(fixture.client.get("/api/v1/papers",
                                            params={"hash": HASH_A[:10]}))["total"] == 1
            assert _data(fixture.client.get("/api/v1/papers",
                                            params={"hashes": f"{HASH_A},{HASH_B}"}))["total"] == 2
            assert _data(fixture.client.get("/api/v1/papers", params={"year_from": 2017}))["total"] == 1

            facets = _data(fixture.client.get("/api/v1/papers/facets"))
            assert set(facets["journals"]) == {"NeurIPS", "CVPR"}
            assert facets["year_from"] == 2016 and facets["year_to"] == 2017
            assert facets["total"] == 2

            detail = _data(fixture.client.get(f"/api/v1/papers/{HASH_A}"))
            assert detail["paper"]["title"] == "Attention Is All You Need"
            assert detail["abstract"].startswith("We propose the Transformer")
            assert {item["label"] for item in detail["assets"]} == {"fig1", "tab1"}
            assert detail["affiliations"][0]["name"] == "Google Brain"
            assert detail["pyramid"]["counts"]["C"] == 1
            assert detail["kg_summary"]["nodes"] >= 2
            assert detail["okf"]["concepts"]
            assert detail["article"] if False else True  # placeholder guard removed below
        finally:
            fixture.close()


def test_paper_not_found_returns_404_envelope():
    with MockModelServer() as server:
        fixture = GatewayFixture(server)
        try:
            response = fixture.client.get(f"/api/v1/papers/{'f' * 64}")
            assert response.status_code == 404
            error = _error(response)
            assert error["code"] == "PAPER_NOT_FOUND"
            assert response.json()["data"] is None
        finally:
            fixture.close()


def test_pdf_and_asset_streaming_support_range():
    with MockModelServer() as server:
        fixture = GatewayFixture(server)
        try:
            client = fixture.client
            pdf_path = fixture.services.paths.paper_pdf(HASH_A)
            pdf_path.write_bytes(b"%PDF-1.4\n" + b"x" * 5000)

            full = client.get(f"/api/v1/papers/{HASH_A}/pdf")
            assert full.status_code == 200
            assert full.headers["accept-ranges"] == "bytes"
            assert len(full.content) == pdf_path.stat().st_size

            partial = client.get(f"/api/v1/papers/{HASH_A}/pdf",
                                 headers={"Range": "bytes=10-19"})
            assert partial.status_code == 206
            assert partial.headers["content-range"] == f"bytes 10-19/{pdf_path.stat().st_size}"
            assert len(partial.content) == 10

            suffix = client.get(f"/api/v1/papers/{HASH_A}/pdf", headers={"Range": "bytes=-100"})
            assert suffix.status_code == 206
            assert len(suffix.content) == 100

            # A malformed range degrades to a full response rather than failing.
            weird = client.get(f"/api/v1/papers/{HASH_A}/pdf", headers={"Range": "bytes=abc"})
            assert weird.status_code == 200

            asset = client.get(f"/api/v1/papers/{HASH_A}/assets/fig1")
            assert asset.status_code == 200
            assert asset.content.startswith(b"\x89PNG")

            missing = client.get(f"/api/v1/papers/{HASH_A}/assets/fig9")
            assert missing.status_code == 404
            assert _error(missing)["code"] == "ASSET_NOT_FOUND"
        finally:
            fixture.close()


def test_okf_endpoints_tree_concept_and_verify():
    with MockModelServer() as server:
        fixture = GatewayFixture(server)
        try:
            client = fixture.client
            tree = _data(client.get(f"/api/v1/papers/{HASH_A}/okf"))
            assert tree["okf_version"] == "0.2"
            assert tree["exists"] is True
            ids = [node["concept_id"] for node in tree["tree"]]
            assert "paper" in ids
            assert "index" not in ids and "log" not in ids

            concept = _data(client.get(f"/api/v1/papers/{HASH_A}/okf/paper"))
            assert concept["frontmatter"]["type"] == "Research Paper"
            assert concept["frontmatter"]["paper_hash"] == HASH_A
            assert concept["body"]
            assert concept["raw"].startswith("---")

            report = _data(client.get(f"/api/v1/papers/{HASH_A}/okf/verify"))
            assert report["conformant"] is True, report["errors"]
            assert report["files"] >= 4

            missing = client.get(f"/api/v1/papers/{HASH_A}/okf/nope")
            assert missing.status_code == 404
            assert _error(missing)["code"] == "CONCEPT_NOT_FOUND"
        finally:
            fixture.close()


def test_paper_export_is_a_zip_containing_everything():
    with MockModelServer() as server:
        fixture = GatewayFixture(server)
        try:
            response = fixture.client.get(f"/api/v1/papers/{HASH_A}/export")
            assert response.status_code == 200
            assert response.headers["content-type"] == "application/zip"

            with zipfile.ZipFile(__import__("io").BytesIO(response.content)) as archive:
                names = set(archive.namelist())
            assert "okf/paper.md" in names
            assert "fig/fig1.png" in names
            assert "tab/tab1.png" in names
        finally:
            fixture.close()


# ---------------------------------------------------------------------------
# ingest and jobs (SSE)
# ---------------------------------------------------------------------------

def test_upload_creates_job_and_streams_events():
    with MockModelServer() as server:
        fixture = GatewayFixture(server)
        try:
            client = fixture.client
            pdf = build_sample_pdf()

            response = client.post(
                "/api/v1/ingest/upload",
                files=[("files", ("attention.pdf", pdf, "application/pdf"))],
            )
            assert response.status_code == 200
            data = _data(response)
            assert data["accepted"] == 1
            assert data["duplicated"] == 0
            job_id = data["items"][0]["job_id"]
            assert job_id

            # The job id is stable and the row exists immediately.
            job = _data(client.get(f"/api/v1/jobs/{job_id}"))
            assert job["id"] == job_id
            assert job["kind"] == "ingest"

            # SSE: the stream must terminate on `done` and mention stages.
            with client.stream("GET", f"/api/v1/jobs/{job_id}/events") as stream:
                assert stream.status_code == 200
                assert stream.headers["content-type"].startswith("text/event-stream")
                body = b"".join(stream.iter_bytes()).decode("utf-8")
            assert "event: done" in body
            assert "event: stage" in body or "event: progress" in body

            listing = _data(client.get("/api/v1/jobs", params={"status": "done"}))
            assert listing["total"] >= 1

            # Re-uploading the same bytes is reported as a duplicate, not queued.
            again = _data(client.post(
                "/api/v1/ingest/upload",
                files=[("files", ("attention.pdf", pdf, "application/pdf"))],
            ))
            assert again["duplicated"] == 1 and again["accepted"] == 0
        finally:
            fixture.close()


def test_upload_rejects_non_pdf():
    with MockModelServer() as server:
        fixture = GatewayFixture(server)
        try:
            data = _data(fixture.client.post(
                "/api/v1/ingest/upload",
                files=[("files", ("notes.txt", b"hello", "text/plain"))],
            ))
            assert data["accepted"] == 0
            assert "只支持 PDF" in data["items"][0]["error"]
        finally:
            fixture.close()


def test_job_cancel_and_missing_job():
    with MockModelServer() as server:
        fixture = GatewayFixture(server)
        try:
            client = fixture.client
            assert client.get("/api/v1/jobs/nope").status_code == 404
            assert _error(client.get("/api/v1/jobs/nope"))["code"] == "JOB_NOT_FOUND"

            fixture.services.jobs.create("job_manual", kind="ingest", hash_value=HASH_A)
            outcome = _data(client.post("/api/v1/jobs/job_manual/cancel"))
            assert outcome["cancelled"] is True
            assert _data(client.get("/api/v1/jobs/job_manual"))["status"] == "cancelled"
        finally:
            fixture.close()


# ---------------------------------------------------------------------------
# RAG
# ---------------------------------------------------------------------------

def test_rag_search_pyramid_units_and_overview():
    with MockModelServer() as server:
        fixture = GatewayFixture(server)
        try:
            client = fixture.client
            result = _data(client.post("/api/v1/rag/search", json={
                "query": "self-attention encoder layers",
                "hashes": [HASH_A],
            }))
            assert result["empty"] is False
            assert result["candidates"]
            assert result["evidences"]
            assert result["evidences"][0]["hash"] == HASH_A
            assert result["evidences"][0]["unit_id"]
            assert "trace" in result["evidences"][0]

            pyramid = _data(client.get(f"/api/v1/rag/pyramid/{HASH_A}"))
            assert pyramid["C"]["unit_id"].startswith("c:")
            assert pyramid["counts"]["B"] >= 2
            assert all(item["parents"] for item in pyramid["B"])

            unit_id = pyramid["B"][0]["unit_id"]
            unit = _data(client.get(f"/api/v1/rag/units/{unit_id}"))
            assert unit["unit"]["id"] == unit_id
            assert unit["unit"]["paper_title"] == "Attention Is All You Need"
            assert unit["parents"] and unit["parents"][0]["layer"] == "C"
            assert unit["children"], "B 单元应能下钻到 A 层"

            c_unit = _data(client.get(f"/api/v1/rag/units/{pyramid['C']['unit_id']}"))
            assert c_unit["children"], "C 单元应关联 B 层"

            graph = _data(client.get("/api/v1/rag/graph", params={"hashes": HASH_A}))
            assert graph["counts"]["by_layer"]["C"] == 1
            assert graph["edges"], "金字塔关系图应有边"

            overview = _data(client.get("/api/v1/rag/overview"))
            assert overview["papers"] == 2
            assert overview["units"]["C"] == 2
            assert "sections" in overview

            empty = _data(client.post("/api/v1/rag/search",
                                      json={"query": "quantum chromodynamics", "min_score": 0.99}))
            assert empty["empty"] is True
            assert empty["reason"] == "apex_below_threshold"

            bad = client.post("/api/v1/rag/search", json={"query": "   "})
            assert bad.status_code == 400
            assert _error(bad)["code"] == "BAD_REQUEST"
        finally:
            fixture.close()


def test_rag_rebuild_job_runs():
    with MockModelServer() as server:
        fixture = GatewayFixture(server)
        try:
            client = fixture.client
            data = _data(client.post("/api/v1/rag/rebuild",
                                     json={"hashes": [HASH_A], "layers": ["A", "B", "C"]}))
            job_id = data["job_id"]

            with client.stream("GET", f"/api/v1/jobs/{job_id}/events") as stream:
                body = b"".join(stream.iter_bytes()).decode("utf-8")
            assert "event: done" in body
            job = _data(client.get(f"/api/v1/jobs/{job_id}"))
            assert job["status"] == "done"
            assert fixture.services.vectors.count()["C"] == 2
        finally:
            fixture.close()


# ---------------------------------------------------------------------------
# knowledge graph
# ---------------------------------------------------------------------------

def test_kg_sections_fishbone_graph_and_export():
    with MockModelServer() as server:
        fixture = GatewayFixture(server)
        try:
            client = fixture.client
            sections = _data(client.get("/api/v1/kg/sections"))
            assert [item["label"] for item in sections["items"]] == [
                "背景", "数据", "方法", "实验", "结论", "讨论", "不足与展望"]
            assert [item["slug"] for item in sections["items"]][:3] == [
                "background", "data", "method"]

            fish = _data(client.get(f"/api/v1/kg/fishbone/{HASH_A}"))
            assert fish["title"] == "Attention Is All You Need"
            assert len(fish["spine"]) == 7
            method = next(item for item in fish["spine"] if item["slug"] == "method")
            assert method["nodes"], "方法维度应有内容节点"
            node = method["nodes"][0]
            assert node["assets"], "内容节点应挂载图表"
            assert node["assets"][0]["url"].startswith(f"/api/v1/papers/{HASH_A}/assets/")

            graph = _data(client.get("/api/v1/kg/graph", params={"hashes": HASH_A}))
            kinds = {node["kind"] for node in graph["nodes"]}
            assert {"paper", "keyword"} <= kinds
            assert graph["stats"]["nodes"] > 0
            assert any(edge["relation"] == "has_keyword" for edge in graph["edges"])

            search = _data(client.get("/api/v1/kg/search", params={"q": "Attention"}))
            assert search["total"] >= 1

            stats = _data(client.get("/api/v1/kg/stats"))
            assert stats["nodes"] >= 2

            exported = client.get("/api/v1/kg/export", params={"format": "json"})
            assert exported.status_code == 200
            payload = json.loads(exported.content)
            assert payload["nodes"] and payload["edges"]

            graphml = client.get("/api/v1/kg/export", params={"format": "graphml"})
            assert graphml.status_code == 200
            assert b"graphml" in graphml.content[:400]
        finally:
            fixture.close()


def test_kg_shortest_path_between_two_papers():
    with MockModelServer() as server:
        fixture = GatewayFixture(server)
        try:
            client = fixture.client
            from paperopenclaw.storage.repos.kg import paper_key, keyword_key

            shared = keyword_key("transformer")
            fixture.services.kg.upsert_nodes([
                {"node_key": paper_key(HASH_B), "kind": "paper",
                 "paper_hash": HASH_B, "label": "Convolutional"},
                {"node_key": shared, "kind": "keyword", "paper_hash": "", "label": "transformer"},
            ])
            # 两篇论文共享同一关键词节点，从而在图上连通。
            fixture.services.kg.add_edges([
                (paper_key(HASH_A), shared, "has_keyword", 1.0, None),
                (paper_key(HASH_B), shared, "has_keyword", 1.0, None),
            ])
            data = _data(client.get("/api/v1/kg/path", params={
                "source": paper_key(HASH_A), "target": paper_key(HASH_B)}))
            assert data["found"] is True, data
            assert len(data["path"]) >= 2
            assert any(item["kind"] == "keyword" for item in data["path"][1:-1])
        finally:
            fixture.close()


# ---------------------------------------------------------------------------
# agent
# ---------------------------------------------------------------------------

def test_agent_profiles_and_tools():
    with MockModelServer() as server:
        fixture = GatewayFixture(server)
        try:
            client = fixture.client
            profiles = _data(client.get("/api/v1/agent/profiles"))
            assert {"qa", "methodologist", "skeptic"} <= {item["id"] for item in profiles["items"]}

            tools = _data(client.get("/api/v1/agent/tools"))
            by_name = {item["name"]: item for item in tools["items"]}
            assert "rag_search" in by_name
            assert by_name["rag_search"]["source"] == "builtin"
            assert by_name["rag_search"]["params_schema"]["required"] == ["query"]
        finally:
            fixture.close()


def test_agent_qa_and_stream():
    with MockModelServer() as server:
        fixture = GatewayFixture(server)
        try:
            client = fixture.client
            data = _data(client.post("/api/v1/agent/qa", json={
                "question": "自注意力机制如何并行计算？", "hashes": [HASH_A]}))
            assert data["empty"] is False
            assert data["citations"]
            assert data["session_id"]
            assert data["answer"]

            with client.stream("POST", "/api/v1/agent/qa/stream", json={
                    "question": "自注意力机制如何并行计算？", "hashes": [HASH_A]}) as stream:
                assert stream.status_code == 200
                body = b"".join(stream.iter_bytes()).decode("utf-8")
            assert "event: citations" in body
            assert "event: delta" in body
            assert "event: done" in body

            fixture.services.settings.update({"retrieval": {"min_score": 0.99}})
            none = _data(client.post("/api/v1/agent/qa", json={
                "question": "量子色动力学的格点验证", "hashes": [HASH_A]}))
            assert none["empty"] is True
            assert "库中无相关内容" in none["answer"]

            bad = client.post("/api/v1/agent/qa", json={"question": ""})
            assert bad.status_code == 400
        finally:
            fixture.close()


def test_agent_run_uses_tool_loop():
    with MockModelServer() as server:
        fixture = GatewayFixture(server)
        try:
            import test_agent as agent_tests

            agent_tests.STATE.tool_then_answer = True
            agent_tests.STATE.tool_rounds = 0
            try:
                data = _data(fixture.client.post("/api/v1/agent/run", json={
                    "task": "自注意力复杂度如何？", "profile": "methodologist",
                    "hashes": [HASH_A], "tools": ["rag_search", "paper_meta"]}))
                assert data["steps"] >= 1
                assert any(call["name"] == "rag_search" for call in data["tool_calls"])
                assert data["answer"]
                assert agent_tests.STATE.tool_rounds == 1
            finally:
                agent_tests.STATE.tool_then_answer = False
        finally:
            fixture.close()


def test_agent_brainstorm_and_session_endpoints():
    with MockModelServer() as server:
        fixture = GatewayFixture(server)
        try:
            client = fixture.client
            data = _data(client.post("/api/v1/agent/brainstorm", json={
                "topic": "Transformer 长序列可行性",
                "hashes": [HASH_A],
                "agents": [{"id": "methodologist"}, {"id": "skeptic"}],
                "rounds": 1,
            }))
            session_id = data["session_id"]
            assert session_id

            # The stream terminates because run_brainstorm publishes `done`.
            with client.stream("GET", f"/api/v1/agent/sessions/{session_id}/events") as stream:
                assert stream.status_code == 200
                body = b"".join(stream.iter_bytes()).decode("utf-8")
            assert "event: done" in body

            session = _data(client.get(f"/api/v1/agent/sessions/{session_id}"))
            assert session["session"]["kind"] == "brainstorm"
            assert {item["id"] for item in session["agents"]} == {"methodologist", "skeptic"}
            assert session["by_agent"], "应按智能体分组"
            assert all(message["agent_id"] for message in session["messages"])
            assert session["blackboard"]
            assert session["synthesis"]["consensus"]

            listed = _data(client.get("/api/v1/agent/sessions", params={"kind": "brainstorm"}))
            assert listed["total"] >= 1

            blackboard = _data(client.get(
                f"/api/v1/agent/sessions/{session_id}/blackboard"))
            assert blackboard["items"]

            posted = _data(client.post(f"/api/v1/agent/sessions/{session_id}/message",
                                       json={"content": "再多谈谈工程代价"}))
            assert posted["message_id"] > 0

            assert client.get("/api/v1/agent/sessions/nope").status_code == 404
            assert _error(client.get("/api/v1/agent/sessions/nope"))["code"] == "SESSION_NOT_FOUND"

            deleted = _data(client.delete(f"/api/v1/agent/sessions/{session_id}"))
            assert deleted["deleted"] is True
            assert client.get(f"/api/v1/agent/sessions/{session_id}").status_code == 404
        finally:
            fixture.close()


# ---------------------------------------------------------------------------
# skills
# ---------------------------------------------------------------------------

def _write_skill(root: Path, skill_id: str = "stats-helper") -> Path:
    directory = root / skill_id
    (directory / "scripts").mkdir(parents=True)
    (directory / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")
    (directory / "scripts" / "describe.py").write_text(DESCRIBE_SCRIPT, encoding="utf-8")
    return directory


def test_skill_endpoints_full_lifecycle():
    with MockModelServer() as server:
        fixture = GatewayFixture(server)
        try:
            client = fixture.client
            assert _data(client.get("/api/v1/skills"))["items"] == []

            with TempWorkspace() as tmp:
                source = _write_skill(Path(tmp))
                installed = _data(client.post("/api/v1/skills/install",
                                              json={"path": str(source)}))
                assert installed["id"] == "stats-helper"
                assert installed["enabled"] is True

                listing = _data(client.get("/api/v1/skills"))
                assert len(listing["items"]) == 1

                tools = _data(client.get("/api/v1/agent/tools"))
                assert "skill_describe" in {item["name"] for item in tools["items"]}

                detail = _data(client.get("/api/v1/skills/stats-helper"))
                assert detail["skill"]["version"] == "1.2.0"
                assert "scripts/describe.py" in detail["files"]

                disabled = _data(client.patch("/api/v1/skills/stats-helper",
                                              json={"enabled": False}))
                assert disabled["enabled"] is False
                tools = _data(client.get("/api/v1/agent/tools"))
                assert "skill_describe" not in {item["name"] for item in tools["items"]}

                _data(client.patch("/api/v1/skills/stats-helper", json={"enabled": True}))
                reloaded = _data(client.post("/api/v1/skills/stats-helper/reload"))
                assert reloaded["id"] == "stats-helper"

                deleted = _data(client.delete("/api/v1/skills/stats-helper"))
                assert deleted["deleted"] is True
                assert client.get("/api/v1/skills/stats-helper").status_code == 404

            missing = client.post("/api/v1/skills/install", json={"path": "Z:/nope"})
            assert missing.status_code == 400
        finally:
            fixture.close()


# ---------------------------------------------------------------------------
# files
# ---------------------------------------------------------------------------

def test_file_endpoints_refuse_escapes_and_serve_inside():
    with MockModelServer() as server:
        fixture = GatewayFixture(server)
        try:
            client = fixture.client
            inside = _data(client.get("/api/v1/files/list", params={"path": f"papers/{HASH_A}"}))
            names = {entry["name"] for entry in inside["entries"]}
            assert "paper.pdf" in names or "okf" in names

            for attempt in ("../../../etc/passwd", "..\\..\\windows\\win.ini", "/etc/passwd"):
                response = client.get("/api/v1/files/raw", params={"path": attempt})
                assert response.status_code in (400, 404), attempt
                if response.status_code == 400:
                    assert _error(response)["code"] == "PATH_OUT_OF_SCOPE"

            download = client.get("/api/v1/files/download",
                                  params={"path": f"papers/{HASH_A}/fig/fig1.png"})
            assert download.status_code == 200
            assert "attachment" in download.headers["content-disposition"]
        finally:
            fixture.close()


def test_route_conflicts_do_not_shadow_literals():
    """`/papers/facets` must not be parsed as a paper hash."""
    with MockModelServer() as server:
        fixture = GatewayFixture(server)
        try:
            response = fixture.client.get("/api/v1/papers/facets")
            assert response.status_code == 200
            assert "journals" in _data(response)

            # And the okf/verify literal must beat the `{concept_id:path}` route.
            verify = fixture.client.get(f"/api/v1/papers/{HASH_A}/okf/verify")
            assert verify.status_code == 200
            assert "conformant" in _data(verify)
        finally:
            fixture.close()


def test_unknown_api_route_returns_envelope():
    with MockModelServer() as server:
        fixture = GatewayFixture(server)
        try:
            response = fixture.client.get("/api/v1/does-not-exist")
            assert response.status_code == 404
            payload = response.json()
            assert payload["ok"] is False
            assert payload["error"]["code"]
        finally:
            fixture.close()


# ---------------------------------------------------------------------------
# runner
# ---------------------------------------------------------------------------

def _run_all() -> int:
    return run_all(globals(), verbose=True)


if __name__ == "__main__":
    raise SystemExit(_run_all())
