"""End-to-end ingest pipeline test.

Builds a synthetic 3-page PDF (title/abstract, an embedded figure with a
"Figure 1" caption, a ruled table with a "Table 1" caption), points the app at a
local mock OpenAI-compatible server, and runs the real seven-stage pipeline.

That exercises the parts most likely to break in combination: layout detection
and cropping, structured parse normalisation, OKF bundle generation and
conformance, pyramid construction with links, and fishbone graph assembly.

    python tests/test_ingest.py
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _harness import TempWorkspace, run_all  # noqa: E402

from paperopenclaw.config import Settings  # noqa: E402
from paperopenclaw.core.errors import DuplicatePaper  # noqa: E402
from paperopenclaw.kg.skeleton import SPINE_SLUGS  # noqa: E402
from paperopenclaw.memory.okf_fs import validate_bundle  # noqa: E402
from paperopenclaw.services import create_services  # noqa: E402
from paperopenclaw.storage.repos.rag import unit_id_b, unit_id_c  # noqa: E402


# ---------------------------------------------------------------------------
# synthetic PDF
# ---------------------------------------------------------------------------

PAGE_W, PAGE_H = 595.0, 842.0


def _figure_png() -> bytes:
    """A small PNG used as the embedded figure."""
    import fitz

    document = fitz.open()
    page = document.new_page(width=240, height=140)
    page.draw_rect(fitz.Rect(10, 10, 230, 130), color=(0.2, 0.3, 0.8), width=2)
    page.draw_line(fitz.Point(20, 110), fitz.Point(220, 110), color=(0, 0, 0), width=1)
    page.draw_line(fitz.Point(20, 110), fitz.Point(20, 30), color=(0, 0, 0), width=1)
    for index in range(5):
        x = 40 + index * 36
        page.draw_rect(fitz.Rect(x, 110 - (index + 1) * 14, x + 22, 110),
                       color=(0.8, 0.3, 0.2), fill=(0.8, 0.3, 0.2))
    data = page.get_pixmap(matrix=fitz.Matrix(3, 3)).tobytes("png")
    document.close()
    return data


def build_sample_pdf() -> bytes:
    """A three-page paper: text, a captioned figure, a captioned table."""
    import fitz

    document = fitz.open()

    # ---- page 1: title, authors, abstract ----
    page = document.new_page(width=PAGE_W, height=PAGE_H)
    page.insert_text((60, 80), "Attention Is All You Need", fontsize=18)
    page.insert_text((60, 110), "Ashish Vaswani, Noam Shazeer", fontsize=11)
    page.insert_text((60, 130), "Google Brain; University of Toronto", fontsize=10)
    page.insert_text((60, 170), "Abstract", fontsize=13)
    page.insert_textbox(
        fitz.Rect(60, 180, PAGE_W - 60, 330),
        "The dominant sequence transduction models are based on complex recurrent or "
        "convolutional neural networks. We propose the Transformer, a model architecture "
        "relying entirely on self-attention to draw global dependencies between input and "
        "output. Experiments on WMT 2014 English-to-German translation reach 28.4 BLEU.",
        fontsize=10,
    )
    page.insert_text((60, 360), "Keywords: attention, transformer, machine translation", fontsize=10)

    # ---- page 2: figure + caption ----
    page = document.new_page(width=PAGE_W, height=PAGE_H)
    page.insert_text((60, 60), "1 Introduction", fontsize=13)
    page.insert_textbox(
        fitz.Rect(60, 70, PAGE_W - 60, 180),
        "Recurrent models process sequences left to right, which precludes parallelisation. "
        "Attention mechanisms allow modelling of dependencies regardless of distance.",
        fontsize=10,
    )
    page.insert_image(fitz.Rect(90, 210, 490, 440), stream=_figure_png())
    page.insert_textbox(
        fitz.Rect(90, 450, 490, 490),
        "Figure 1: The Transformer architecture, showing the encoder and decoder stacks.",
        fontsize=9,
    )

    # ---- page 3: a ruled table + caption ----
    page = document.new_page(width=PAGE_W, height=PAGE_H)
    page.insert_text((60, 60), "3 Results", fontsize=13)
    page.insert_textbox(
        fitz.Rect(60, 120, 520, 150),
        "Table 1: BLEU scores on WMT 2014 English-to-German translation.",
        fontsize=9,
    )
    rows = [["Model", "BLEU", "Params"],
            ["ByteNet", "23.8", "10M"],
            ["ConvS2S", "25.2", "20M"],
            ["Transformer (base)", "27.3", "65M"],
            ["Transformer (big)", "28.4", "213M"]]
    top, row_h, left, col_w = 160.0, 24.0, 90.0, 120.0
    width = col_w * 3
    for row_index in range(len(rows) + 1):
        y = top + row_index * row_h
        page.draw_line(fitz.Point(left, y), fitz.Point(left + width, y), color=(0, 0, 0), width=0.7)
    for col_index in range(4):
        x = left + col_index * col_w
        page.draw_line(fitz.Point(x, top), fitz.Point(x, top + row_h * len(rows)),
                       color=(0, 0, 0), width=0.7)
    for row_index, row in enumerate(rows):
        for col_index, cell in enumerate(row):
            page.insert_text((left + col_index * col_w + 4, top + row_index * row_h + 16),
                             cell, fontsize=9)

    data = document.tobytes()
    document.close()
    return data


# ---------------------------------------------------------------------------
# mock model server
# ---------------------------------------------------------------------------

PARSE_PAYLOAD: Dict[str, Any] = {
    "title": "Attention Is All You Need",
    "title_original": "",
    "authors": [
        {"name": "Ashish Vaswani", "affiliations": ["Google Brain"],
         "corresponding": True, "email": "avaswani@google.com"},
        {"name": "Noam Shazeer", "affiliations": ["Google Brain", "University of Toronto"]},
    ],
    "affiliations": [
        {"name": "Google Brain", "department": "Research", "country": "USA"},
        {"name": "University of Toronto", "department": "Computer Science", "country": "Canada"},
    ],
    "journal": "NeurIPS",
    "venue_type": "conference",
    "published_at": "2017-06-12",
    "year": 2017,
    "doi": "10.48550/arXiv.1706.03762",
    "url": "",
    "abstract": ("The dominant sequence transduction models are based on complex recurrent or "
                 "convolutional neural networks. We propose the Transformer, a model architecture "
                 "relying entirely on self-attention to draw global dependencies between input "
                 "and output. Experiments on WMT 2014 English-to-German translation reach 28.4 BLEU."),
    "keywords": ["attention", "transformer", "machine translation"],
    "language": "en",
    "sections": [
        {"title": "Introduction",
         "text": ("Recurrent models process sequences left to right, which precludes "
                  "parallelisation. Attention mechanisms allow modelling of dependencies "
                  "regardless of distance, and have been used in various sequence tasks."),
         "section": "background"},
        {"title": "Model Architecture",
         "text": ("The Transformer follows an encoder-decoder structure using stacked "
                  "self-attention and point-wise feed-forward layers. The encoder has six "
                  "identical layers, each with multi-head self-attention and a feed-forward "
                  "network. The decoder adds cross-attention over the encoder output."),
         "section": "method"},
        {"title": "Results",
         "text": ("On WMT 2014 English-to-German translation the big Transformer reaches "
                  "28.4 BLEU, more than 2 BLEU above the previous best result, while training "
                  "cost is a small fraction of the baselines."),
         "section": "experiment"},
        {"title": "Conclusion",
         "text": ("We presented the Transformer, the first sequence transduction model relying "
                  "entirely on attention. It trains faster and translates better than "
                  "architectures based on recurrent or convolutional layers."),
         "section": "conclusion"},
    ],
    "figures": [
        {"label": "fig1",
         "caption": "Figure 1: The Transformer architecture, showing the encoder and decoder stacks.",
         "page": 2,
         "content": "编码器-解码器结构，多头自注意力与逐位置前馈网络。"},
    ],
    "tables": [
        {"label": "tab1",
         "caption": "Table 1: BLEU scores on WMT 2014 English-to-German translation.",
         "page": 3,
         "content": "| Model | BLEU | Params |\n|---|---|---|\n| Transformer (big) | 28.4 | 213M |"},
    ],
    "contributions": ["提出完全基于注意力的序列转换模型", "在 WMT 2014 上达到 28.4 BLEU"],
    "limitations": ["自注意力的复杂度随序列长度平方增长"],
    "future_work": ["将 Transformer 应用于图像与音频"],
}


class ModelState:
    def __init__(self) -> None:
        self.requests: List[Dict[str, Any]] = []
        self.parse_calls = 0
        self.digest_a_calls = 0
        self.digest_b_calls = 0


STATE = ModelState()


def _system_text(payload: Dict[str, Any]) -> str:
    for message in payload.get("messages") or []:
        if message.get("role") == "system":
            content = message.get("content")
            if isinstance(content, str):
                return content
    return ""


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
        user = _last_user_text(payload)

        if "结构化抽取引擎" in system:
            STATE.parse_calls += 1
            content = json.dumps(PARSE_PAYLOAD, ensure_ascii=False)
        elif "学术编辑" in system:
            STATE.digest_a_calls += 1
            section = ""
            for line in user.splitlines():
                if line.startswith("章节标题："):
                    section = line.split("：", 1)[1].strip()
            content = (f"{section} 部分的大意：本文在该部分给出了具体方法与实验证据，"
                       f"关键数值为 28.4 BLEU，模型由六层自注意力堆叠而成。")
        elif "学术综述作者" in system:
            STATE.digest_b_calls += 1
            dimension = ""
            for line in user.splitlines():
                if line.startswith("目标维度："):
                    dimension = line.split("：", 1)[1].strip()
            content = json.dumps({
                "summary": f"{dimension}维度的要点概括：方法完全基于自注意力，实验达到 28.4 BLEU。",
                "points": [
                    {"text": f"{dimension}：Transformer 由六层相同的自注意力层堆叠而成。",
                     "assets": ["fig1"]},
                    {"text": f"{dimension}：在 WMT 2014 英德翻译上达到 28.4 BLEU。",
                     "assets": ["tab1"]},
                    {"text": f"{dimension}：训练成本显著低于循环与卷积基线。", "assets": []},
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
        else:
            content = "{}"

        self._send_json(200, {
            "id": "chatcmpl-mock",
            "model": payload.get("model"),
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
        })


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
# fixture
# ---------------------------------------------------------------------------

class IngestFixture:
    """Services wired to the mock model with a lexical embedder."""

    def __init__(self, server: MockModelServer, mode: str = "file") -> None:
        self._tmp = TempWorkspace()
        settings = Settings(
            providers=[{
                "id": "mock", "name": "Mock", "base_url": server.base_url,
                "api_key": "sk-test", "models": ["mock-large"], "vision": True,
            }],
            routing={
                "parse": {"provider": "mock", "model": "mock-large", "temperature": 0.0},
                "chat": {"provider": "mock", "model": "mock-large"},
                "digest": {"provider": "mock", "model": "mock-large", "temperature": 0.2},
            },
            embedding={"provider": "lexical", "dimension": 128, "model": "hashed-ngram"},
            ingest={"pdf_mode": mode, "vision_batch_pages": 5, "render_dpi": 96,
                    "image_dpi": 110, "max_retry": 1},
            retrieval={"min_score": 0.05, "stop_at_b": False,
                       "cross_link_min_score": 0.05, "cross_link_top_k": 3},
        )
        self.services = create_services(str(self._tmp.path), settings=settings)

    def close(self) -> None:
        self.services.close()
        self._tmp.cleanup()


# ---------------------------------------------------------------------------
# layout tests
# ---------------------------------------------------------------------------

def test_layout_detects_and_crops_figure_and_table():
    from paperopenclaw.ingest.pdf_layout import analyse_pdf

    pdf = build_sample_pdf()
    report = analyse_pdf(pdf, dpi=110, min_area_ratio=0.012)

    assert report.page_count == 3
    assert report.page_texts and "Attention" in report.page_texts[0]

    labels = {item.label: item for item in report.assets}
    assert "fig1" in labels, f"未检出图：{[a.label for a in report.assets]}"
    figure = labels["fig1"]
    assert figure.kind == "figure"
    assert figure.page == 2
    assert "Transformer architecture" in figure.caption
    assert figure.data[:8] == b"\x89PNG\r\n\x1a\n", "裁剪结果应为 PNG"
    assert figure.width > 100 and figure.height > 50

    assert "tab1" in labels, f"未检出表：{[a.label for a in report.assets]}"
    table = labels["tab1"]
    assert table.kind == "table"
    assert table.page == 3
    assert "BLEU scores" in table.caption

    captions = {(item.kind, item.number) for item in report.captions}
    assert ("figure", "1") in captions
    assert ("table", "1") in captions


def test_layout_numbering_follows_appearance_order():
    from paperopenclaw.ingest.pdf_layout import analyse_pdf

    report = analyse_pdf(build_sample_pdf(), dpi=96)
    figures = [item for item in report.assets if item.kind == "figure"]
    tables = [item for item in report.assets if item.kind == "table"]
    assert [item.label for item in figures] == [f"fig{i + 1}" for i in range(len(figures))]
    assert [item.label for item in tables] == [f"tab{i + 1}" for i in range(len(tables))]
    assert [item.ordinal for item in figures] == list(range(1, len(figures) + 1))


def test_body_reference_to_figure_is_not_extracted():
    """正文里的 "Figure 1 shows …" / "图 1 显示了 …" 不得触发图表提取。

    这是重设计第 7 条要求：不能因为文中出现 fig1 字样就误提图。
    """
    import fitz

    from paperopenclaw.ingest.pdf_layout import analyse_pdf

    document = fitz.open()
    page = document.new_page(width=PAGE_W, height=PAGE_H)
    # 一个多行段落里引用 Figure 1（无真实图片）。
    page.insert_textbox(
        fitz.Rect(60, 60, PAGE_W - 60, 260),
        "Figure 1 shows the overall architecture of our proposed pipeline. "
        "The encoder and decoder are stacked, and training proceeds end to end. "
        "We evaluate on three datasets and report BLEU as the main metric.",
        fontsize=11,
    )
    # 一个多行段落里引用「图 1」（中文）。
    page.insert_textbox(
        fitz.Rect(60, 300, PAGE_W - 60, 500),
        "图 1 显示了本文提出的整体流程。编码器与解码器堆叠，训练端到端进行。"
        "我们在三个数据集上评估，并报告 BLEU 指标。",
        fontsize=11,
    )
    data = document.tobytes()
    document.close()

    report = analyse_pdf(data, dpi=96)
    assert report.assets == [], f"正文中的图表引用不应被提取：{[a.label for a in report.assets]}"
    assert report.captions == [], f"正文引用不应被识别为图注：{[c.text for c in report.captions]}"


def test_standalone_caption_still_extracted():
    """独立的短图注块仍应被识别（收紧规则不能误伤真图注）。"""
    import fitz

    from paperopenclaw.ingest.pdf_layout import analyse_pdf

    document = fitz.open()
    page = document.new_page(width=PAGE_W, height=PAGE_H)
    page.insert_image(fitz.Rect(90, 120, 490, 360), stream=_figure_png())
    # 单行独立图注块。
    page.insert_textbox(
        fitz.Rect(90, 380, 490, 410),
        "Figure 1: The Transformer architecture.",
        fontsize=10,
    )
    data = document.tobytes()
    document.close()

    report = analyse_pdf(data, dpi=96)
    labels = [item.label for item in report.assets]
    assert "fig1" in labels, f"独立图注应被提取：{labels}"
    figure = next(item for item in report.assets if item.label == "fig1")
    assert "Transformer architecture" in figure.caption


# ---------------------------------------------------------------------------
# pipeline tests
# ---------------------------------------------------------------------------

def test_full_ingest_produces_all_artefacts():
    with MockModelServer() as server:
        fixture = IngestFixture(server)
        try:
            services = fixture.services
            result = services.ingest.ingest_bytes(build_sample_pdf(),
                                                  filename="attention.pdf")
            hash_value = result["hash"]
            assert len(hash_value) == 64

            # ---- paper table (FR-2) --------------------------------
            paper = services.papers.get(hash_value)
            assert paper["title"] == "Attention Is All You Need"
            assert paper["journal"] == "NeurIPS"
            assert paper["published_at"] == "2017-06-12"
            assert paper["year"] == 2017
            assert paper["language"] == "en"
            assert paper["status"] == "ready"
            assert paper["page_count"] == 3

            authors = services.papers.list_authors(hash_value)
            assert [item["name"] for item in authors] == ["Ashish Vaswani", "Noam Shazeer"]
            assert authors[0]["is_corresponding"] == 1
            # Noam carries two institutions (FR-2.3).
            assert set(authors[1]["affiliations"]) == {"Google Brain", "University of Toronto"}
            assert {item["name"] for item in services.papers.list_affiliations(hash_value)} == {
                "Google Brain", "University of Toronto"}

            # ---- assets in fig/ and tab/ (FR-4) --------------------
            assets = services.papers.list_assets(hash_value)
            by_label = {item["label"]: item for item in assets}
            assert "fig1" in by_label and "tab1" in by_label

            fig_path = services.paths.paper_fig_dir(hash_value) / "fig1.png"
            tab_path = services.paths.paper_tab_dir(hash_value) / "tab1.png"
            assert fig_path.is_file(), "图应存放在 fig/ 目录"
            assert tab_path.is_file(), "表应存放在 tab/ 目录"
            assert by_label["fig1"]["file_path"].endswith(f"papers/{hash_value}/fig/fig1.png")
            assert by_label["tab1"]["file_path"].endswith(f"papers/{hash_value}/tab/tab1.png")
            # The model's captions were merged in (FR-3 + FR-4 split of duty).
            assert "Transformer" in by_label["fig1"]["caption"]
            assert by_label["fig1"]["caption_source"] in ("merged", "model")
            assert by_label["fig1"]["section"], "图表应归属到某个鱼骨维度"

            # ---- OKF bundle (FR-5) --------------------------------
            bundle = services.paths.paper_okf_dir(hash_value)
            assert (bundle / "index.md").is_file()
            assert (bundle / "log.md").is_file()
            assert (bundle / "paper.md").is_file()
            assert (bundle / "fulltext.md").is_file(), "OKF 应包含全文（不删减）"
            assert (bundle / "figures" / "fig1.md").is_file()
            assert (bundle / "tables" / "tab1.md").is_file()
            assert list((bundle / "sections").glob("*.md"))

            # ---- OKF 全文：来自 PyMuPDF 的完整正文，不删减 -------------
            fulltext = (bundle / "fulltext.md").read_text(encoding="utf-8")
            assert "第 1 页" in fulltext
            assert "第 2 页" in fulltext and "第 3 页" in fulltext
            # 标题、摘要与图注等正文均须原样保留。
            assert "Attention Is All You Need" in fulltext
            assert "dominant sequence transduction" in fulltext
            assert "Figure 1: The Transformer architecture" in fulltext

            report = validate_bundle(bundle)
            assert report["conformant"] is True, report["errors"]
            assert report["files"] >= 8  # paper + fulltext + 4 sections + fig1 + tab1 + pdf source

            concepts = services.papers.list_okf_concepts(hash_value)
            concept_ids = {item["concept_id"] for item in concepts}
            assert "paper" in concept_ids
            assert "fulltext" in concept_ids
            assert "figures/fig1" in concept_ids
            assert "tables/tab1" in concept_ids
            assert any(item.startswith("sections/") for item in concept_ids)

            # ---- RAG pyramid (FR-6) -------------------------------
            units = services.rag.units_of_paper(hash_value)
            layers = {layer: [u for u in units if u["layer"] == layer] for layer in "ABC"}
            assert len(layers["C"]) == 1
            assert len(layers["B"]) >= 3, f"B 层应覆盖多个维度，实际 {len(layers['B'])}"
            assert len(layers["A"]) == 4

            # C is the abstract verbatim, not a summary (FR-6.3).
            assert layers["C"][0]["text"].startswith("The dominant sequence transduction")
            # A units are the per-section digests.
            assert all(unit["section"] in SPINE_SLUGS for unit in layers["A"])
            # B units carry the structured points the graph consumes.
            for unit in layers["B"]:
                assert unit["meta"].get("points"), "B 层应带 points"
                assert unit["meta"].get("summary") or unit["text"]

            # Adjacent-layer links exist in both directions (FR-6.5).
            a_ids = {unit["id"] for unit in layers["A"]}
            b_ids = {unit["id"] for unit in layers["B"]}
            links = services.rag.all_links(hashes=[hash_value])
            relations = {link["relation"] for link in links}
            assert "a_to_b" in relations and "b_to_c" in relations
            assert all(link["src_unit"] in a_ids for link in links if link["relation"] == "a_to_b")
            assert all(link["src_unit"] in b_ids for link in links if link["relation"] == "b_to_c")

            pyramid = services.rag.pyramid(hash_value)
            assert pyramid["C"]["unit_id"] == unit_id_c(hash_value)
            assert len(pyramid["B"]) == len(layers["B"])
            for unit in pyramid["B"]:
                assert unit["parents"] == [unit_id_c(hash_value)]
                assert unit["children"], "每个 B 单元应至少关联一个 A 单元"

            # ---- vectors ------------------------------------------
            counts = services.vectors.count()
            assert counts["A"] == len(layers["A"])
            assert counts["B"] == len(layers["B"])
            assert counts["C"] == 1

            # ---- fishbone graph (FR-7.1) ---------------------------
            spine = services.fishbone.fishbone(hash_value)
            assert [item["slug"] for item in spine] == SPINE_SLUGS
            assert [item["label"] for item in spine] == [
                "背景", "数据", "方法", "实验", "结论", "讨论", "不足与展望"]
            populated = [item for item in spine if item["nodes"]]
            assert populated, "鱼骨至少应有一个维度挂载了内容"
            total_nodes = sum(len(item["nodes"]) for item in spine)
            assert total_nodes >= 3

            # Figures/tables are attached as links, not bytes (FR-7.4).
            asset_nodes = [
                asset for item in spine for node in item["nodes"] for asset in node["assets"]
            ]
            assert asset_nodes, "应有内容节点挂载图表"
            for asset in asset_nodes:
                assert asset["url"].startswith(f"/api/v1/papers/{hash_value}/assets/")
                assert asset["path"].endswith(".png")

            # ---- knowledge graph (keywords from A, FR-7.1 重设计) ---
            graph = services.kg.subgraph(hashes=[hash_value])
            kinds = {node["kind"] for node in graph["nodes"]}
            assert "paper" in kinds and "keyword" in kinds
            keyword_count = sum(1 for node in graph["nodes"] if node["kind"] == "keyword")
            assert keyword_count >= 1, "知识图谱应从 A 层抽取到关键词"

            # ---- meta snapshot ------------------------------------
            meta = json.loads(services.paths.paper_meta(hash_value).read_text(encoding="utf-8"))
            assert meta["hash"] == hash_value
            assert meta["counts"]["rag"]["C"] == 1
            assert meta["page_count"] == 3

            # ---- job bookkeeping ----------------------------------
            job = services.jobs.get(result["job_id"])
            assert job["status"] == "done"
            assert job["progress"] == 1.0
            assert job["usage"]["prompt_tokens"] > 0
            assert len(job["stages"]) == 7
            assert all(stage["finished_at"] for stage in job["stages"])
        finally:
            fixture.close()


def test_ingest_is_idempotent_by_hash():
    with MockModelServer() as server:
        fixture = IngestFixture(server)
        try:
            services = fixture.services
            pdf = build_sample_pdf()
            first = services.ingest.ingest_bytes(pdf, filename="a.pdf")
            assert services.db.count("papers") == 1

            try:
                services.ingest.ingest_bytes(pdf, filename="a.pdf")
            except DuplicatePaper as exc:
                assert exc.code == "DUPLICATE_PAPER"
            else:
                raise AssertionError("重复入库应被哈希去重拦截")

            assert services.db.count("papers") == 1
            assert services.papers.get(first["hash"])["status"] == "ready"
        finally:
            fixture.close()


def test_ingest_falls_back_to_local_parse_when_model_fails():
    """PNG→LLM 失败时应回退到本地 PyMuPDF 抽取，入库仍能完成。"""
    with MockModelServer() as server:
        fixture = IngestFixture(server)
        try:
            services = fixture.services
            # 解除解析模型，强制触发视觉解析失败 → 本地兜底。
            services.settings.update({"routing": {"parse": {"provider": "", "model": ""}}})
            services.registry.invalidate()

            result = services.ingest.ingest_bytes(build_sample_pdf(), filename="fallback.pdf")
            hash_value = result["hash"]

            paper = services.papers.get(hash_value)
            assert paper["status"] == "ready", f"本地兜底后应入库成功：{paper.get('error')}"
            assert paper["title"], "本地兜底应抽取到标题"
            assert paper["language"] in ("en", "zh", "mixed")

            # OKF 仍生成全文（本地兜底不依赖模型，正文来自 PyMuPDF）。
            fulltext = (services.paths.paper_okf_dir(hash_value) / "fulltext.md").read_text(encoding="utf-8")
            assert "Attention Is All You Need" in fulltext

            # 金字塔至少生成 C 层（A/B 摘要模型仍可用时也会生成）。
            units = services.rag.units_of_paper(hash_value)
            assert any(u["layer"] == "C" for u in units)

            meta = json.loads(services.paths.paper_meta(hash_value).read_text(encoding="utf-8"))
            assert meta["provider"] == "local"
            assert meta["model"] == "pymupdf-fallback"
        finally:
            fixture.close()


def test_ingest_in_image_mode_batches_pages():
    with MockModelServer() as server:
        fixture = IngestFixture(server, mode="image")
        try:
            services = fixture.services
            before = STATE.parse_calls
            request_mark = len(STATE.requests)
            result = services.ingest.ingest_bytes(build_sample_pdf(), filename="imgs.pdf")
            new_requests = STATE.requests[request_mark:]
            # 3 pages at batch size 5 -> one request, but it must have been the
            # image path (no `file` content part).
            assert STATE.parse_calls == before + 1
            file_parts = [
                part for request in new_requests
                for message in request["payload"].get("messages") or []
                for part in (message.get("content") or [])
                if isinstance(part, dict) and part.get("type") == "file"
            ]
            assert not file_parts, "图像模式不应发送 file 内容块"
            image_parts = [
                part for request in new_requests
                for message in request["payload"].get("messages") or []
                for part in (message.get("content") or [])
                if isinstance(part, dict) and part.get("type") == "image_url"
            ]
            assert len(image_parts) >= 3, "应按页发送图像"
            assert services.papers.get(result["hash"])["status"] == "ready"
        finally:
            fixture.close()


def test_cross_paper_c_links_are_created():
    with MockModelServer() as server:
        fixture = IngestFixture(server)
        try:
            services = fixture.services
            first = services.ingest.ingest_bytes(build_sample_pdf(), filename="a.pdf")

            # A second, different paper: alter the abstract so the apex differs.
            original = PARSE_PAYLOAD["abstract"]
            PARSE_PAYLOAD["abstract"] = original + " A companion study on graph networks."
            try:
                second = services.ingest.ingest_bytes(
                    build_sample_pdf().replace(b"Attention Is All You Need",
                                               b"Graph Networks Are All You Need"),
                    filename="b.pdf",
                )
            finally:
                PARSE_PAYLOAD["abstract"] = original

            assert second["hash"] != first["hash"]
            links = services.rag.all_links(relations=["c_to_c"])
            assert links, "应建立跨论文 C 层关联（FR-6.6）"
            connected = {link["src_unit"] for link in links} | {link["dst_unit"] for link in links}
            assert unit_id_c(first["hash"]) in connected
            assert unit_id_c(second["hash"]) in connected

            cross = services.rag.cross_links(unit_id_c(first["hash"]))
            assert cross and cross[0]["hash"] == second["hash"]
        finally:
            fixture.close()


def test_delete_removes_every_store():
    with MockModelServer() as server:
        fixture = IngestFixture(server)
        try:
            services = fixture.services
            result = services.ingest.ingest_bytes(build_sample_pdf(), filename="a.pdf")
            hash_value = result["hash"]
            assert services.paths.paper_dir(hash_value).is_dir()
            assert services.vectors.count()["C"] == 1

            outcome = services.ingest.delete(hash_value)

            assert outcome["deleted"] is True
            assert outcome["files"] is True
            assert not services.paths.paper_dir(hash_value).exists()
            for table in ("papers", "authors", "affiliations", "assets", "okf_concepts",
                          "rag_units"):
                assert services.db.count(table) == 0, f"{table} 未清理"
            # 知识图谱（关键词图）随论文删除清空。
            assert services.kg.stats()["nodes"] == 0
            # 鱼骨图的七个全局骨架节点正确保留，仅论文自身节点被清理。
            assert services.fishbone.stats()["nodes"] == len(SPINE_SLUGS)
            assert services.vectors.count()["C"] == 0
        finally:
            fixture.close()


def test_doctor_reports_healthy_after_ingest():
    with MockModelServer() as server:
        fixture = IngestFixture(server)
        try:
            services = fixture.services
            services.ingest.ingest_bytes(build_sample_pdf(), filename="a.pdf")
            report = services.doctor()
            failing = [item for item in report["checks"] if not item["ok"]]
            assert not failing, f"诊断未通过：{failing}"
            health = services.health()
            assert health["status"] == "ok"
            assert health["counts"]["papers"] == 1
        finally:
            fixture.close()


# ---------------------------------------------------------------------------
# parse normalisation (unit level)
# ---------------------------------------------------------------------------

def test_normalise_payload_tolerates_loose_model_output():
    from paperopenclaw.ingest.vision_parse import normalise_payload

    parsed = normalise_payload({
        "title": " T ",
        "authors": ["Alice", {"name": "Bob", "affiliation": "MIT", "corresponding": True}, {}],
        "affiliations": "MIT",
        "journal": "arXiv",
        "published_at": "2024年5月",
        "keywords": "a, b；c",
        "sections": [{"heading": "Intro", "content": "text"}, "raw string section"],
        "figures": [{"id": "fig1", "title": "Cap", "content": "c"}],
        "tables": [],
    })
    assert parsed.title == "T"
    assert [item["name"] for item in parsed.authors] == ["Alice", "Bob"]
    assert parsed.affiliations[0]["name"] == "MIT"
    assert parsed.venue_type == "preprint"
    assert parsed.published_at == "2024-05"
    assert parsed.year == 2024
    assert parsed.keywords == ["a", "b", "c"]
    assert len(parsed.sections) == 2
    # Spine attribution needs the whole ordered section list, so it happens in
    # the pipeline rather than per section; verify the carry-forward works.
    from paperopenclaw.kg.skeleton import assign_sections

    assigned = assign_sections(parsed.sections)
    assert all(item["section"] in SPINE_SLUGS for item in assigned)
    assert assigned[0]["section"] == "background", "无法归类时首个章节应落到背景"
    assert parsed.figures[0]["label"] == "fig1"
    assert parsed.language == "en"


def test_normalise_payload_rejects_empty():
    from paperopenclaw.core.errors import ParseFailed
    from paperopenclaw.ingest.vision_parse import normalise_payload

    try:
        normalise_payload({"foo": "bar"})
    except ParseFailed:
        return
    raise AssertionError("空载荷应抛出 ParseFailed")


def test_reconcile_assets_matches_by_label_then_position():
    from paperopenclaw.ingest.vision_parse import reconcile_assets

    local = [
        {"kind": "figure", "label": "fig1", "ordinal": 1, "page": 2, "caption": "", "file_path": "f1.png"},
        {"kind": "figure", "label": "fig2", "ordinal": 2, "page": 4, "caption": "local cap", "file_path": "f2.png"},
    ]
    model_figures = [
        {"label": "fig1", "caption": "model cap 1", "content": "c1"},
        {"label": "fig2", "caption": "model cap 2", "content": "c2"},
        {"label": "fig3", "caption": "extra", "content": "c3"},
    ]
    merged = reconcile_assets(local, model_figures, [])
    assert len(merged) == 3
    assert merged[0]["caption"] == "model cap 1"
    assert merged[1]["caption"] == "model cap 2"  # model caption overrides
    assert merged[2]["missing_file"] is True, "多出的模型条目应保留为纯文本条目"


# ---------------------------------------------------------------------------
# runner
# ---------------------------------------------------------------------------

def _run_all() -> int:
    return run_all(globals(), verbose=True)


if __name__ == "__main__":
    raise SystemExit(_run_all())
