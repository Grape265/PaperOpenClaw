"""Agent-layer tests: tools, isolation, skills, QA and brainstorm.

Uses a local mock provider that dispatches on the *system prompt*, so the same
server answers as a parser, an analyst, a moderator or a streaming QA model.
That keeps the tests focused on orchestration rather than on prompt wording.

    python tests/test_agent.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _harness import TempWorkspace, run_all  # noqa: E402

# The mock provider answers every prompt family the app uses: agent reasoning
# here, and the parse/digest prompts reused from the ingest fixture so a gateway
# test can drive a real upload end to end.
from test_ingest import PARSE_PAYLOAD  # noqa: E402

from paperopenclaw.agent import profiles as profile_module  # noqa: E402
from paperopenclaw.agent.skill_loader import SkillLoader, parse_manifest  # noqa: E402
from paperopenclaw.agent.tools import Tool, ToolContext, ToolRegistry, build_default_registry  # noqa: E402
from paperopenclaw.config import Settings  # noqa: E402
from paperopenclaw.core.paths import DataPaths  # noqa: E402
from paperopenclaw.services import create_services  # noqa: E402
from paperopenclaw.storage.db import Database  # noqa: E402
from paperopenclaw.storage.repos.rag import unit_id_a, unit_id_b, unit_id_c  # noqa: E402

HASH_A = "a" * 64
HASH_B = "b" * 64


# ---------------------------------------------------------------------------
# mock provider
# ---------------------------------------------------------------------------

class MockState:
    def __init__(self) -> None:
        self.requests: List[Dict[str, Any]] = []
        self.tool_then_answer = False
        self.tool_rounds = 0


STATE = MockState()


def _system_text(payload: Dict[str, Any]) -> str:
    return " ".join(
        message.get("content") if isinstance(message.get("content"), str) else ""
        for message in payload.get("messages") or []
        if message.get("role") == "system"
    )


def _has_tool_role(payload: Dict[str, Any]) -> bool:
    return any(message.get("role") == "tool" for message in payload.get("messages") or [])


def _last_user_text(payload: Dict[str, Any]) -> str:
    texts = []
    for message in payload.get("messages") or []:
        if message.get("role") == "user":
            content = message.get("content")
            if isinstance(content, str):
                texts.append(content)
            elif isinstance(content, list):
                texts.extend(part.get("text", "") for part in content
                             if isinstance(part, dict) and part.get("type") == "text")
    return texts[-1] if texts else ""


DECOMPOSE = {
    "sub_questions": [
        {"role": "methodologist", "question": "自注意力的计算复杂度如何？", "focus": "机制"},
        {"role": "experimentalist", "question": "BLEU 提升是否可信？", "focus": "证据"},
        {"role": "skeptic", "question": "结论是否被过度声称？", "focus": "质疑"},
        {"role": "connector", "question": "能否迁移到视觉任务？", "focus": "迁移"},
    ],
    "shared_context": "围绕 Transformer 论文的方法与证据展开讨论。",
}

OPINION = {
    "claim": "自注意力的复杂度随序列长度平方增长。",
    "reasoning": "每一对位置都要计算注意力权重，因此复杂度为 O(n^2)。",
    "evidence": [{"point": "模型由六层自注意力堆叠", "source": "[1]"}],
    "confidence": 0.75,
    "caveats": ["未考虑稀疏注意力变体"],
    "questions": ["在长序列上是否有近似方法？"],
}

SYNTHESIS = {
    "synthesis": "讨论围绕机制与证据展开，共识是自注意力有效，分歧在于长序列代价。",
    "consensus": [{"point": "自注意力可并行", "supporters": ["methodologist", "engineer"]}],
    "disagreements": [{"topic": "长序列可行性",
                       "positions": [{"role": "skeptic", "stance": "代价过高"}],
                       "assessment": "怀疑方证据更充分。"}],
    "evidence_summary": [{"point": "28.4 BLEU", "sources": [HASH_A[:12]]}],
    "open_questions": ["稀疏注意力能否保持质量？"],
    "recommendations": ["在长文本任务上先做复杂度评估。"],
}


class MockHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args: Any) -> None:
        return

    def _send_json(self, status: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            payload = {}
        STATE.requests.append({"path": self.path, "payload": payload})

        system = _system_text(payload)
        tools = payload.get("tools") or []

        if payload.get("stream"):
            self._send_stream("基于库中证据，自注意力的复杂度随序列长度平方增长 [1]。")
            return

        # Tool-using loop: first call asks for a tool, later calls answer.
        if tools and STATE.tool_then_answer and not _has_tool_role(payload):
            STATE.tool_rounds += 1
            self._send_json(200, {
                "id": "chatcmpl-tool",
                "model": payload.get("model"),
                "choices": [{
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [{
                            "id": "call_1", "type": "function",
                            "function": {"name": "rag_search",
                                         "arguments": json.dumps({"query": "自注意力 复杂度"})},
                        }],
                    },
                    "finish_reason": "tool_calls",
                }],
                "usage": {"prompt_tokens": 40, "completion_tokens": 10, "total_tokens": 50},
            })
            return

        if "拆解为若干" in system:
            content = json.dumps(DECOMPOSE, ensure_ascii=False)
        elif "黑板摘要" in system:
            content = "本轮讨论集中在自注意力的机制与长序列代价，方法方强调并行性，怀疑方强调 O(n^2)。"
        elif "负责做最终汇总" in system:
            content = json.dumps(SYNTHESIS, ensure_ascii=False)
        elif "看不到其他角色" in system:
            # 自然对话输出：对证据的提炼总结 + 行内 [n] + 结尾参考文献列表。
            content = (
                "自注意力通过并行计算任意位置间的依赖，摆脱了循环结构的串行瓶颈 [1]。"
                "这是 Transformer 相比 RNN 最本质的改进，我的推测是长序列下的复杂度仍是主要代价。\n\n"
                "参考文献：\n"
                "[1] Ashish Vaswani，Attention Is All You Need，"
                + HASH_A
            )
        elif "结构化抽取引擎" in system:
            content = json.dumps(PARSE_PAYLOAD, ensure_ascii=False)
        elif "学术编辑" in system:
            content = ("该章节的大意：本文给出了具体方法与实验证据，关键数值为 28.4 BLEU，"
                       "模型由六层自注意力堆叠而成。")
        elif "学术综述作者" in system:
            dimension = ""
            for line in _last_user_text(payload).splitlines():
                if line.startswith("目标维度："):
                    dimension = line.split("：", 1)[1].strip()
            content = json.dumps({
                "summary": f"{dimension}维度的要点概括：方法完全基于自注意力，实验达到 28.4 BLEU。",
                "points": [
                    {"text": f"{dimension}：Transformer 由六层相同的自注意力层堆叠而成。",
                     "assets": ["fig1"]},
                    {"text": f"{dimension}：在 WMT 2014 英德翻译上达到 28.4 BLEU。",
                     "assets": ["tab1"]},
                ],
                "importance": 0.8,
            }, ensure_ascii=False)
        elif "七维鱼骨" in system or "结构分析专家" in system:
            content = json.dumps({
                "sections": [
                    {"section": "background", "points": [
                        {"text": "本文研究低频环流变率模态，用于区域与全球环流诊断。", "assets": []}]},
                    {"section": "method", "points": [
                        {"text": "采用旋转主成分分析提取遥相关模态。", "assets": ["fig1"]}]},
                    {"section": "experiment", "points": [
                        {"text": "在五种再分析资料上对比验证，达到 28.4 BLEU。", "assets": ["tab1"]}]},
                ],
            }, ensure_ascii=False)
        elif "学术术语识别专家" in system or "研究专业关键词" in system:
            content = json.dumps({"keywords": ["self-attention", "transformer", "WMT 2014"]},
                                 ensure_ascii=False)
        elif "论文知识库问答助手" in system:
            content = "根据检索到的证据，自注意力机制可以并行计算 [1]。"
        else:
            content = "最终回答：基于库中证据，自注意力可以并行计算 [1]。"

        self._send_json(200, {
            "id": "chatcmpl-mock",
            "model": payload.get("model"),
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 60, "completion_tokens": 30, "total_tokens": 90},
        })

    def _send_stream(self, text: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        for piece in [text[i:i + 8] for i in range(0, len(text), 8)]:
            chunk = {"choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}]}
            self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8"))
            self.wfile.flush()
        final = {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                 "usage": {"prompt_tokens": 60, "completion_tokens": 20, "total_tokens": 80}}
        self.wfile.write(f"data: {json.dumps(final)}\n\n".encode("utf-8"))
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


class MockModelServer:
    def __init__(self) -> None:
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), MockHandler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}/v1"

    def __enter__(self) -> "MockModelServer":
        self.thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


# ---------------------------------------------------------------------------
# fixture: a seeded two-paper pyramid without running the PDF pipeline
# ---------------------------------------------------------------------------

class AgentFixture:
    def __init__(self, server: MockModelServer) -> None:
        self._tmp = TempWorkspace()
        settings = Settings(
            providers=[{"id": "mock", "name": "Mock", "base_url": server.base_url,
                        "api_key": "sk-test", "models": ["mock-large"], "vision": True}],
            routing={"parse": {"provider": "mock", "model": "mock-large"},
                     "chat": {"provider": "mock", "model": "mock-large"},
                     "digest": {"provider": "mock", "model": "mock-large"}},
            embedding={"provider": "lexical", "dimension": 128, "model": "hashed-ngram"},
            retrieval={"min_score": 0.0, "stop_at_b": False, "top_k_c": 3, "top_k_b": 5, "top_k_a": 5},
            agent={"max_steps": 4, "brainstorm_rounds": 1},
        )
        self.services = create_services(str(self._tmp.path), settings=settings)
        self._seed()

    def _seed(self) -> None:
        services = self.services
        papers = [
            (HASH_A, "Attention Is All You Need", "NeurIPS", 2017,
             "We propose the Transformer, a model architecture relying entirely on self-attention."),
            (HASH_B, "Convolutional Image Classification", "CVPR", 2016,
             "We study convolutional neural networks for image classification."),
        ]
        for hash_value, title, journal, year, abstract in papers:
            services.papers.create(hash_value, title=title, status="ready")
            services.papers.update(hash_value, journal=journal, year=year, abstract=abstract,
                                   published_at=f"{year}-06-01", language="en")
        services.papers.replace_authors_and_affiliations(
            HASH_A,
            [{"name": "Ashish Vaswani", "affiliations": ["Google Brain"], "corresponding": True}],
            [{"name": "Google Brain", "department": "Research", "country": "USA"}],
        )

        services.rag.replace_units(HASH_A, [
            {"id": unit_id_c(HASH_A), "layer": "C", "title": "Attention Is All You Need",
             "text": "We propose the Transformer, a model architecture relying entirely on "
                     "self-attention to draw global dependencies."},
            {"id": unit_id_b(HASH_A, "method"), "layer": "B", "section": "method", "ordinal": 3,
             "title": "方法", "text": "Transformer 由六层自注意力堆叠而成。",
             "meta": {"points": [{"text": "模型由六层相同的自注意力层堆叠而成。", "assets": ["fig1"]}],
                      "summary": "方法要点", "importance": 0.8}},
            {"id": unit_id_b(HASH_A, "experiment"), "layer": "B", "section": "experiment",
             "ordinal": 4, "title": "实验", "text": "在 WMT 2014 英德翻译上达到 28.4 BLEU。",
             "meta": {"points": [{"text": "WMT 2014 英德翻译达到 28.4 BLEU。", "assets": ["tab1"]}],
                      "summary": "实验要点", "importance": 0.7}},
            {"id": unit_id_a(HASH_A, 0), "layer": "A", "section": "method", "ordinal": 0,
             "title": "Model Architecture",
             "text": "The encoder stacks six identical layers, each with multi-head self-attention."},
            {"id": unit_id_a(HASH_A, 1), "layer": "A", "section": "experiment", "ordinal": 1,
             "title": "Results", "text": "The big model reaches 28.4 BLEU on WMT 2014 En-De."},
        ])
        services.rag.replace_units(HASH_B, [
            {"id": unit_id_c(HASH_B), "layer": "C", "title": "Convolutional Image Classification",
             "text": "We study convolutional neural networks for image classification."},
            {"id": unit_id_b(HASH_B, "method"), "layer": "B", "section": "method", "ordinal": 3,
             "title": "方法", "text": "卷积网络用于图像分类。",
             "meta": {"points": [{"text": "卷积层提取局部特征。", "assets": []}],
                      "summary": "方法要点", "importance": 0.6}},
            {"id": unit_id_a(HASH_B, 0), "layer": "A", "section": "method", "ordinal": 0,
             "title": "Network", "text": "The convolutional network has five layers."},
        ])
        services.rag.add_links([
            (unit_id_a(HASH_A, 0), unit_id_b(HASH_A, "method"), "a_to_b", 1.0, None),
            (unit_id_a(HASH_A, 1), unit_id_b(HASH_A, "experiment"), "a_to_b", 1.0, None),
            (unit_id_b(HASH_A, "method"), unit_id_c(HASH_A), "b_to_c", 1.0, None),
            (unit_id_b(HASH_A, "experiment"), unit_id_c(HASH_A), "b_to_c", 1.0, None),
            (unit_id_a(HASH_B, 0), unit_id_b(HASH_B, "method"), "a_to_b", 1.0, None),
            (unit_id_b(HASH_B, "method"), unit_id_c(HASH_B), "b_to_c", 1.0, None),
        ])

        for layer in ("A", "B", "C"):
            units = services.rag.units_of_layer(layer)
            services.vectors.upsert(
                layer,
                [unit["id"] for unit in units],
                services.embedder.encode([unit["text"] for unit in units]),
                [unit["text"] for unit in units],
                [{"paper_hash": unit["paper_hash"], "section": unit.get("section") or "",
                  "title": unit.get("title") or ""} for unit in units],
            )

        # Assets so asset_list / okf_read have something real to read.
        for hash_value in (HASH_A, HASH_B):
            services.paths.ensure_paper_dirs(hash_value)
        (services.paths.paper_fig_dir(HASH_A) / "fig1.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        (services.paths.paper_tab_dir(HASH_A) / "tab1.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        services.papers.replace_assets(HASH_A, [
            {"kind": "figure", "label": "fig1", "ordinal": 1, "page": 2, "section": "method",
             "caption": "Figure 1: The Transformer architecture.",
             "file_path": f"papers/{HASH_A}/fig/fig1.png", "width": 600, "height": 400},
            {"kind": "table", "label": "tab1", "ordinal": 1, "page": 3, "section": "experiment",
             "caption": "Table 1: BLEU scores.",
             "file_path": f"papers/{HASH_A}/tab/tab1.png", "width": 500, "height": 300},
        ])

        from paperopenclaw.ingest.fishbone_builder import FishboneBuilder
        from paperopenclaw.ingest.kg_builder import KnowledgeGraphBuilder

        FishboneBuilder(services.papers, services.fishbone, services.rag).build(
            HASH_A, title="Attention Is All You Need",
            assets=services.papers.list_assets(HASH_A),
            b_units=services.rag.units_of_paper(HASH_A, "B"),
        )
        KnowledgeGraphBuilder(services.papers, services.kg, services.rag).build(
            HASH_A, title="Attention Is All You Need",
            a_units=services.rag.units_of_paper(HASH_A, "A"),
        )

        # A minimal OKF bundle so okf_read is exercisable.
        from paperopenclaw.ingest.okf_writer import write_bundle

        okf = write_bundle(
            services.paths, HASH_A,
            paper={"title": "Attention Is All You Need",
                   "abstract": "We propose the Transformer.",
                   "authors": [{"name": "Ashish Vaswani", "affiliations": [{"name": "Google Brain"}]}],
                   "affiliations": [{"name": "Google Brain"}],
                   "keywords": ["attention"], "language": "en", "journal": "NeurIPS",
                   "year": 2017, "published_at": "2017-06-01", "venue_type": "conference"},
            sections=[{"title": "Model Architecture", "text": "Six layers of self-attention.",
                       "section": "method", "slug": "model-architecture"}],
            assets=services.papers.list_assets(HASH_A),
        )
        # The ingest pipeline normally indexes the concepts it writes; mirror it.
        services.papers.replace_okf_concepts(HASH_A, okf.concepts)

    @property
    def paths(self) -> DataPaths:
        return self.services.paths

    def close(self) -> None:
        self.services.close()
        self._tmp.cleanup()


# ---------------------------------------------------------------------------
# tool registry
# ---------------------------------------------------------------------------

def test_registry_lists_and_enforces_allowlist():
    services_context = None
    registry = build_default_registry()
    names = set(registry.names())
    assert {"rag_search", "paper_meta", "kg_query", "okf_read", "asset_list", "py_eval"} <= names

    context = ToolContext(services=None, agent_id="a1", allowed_tools={"rag_search"})
    assert registry.is_allowed("rag_search", context.allowed_tools)
    assert not registry.is_allowed("py_eval", context.allowed_tools)

    outcome = registry.invoke("py_eval", context, {"expression": "1+1"})
    assert outcome.ok is False
    assert "无权调用" in outcome.error

    unknown = registry.invoke("nope", context, {})
    assert unknown.ok is False and "未知工具" in unknown.error

    # Specs handed to the model are filtered too, so it never sees what it
    # cannot call.
    specs = registry.specs({"rag_search"})
    assert [spec.name for spec in specs] == ["rag_search"]


def test_tool_results_never_raise():
    registry = ToolRegistry()

    def explode(context, arguments):
        raise RuntimeError("boom")

    registry.register(Tool("bad", "总是失败", {"type": "object", "properties": {}}, explode))
    outcome = registry.invoke("bad", ToolContext(services=None), {})
    assert outcome.ok is False
    assert "boom" in outcome.error
    assert json.loads(outcome.as_text())["error"]


def test_scope_intersects_requested_hashes():
    context = ToolContext(services=None, allowed_hashes=[HASH_A])
    assert context.scope(None) == [HASH_A]
    assert context.scope([HASH_A, HASH_B]) == [HASH_A]
    assert context.scope([HASH_B]) == []

    unrestricted = ToolContext(services=None)
    assert unrestricted.scope(None) is None
    assert unrestricted.scope([HASH_B]) == [HASH_B]


# ---------------------------------------------------------------------------
# built-in tools against the seeded fixture
# ---------------------------------------------------------------------------

def test_builtin_tools_are_scoped_and_productive():
    with MockModelServer() as server:
        fixture = AgentFixture(server)
        try:
            services = fixture.services
            registry = build_default_registry()
            context = ToolContext(services=services, agent_id="a1",
                                  allowed_hashes=[HASH_A],
                                  allowed_tools={"rag_search", "paper_meta", "kg_query",
                                                 "okf_read", "asset_list", "py_eval"})

            rag = registry.invoke("rag_search", context, {"query": "self-attention encoder layers"})
            assert rag.ok
            assert rag.payload["empty"] is False
            assert rag.payload["evidence"], "应检索到证据"
            assert all(item["hash"] == HASH_A for item in rag.payload["evidence"]), \
                "工具必须把范围限制在授权论文内"

            meta = registry.invoke("paper_meta", context, {})
            assert meta.ok
            assert meta.payload["count"] == 1, "只应看到授权的 1 篇论文"
            assert meta.payload["papers"][0]["hash"] == HASH_A
            assert meta.payload["papers"][0]["authors"][0]["name"] == "Ashish Vaswani"
            assert meta.payload["papers"][0]["authors"][0]["affiliations"] == ["Google Brain"]

            fish = registry.invoke("kg_query", context, {"mode": "fishbone", "hash": HASH_A})
            assert fish.ok
            assert [item["slug"] for item in fish.payload["spine"]][:3] == [
                "background", "data", "method"]
            populated = [item for item in fish.payload["spine"] if item["nodes"]]
            assert populated
            assert any(node["assets"] for item in populated for node in item["nodes"])

            search = registry.invoke("kg_query", context, {"mode": "search", "query": "encoder"})
            assert search.ok and search.payload["count"] >= 1

            concepts = registry.invoke("okf_read", context, {"hash": HASH_A})
            assert concepts.ok
            ids = {item["concept_id"] for item in concepts.payload["concepts"]}
            assert "paper" in ids and "figures/fig1" in ids

            one = registry.invoke("okf_read", context,
                                  {"hash": HASH_A, "concept_id": "paper"})
            assert one.ok
            assert one.payload["type"] == "Research Paper"
            assert one.payload["frontmatter"]["title"] == "Attention Is All You Need"
            assert one.payload["frontmatter"]["journal"] == "NeurIPS"

            assets = registry.invoke("asset_list", context, {"hash": HASH_A})
            assert assets.ok and assets.payload["count"] == 2
            labels = {item["label"] for item in assets.payload["assets"]}
            assert labels == {"fig1", "tab1"}
            assert all(item["url"].startswith("/api/v1/papers/") for item in assets.payload["assets"])

            # Out-of-scope access is refused rather than silently allowed.
            denied = registry.invoke("okf_read", context, {"hash": HASH_B, "concept_id": "paper"})
            assert denied.ok
            assert "无权访问" in denied.payload.get("error", "")

            # Calculation tool.
            calc = registry.invoke("py_eval", context, {"expression": "(28.4 - 25.8) / 25.8 * 100"})
            assert calc.ok
            assert abs(calc.payload["result"] - 10.0775) < 0.01
        finally:
            fixture.close()


def test_py_eval_refuses_unsafe_expressions():
    registry = build_default_registry()
    context = ToolContext(services=None, allowed_tools={"py_eval"})
    for expression in ("__import__('os').system('echo hi')",
                       "open('x')",
                       "a + 1",
                       "[1,2][0]",
                       "1 if True else 2"):
        outcome = registry.invoke("py_eval", context, {"expression": expression})
        assert outcome.ok
        assert "error" in outcome.payload, f"应拒绝：{expression}"


# ---------------------------------------------------------------------------
# skills
# ---------------------------------------------------------------------------

SKILL_MD = """---
name: 统计助手
description: 提供描述性统计与图表信息查询
version: 1.2.0
author: tester
tools:
  - name: skill_describe
    description: 返回传入数值的描述性统计
    type: script
    script: scripts/describe.py
    parameters:
      type: object
      properties:
        values:
          type: array
          items: {type: number}
      required: [values]
---

# 使用说明

当需要计算论文中一组数值的均值与极差时，调用 skill_describe。
结果必须与原文数值核对后再引用。
"""

DESCRIBE_SCRIPT = """import json, sys

with open(sys.argv[1], encoding="utf-8") as handle:
    payload = json.load(handle)
values = payload["arguments"].get("values") or []
result = {"count": len(values)}
if values:
    result.update({"mean": sum(values) / len(values),
                   "min": min(values), "max": max(values)})
with open(sys.argv[2], "w", encoding="utf-8") as handle:
    json.dump(result, handle, ensure_ascii=False)
"""


def _write_skill(root: Path, skill_id: str = "stats-helper") -> Path:
    directory = root / skill_id
    (directory / "scripts").mkdir(parents=True)
    (directory / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")
    (directory / "scripts" / "describe.py").write_text(DESCRIBE_SCRIPT, encoding="utf-8")
    return directory


def test_parse_manifest_reads_frontmatter_and_tools():
    with TempWorkspace() as tmp:
        directory = _write_skill(Path(tmp))
        manifest = parse_manifest(directory / "SKILL.md", "stats-helper")
        assert manifest.name == "统计助手"
        assert manifest.version == "1.2.0"
        assert manifest.tool_names == ["skill_describe"]
        assert "skill_describe" in manifest.instructions
        assert "当需要计算" in manifest.instructions
        assert manifest.warnings == []


def test_parse_manifest_tolerates_missing_frontmatter():
    with TempWorkspace() as tmp:
        path = Path(tmp) / "SKILL.md"
        path.write_text("这是一个没有 frontmatter 的技能说明。\n第二行。", encoding="utf-8")
        manifest = parse_manifest(path, "bare")
        assert manifest.name == "bare"
        assert "没有 frontmatter" in manifest.description
        assert manifest.tools == []


def test_skill_scan_install_enable_and_tools():
    with MockModelServer() as server:
        fixture = AgentFixture(server)
        try:
            services = fixture.services
            loader: SkillLoader = services.skill_loader

            with TempWorkspace() as tmp:
                source = _write_skill(Path(tmp))
                installed = loader.install_from_directory(source)
                assert installed["id"] == "stats-helper"
                assert installed["enabled"] is True
                assert installed["tools"] == ["skill_describe"]

                # Re-installing without overwrite is refused.
                try:
                    loader.install_from_directory(source)
                except Exception as exc:
                    assert "已存在" in str(exc)
                else:
                    raise AssertionError("重复安装应被拒绝")

                assert loader.install_from_directory(source, overwrite=True)["id"] == "stats-helper"

                registry = build_default_registry()
                assert loader.apply_tools(registry) == 1
                tool = registry.get("skill_describe")
                assert tool is not None and tool.source == "skill:stats-helper"

                # The declared tool actually runs, exchanging JSON via files.
                context = ToolContext(services=services, agent_id="a1",
                                      allowed_tools={"skill_describe"},
                                      workspace=str(services.paths.agent_dir("s", "a1")))
                outcome = registry.invoke("skill_describe", context, {"values": [24.0, 28.4, 25.8]})
                assert outcome.ok, outcome.error
                assert outcome.payload["count"] == 3
                assert abs(outcome.payload["mean"] - 26.0667) < 0.01
                assert outcome.payload["max"] == 28.4

                # Disabling removes the tool from the registry.
                loader.set_enabled("stats-helper", False)
                assert loader.apply_tools(registry) == 0
                assert registry.get("skill_describe") is None
                assert loader.enabled_instructions() == ""

                loader.set_enabled("stats-helper", True)
                loader.apply_tools(registry)
                assert "统计助手" in loader.enabled_instructions()

                # Detail view exposes instructions, declarations and files.
                detail = loader.detail("stats-helper")
                assert detail["skill"]["version"] == "1.2.0"
                assert "scripts/describe.py" in detail["files"]
                assert detail["tool_declarations"][0]["name"] == "skill_describe"

                # Uninstall removes both directory and registry row.
                assert loader.uninstall("stats-helper") is True
                assert not services.paths.skill_dir("stats-helper").exists()
                assert services.skills.get("stats-helper") is None
        finally:
            fixture.close()


def test_skill_zip_install_rejects_path_traversal():
    import zipfile

    with MockModelServer() as server:
        fixture = AgentFixture(server)
        try:
            loader = fixture.services.skill_loader
            with TempWorkspace() as tmp:
                archive = Path(tmp) / "evil.zip"
                with zipfile.ZipFile(archive, "w") as handle:
                    handle.writestr("../../evil/SKILL.md", "---\nname: evil\n---\n")
                try:
                    loader.install_from_zip(archive)
                except Exception as exc:
                    assert "非法路径" in str(exc)
                else:
                    raise AssertionError("路径穿越压缩包应被拒绝")

                good = Path(tmp) / "good.zip"
                source = _write_skill(Path(tmp) / "src")
                with zipfile.ZipFile(good, "w") as handle:
                    for path in source.rglob("*"):
                        if path.is_file():
                            handle.write(path, path.relative_to(source.parent).as_posix())
                installed = loader.install_from_zip(good)
                assert installed["id"] == "stats-helper"
        finally:
            fixture.close()


# ---------------------------------------------------------------------------
# agent loop, QA and brainstorm
# ---------------------------------------------------------------------------

def test_agent_loop_executes_tools_then_answers():
    with MockModelServer() as server:
        fixture = AgentFixture(server)
        try:
            services = fixture.services
            STATE.tool_then_answer = True
            STATE.tool_rounds = 0
            try:
                runtime = services.agents
                context = runtime.make_tool_context(
                    agent_id="qa", allowed_hashes=[HASH_A],
                    allowed_tools=["rag_search", "paper_meta"],
                )
                turn = runtime.run_loop(
                    profile_module.get_profile("qa"),
                    task="自注意力的计算复杂度如何？",
                    tool_context=context,
                )
                assert turn.error == ""
                assert STATE.tool_rounds == 1, "应当调用过一次工具"
                assert any(call["name"] == "rag_search" and call["ok"]
                           for call in turn.tool_calls), turn.tool_calls
                assert turn.text, "应当给出最终回答"
                assert turn.usage.calls >= 2
            finally:
                STATE.tool_then_answer = False
        finally:
            fixture.close()


def test_qa_answers_with_citations():
    with MockModelServer() as server:
        fixture = AgentFixture(server)
        try:
            services = fixture.services
            result = services.agents.answer(
                question="自注意力机制如何并行计算？", hashes=[HASH_A], session_id=None)
            assert result["empty"] is False
            assert result["citations"], "应带引用"
            assert result["citations"][0]["hash"] == HASH_A
            assert result["citations"][0]["layer"] in ("B", "A", "C")
            assert result["layers_used"][0] == "C"
            assert "自注意力" in result["answer"]

            session = services.sessions.get(result["session_id"])
            assert session["kind"] == "qa"
            messages = services.sessions.list_messages(result["session_id"])
            assert [item["role"] for item in messages] == ["user", "assistant"]
            assert messages[1]["citations"], "助手消息应保留引用"
        finally:
            fixture.close()


def test_qa_reports_no_content_instead_of_guessing():
    with MockModelServer() as server:
        fixture = AgentFixture(server)
        try:
            services = fixture.services
            # A strict apex threshold is what makes the library admit it has
            # nothing; with the fixture's permissive default every abstract
            # scores as a weak match.
            services.settings.update({"retrieval": {"min_score": 0.99}})
            mark = len(STATE.requests)
            result = services.agents.answer(
                question="量子色动力学中的渐进自由如何用格点方法验证？",
                hashes=[HASH_A], session_id=None)
            assert result["empty"] is True
            assert result["citations"] == []
            assert "库中无相关内容" in result["answer"]
            # The decisive property: the model was never asked to answer.
            assert len(STATE.requests) == mark, "空检索不应调用大模型"
        finally:
            fixture.close()


def test_qa_streaming_emits_citations_then_deltas():
    with MockModelServer() as server:
        fixture = AgentFixture(server)
        try:
            services = fixture.services

            async def run() -> Dict[str, Any]:
                events = []
                async for event in services.agents.stream_answer(
                    question="自注意力机制如何并行计算？", hashes=[HASH_A]):
                    events.append(event)
                return {"events": events}

            outcome = asyncio.run(run())
            events = outcome["events"]
            kinds = [event["type"] for event in events]
            assert kinds[0] == "status"
            assert "citations" in kinds
            citations_event = next(event for event in events if event["type"] == "citations")
            assert citations_event["citations"]
            assert "delta" in kinds
            text = "".join(event["text"] for event in events if event["type"] == "delta")
            assert "自注意力" in text
            assert kinds[-1] == "done"
            assert events[-1]["usage"]
        finally:
            fixture.close()


def test_brainstorm_isolates_agents_and_synthesises():
    with MockModelServer() as server:
        fixture = AgentFixture(server)
        try:
            services = fixture.services
            outcome = services.agents.brainstorm(
                topic="Transformer 的自注意力在长序列上的可行性",
                hashes=[HASH_A],
                agents=[{"id": "methodologist"}, {"id": "experimentalist"},
                        {"id": "skeptic"}, {"id": "connector"}],
                rounds=1,
            )
            session_id = outcome["session_id"]
            assert outcome["agents"] == ["methodologist", "experimentalist",
                                         "skeptic", "connector"]

            session = services.sessions.get(session_id)
            assert session["kind"] == "brainstorm"
            assert session["status"] == "done"
            assert session["paper_hashes"] == [HASH_A]

            agents = services.sessions.list_agents(session_id)
            assert len(agents) == 4
            # Each agent owns its own workspace and tool set (FR-10.2).
            workspaces = {row["workspace"] for row in agents}
            assert len(workspaces) == 4
            for row in agents:
                assert row["workspace"].startswith(f"agents/{session_id}/")
                assert services.paths.resolve(row["workspace"]).is_dir()
                assert "rag_search" in row["tools"]

            # Messages are attributable per agent: no cross-contamination.
            for row in agents:
                messages = services.sessions.list_messages(session_id, agent_id=row["id"])
                assert messages, f"{row['id']} 应有发言"
                assert all(item["agent_id"] == row["id"] for item in messages)

            blackboard = services.sessions.list_blackboard(session_id)
            assert blackboard, "应产生黑板摘要"
            assert "怀疑方" in blackboard[-1]["content"] or blackboard[-1]["content"]

            synthesis = services.sessions.list_messages(session_id)
            final = [item for item in synthesis if item["role"] == "synthesis"]
            assert final, "应产生主持人汇总"
            payload = json.loads(final[0]["content"])
            assert payload["consensus"] and payload["disagreements"]
            assert outcome["synthesis"]["synthesis"]

            # The moderator is never registered as an isolated participant.
            assert "moderator" not in {row["id"] for row in agents}
        finally:
            fixture.close()


def test_brainstorm_events_are_published():
    from paperopenclaw.core.events import bus, session_topic

    with MockModelServer() as server:
        fixture = AgentFixture(server)
        try:
            services = fixture.services
            captured: List[Dict[str, Any]] = []

            original = bus.publish

            def spy(topic: str, name: str, data: Optional[Dict[str, Any]] = None):
                captured.append({"topic": topic, "name": name, "data": data or {}})
                return original(topic, name, data)

            bus.publish = spy  # type: ignore[assignment]
            try:
                outcome = services.agents.brainstorm(
                    topic="验证事件发布", hashes=[HASH_A],
                    agents=[{"id": "methodologist"}, {"id": "skeptic"}], rounds=1)
            finally:
                bus.publish = original  # type: ignore[assignment]

            topic = session_topic(outcome["session_id"])
            names = [item["name"] for item in captured if item["topic"] == topic]
            assert "round" in names
            assert "plan" in names
            assert "message" in names
            assert "blackboard" in names
            assert "synthesis" in names
            assert names[-1] == "done"
        finally:
            fixture.close()


def test_profiles_catalogue():
    with MockModelServer() as server:
        fixture = AgentFixture(server)
        try:
            catalogue = fixture.services.agents.profiles()
            ids = {item["id"] for item in catalogue}
            assert {"qa", "moderator", "methodologist", "experimentalist",
                    "skeptic", "connector", "engineer"} <= ids
            for item in catalogue:
                assert item["persona"] and item["tools"]
            assert profile_module.resolve_roster(None)[0].id == "methodologist"
            assert [item.id for item in profile_module.resolve_roster(["skeptic", "unknown"])] == ["skeptic"]
        finally:
            fixture.close()


def test_tool_catalogue_reports_sources():
    with MockModelServer() as server:
        fixture = AgentFixture(server)
        try:
            services = fixture.services
            loader = services.skill_loader
            with TempWorkspace() as tmp:
                loader.install_from_directory(_write_skill(Path(tmp)))
                services.agents.reload_skills()
                catalogue = {item["name"]: item for item in services.agents.tool_catalogue()}
                assert catalogue["rag_search"]["source"] == "builtin"
                assert catalogue["rag_search"]["source_name"] == "内置工具"
                assert catalogue["skill_describe"]["source"] == "skill:stats-helper"
                assert catalogue["skill_describe"]["source_name"] == "统计助手"
        finally:
            fixture.close()


def test_example_skill_is_installable_and_works():
    """The shipped example skill must actually run, not just parse."""
    with MockModelServer() as server:
        fixture = AgentFixture(server)
        try:
            example = Path(__file__).resolve().parents[1] / "examples" / "skills" / "numeric-check"
            assert (example / "SKILL.md").is_file(), "示例技能缺失"

            loader = fixture.services.skill_loader
            installed = loader.install_from_directory(example)
            assert installed["id"] == "numeric-check"
            assert set(installed["tools"]) == {"numeric_check", "metric_delta"}

            registry = build_default_registry()
            assert loader.apply_tools(registry) == 2

            context = ToolContext(
                services=fixture.services,
                agent_id="a1",
                allowed_tools={"numeric_check", "metric_delta"},
                workspace=str(fixture.services.paths.agent_dir("s", "a1")),
            )

            outcome = registry.invoke("numeric_check", context, {
                "values": [25.8, 27.3, 28.4],
                "labels": ["base", "mid", "big"],
                "compare": [25.8, 28.4],
            })
            assert outcome.ok, outcome.error
            spread = outcome.payload["spread"]
            assert spread["count"] == 3
            assert spread["max"] == 28.4 and spread["min"] == 25.8
            assert abs(spread["mean"] - 27.1667) < 0.001
            assert outcome.payload["extremes"]["max_label"] == "big"
            change = outcome.payload["change"]
            assert change["absolute"] == 2.6
            assert abs(change["percent"] - 10.0775) < 0.01
            assert change["direction"] == "提升"

            delta = registry.invoke("metric_delta", context, {"before": 28.4, "after": 25.8})
            assert delta.ok, delta.error
            assert delta.payload["change"]["direction"] == "下降"
            assert delta.payload["change"]["percent"] < 0

            bad = registry.invoke("numeric_check", context, {"values": []})
            assert bad.ok
            assert "error" in bad.payload
        finally:
            fixture.close()


# ---------------------------------------------------------------------------
# runner
# ---------------------------------------------------------------------------

def _run_all() -> int:
    return run_all(globals(), verbose=True)


if __name__ == "__main__":
    raise SystemExit(_run_all())
