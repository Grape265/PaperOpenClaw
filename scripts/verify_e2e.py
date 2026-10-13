"""End-to-end acceptance check against a running server.

Verifies the things that unit and gateway tests cannot: that the *built*
frontend is actually served, that deep links return the SPA shell, that static
assets resolve, and that the core API surface answers correctly from a fresh
process with the real data directory.

Usage::

    python -m paperopenclaw serve --port 8791 --no-browser   # in one terminal
    python scripts/verify_e2e.py --base-url http://127.0.0.1:8791

Exit code 0 means every check passed.
"""

from __future__ import annotations

import argparse
import re
import sys
from typing import Any, Callable, Dict, List, Tuple

import httpx

PASS = "PASS"
FAIL = "FAIL"


class Checker:
    def __init__(self, base_url: str) -> None:
        self.base = base_url.rstrip("/")
        self.client = httpx.Client(base_url=self.base, timeout=60.0, follow_redirects=True)
        self.results: List[Tuple[str, bool, str]] = []

    def check(self, name: str, fn: Callable[[], str]) -> None:
        try:
            detail = fn()
            self.results.append((name, True, detail))
        except Exception as exc:  # noqa: BLE001 - the report is the point
            self.results.append((name, False, f"{type(exc).__name__}: {exc}"))

    # ------------------------------------------------------------------
    def run(self) -> int:
        self.check("前端首页可访问并返回 SPA 外壳", self.check_spa)
        self.check("静态资源可加载", self.check_assets)
        self.check("前端深链接回退到 SPA", self.check_deep_link)
        self.check("健康检查", self.check_health)
        self.check("诊断全部通过", self.check_doctor)
        self.check("设置读取且密钥已掩码", self.check_settings)
        self.check("提供方预设", self.check_presets)
        self.check("七维骨架定义", self.check_spine)
        self.check("论文列表与分页包裹", self.check_papers)
        self.check("论文检索参数生效", self.check_paper_search)
        self.check("RAG 金字塔统计", self.check_rag_overview)
        self.check("图谱统计", self.check_kg_stats)
        self.check("智能体角色目录", self.check_profiles)
        self.check("工具注册表", self.check_tools)
        self.check("技能目录", self.check_skills)
        self.check("错误响应为统一包裹", self.check_error_envelope)
        self.check("请求追踪头存在", self.check_request_id)
        self.check("OpenAPI 文档可用", self.check_openapi)
        return self.report()

    # ------------------------------------------------------------------
    def check_spa(self) -> str:
        response = self.client.get("/")
        assert response.status_code == 200, f"HTTP {response.status_code}"
        text = response.text
        assert 'id="app"' in text, "缺少挂载点 #app"
        assert "PaperOpenClaw" in text, "缺少标题"
        size = len(response.content)
        assert size > 200, f"首页过小：{size} 字节"
        return f"{size} 字节，含挂载点与标题"

    def check_assets(self) -> str:
        html = self.client.get("/").text
        scripts = re.findall(r'src="(/assets/[^"]+)"', html)
        styles = re.findall(r'href="(/assets/[^"]+)"', html)
        assert scripts, "首页未引用任何 JS 资源"
        loaded = 0
        for url in scripts + styles:
            response = self.client.get(url)
            assert response.status_code == 200, f"{url} -> HTTP {response.status_code}"
            assert len(response.content) > 0, f"{url} 为空"
            loaded += 1
        return f"加载 {loaded} 个资源（JS {len(scripts)}，CSS {len(styles)}）"

    def check_deep_link(self) -> str:
        checked = 0
        for route in ("/library", "/ingest", "/qa", "/brainstorm", "/graph", "/skills", "/settings"):
            response = self.client.get(route)
            assert response.status_code == 200, f"{route} -> HTTP {response.status_code}"
            assert 'id="app"' in response.text, f"{route} 未返回 SPA 外壳"
            checked += 1
        return f"{checked} 个路由均返回 SPA 外壳"

    def check_health(self) -> str:
        data = self.client.get("/api/v1/health").json()
        assert data["ok"] is True, data
        payload = data["data"]
        assert payload["status"] == "ok"
        assert payload["data_dir"]
        assert set(payload["providers"]) >= {"parse", "chat", "digest", "embedding"}
        return f"版本 {payload['version']}，数据目录 {payload['data_dir']}"

    def check_doctor(self) -> str:
        """Diagnostics must be honest, not universally green.

        On a fresh install no provider is configured, so the model checks are
        *supposed* to fail. The acceptance criterion is therefore conditional:
        all-pass when a provider exists, and precisely the model checks failing
        when none does.
        """
        data = self.client.get("/api/v1/doctor").json()["data"]
        checks = {item["name"]: item["ok"] for item in data["checks"]}

        providers = self.client.get("/api/v1/health").json()["data"]["providers"]
        configured = {
            kind for kind, info in providers.items()
            if kind in ("parse", "chat") and info.get("configured")
        }

        model_checks = ["解析模型已配置", "问答模型已配置"]
        infrastructure = [name for name in checks if name not in model_checks]
        failed_infra = [name for name in infrastructure if not checks[name]]
        assert not failed_infra, f"基础设施检查未通过：{failed_infra}"

        if configured:
            failed_models = [name for name in model_checks if not checks[name]]
            assert not failed_models, f"已配置提供方但检查未通过：{failed_models}"
            return f"{len(checks)} 项检查全部通过（已配置模型：{'、'.join(sorted(configured))}）"

        # No provider: the model checks must report failure rather than pass.
        assert all(not checks[name] for name in model_checks), \
            "未配置模型却报告已配置，诊断不可信"
        return f"基础设施 {len(infrastructure)} 项通过；未配置模型，已如实报告"

    def check_settings(self) -> str:
        data = self.client.get("/api/v1/settings").json()["data"]
        assert "routing" in data and "embedding" in data
        for provider in data["providers"]:
            assert provider["api_key_set"] is not None
            if provider["api_key_set"]:
                assert "…" in provider["api_key"] or provider["api_key"] == "****", \
                    "密钥未掩码"
        return f"{len(data['providers'])} 个提供方，嵌入方案 {data['embedding']['provider']}"

    def check_presets(self) -> str:
        items = self.client.get("/api/v1/provider-presets").json()["data"]["items"]
        ids = {item["id"] for item in items}
        assert {"deepseek", "dashscope", "moonshot", "openai", "ollama"} <= ids, ids
        return f"{len(items)} 个预设：{'、'.join(sorted(ids))}"

    def check_spine(self) -> str:
        items = self.client.get("/api/v1/kg/sections").json()["data"]["items"]
        labels = [item["label"] for item in items]
        expected = ["背景", "数据", "方法", "实验", "结论", "讨论", "不足与展望"]
        assert labels == expected, labels
        return "、".join(labels)

    def check_papers(self) -> str:
        data = self.client.get("/api/v1/papers", params={"page_size": 5}).json()["data"]
        assert set(data) >= {"items", "total", "page", "page_size"}
        return f"共 {data['total']} 篇"

    def check_paper_search(self) -> str:
        base = self.client.get("/api/v1/papers", params={"page_size": 1}).json()["data"]["total"]
        filtered = self.client.get("/api/v1/papers",
                                   params={"q": "zzz-not-a-real-paper"}).json()["data"]["total"]
        assert filtered == 0, "无关关键词不应命中"
        first = self.client.get("/api/v1/papers", params={"page_size": 1}).json()["data"]["items"]
        if first:
            short = first[0]["hash"][:10]
            by_hash = self.client.get("/api/v1/papers", params={"hash": short}).json()["data"]["total"]
            assert by_hash >= 1, "按哈希前缀检索失败"
        return f"总量 {base}，无关词命中 {filtered}"

    def check_rag_overview(self) -> str:
        data = self.client.get("/api/v1/rag/overview").json()["data"]
        assert "units" in data and "vectors" in data
        return f"单元 {data['units']}，向量 {data['vectors']}，跨论文关联 {data['cross_links']}"

    def check_kg_stats(self) -> str:
        data = self.client.get("/api/v1/kg/stats").json()["data"]
        assert "nodes" in data and "edges" in data
        return f"节点 {data['nodes']}，边 {data['edges']}，连通分量 {data.get('components')}"

    def check_profiles(self) -> str:
        items = self.client.get("/api/v1/agent/profiles").json()["data"]["items"]
        ids = {item["id"] for item in items}
        assert {"qa", "moderator", "methodologist", "skeptic"} <= ids, ids
        return f"{len(items)} 个角色"

    def check_tools(self) -> str:
        items = self.client.get("/api/v1/agent/tools").json()["data"]["items"]
        names = {item["name"] for item in items}
        assert {"rag_search", "paper_meta", "kg_query", "okf_read", "asset_list"} <= names, names
        return f"{len(items)} 个工具：{'、'.join(sorted(names))}"

    def check_skills(self) -> str:
        data = self.client.get("/api/v1/skills").json()["data"]
        assert "items" in data
        return f"已安装 {len(data['items'])} 个技能"

    def check_error_envelope(self) -> str:
        response = self.client.get("/api/v1/papers/" + "f" * 64)
        assert response.status_code == 404, response.status_code
        payload = response.json()
        assert payload["ok"] is False
        assert payload["data"] is None
        assert payload["error"]["code"] == "PAPER_NOT_FOUND", payload["error"]
        return f"404 返回 {payload['error']['code']}"

    def check_request_id(self) -> str:
        response = self.client.get("/api/v1/health")
        header = response.headers.get("x-request-id")
        assert header, "缺少 X-Request-Id"
        assert response.json()["meta"]["request_id"] == header, "响应体与头部的 request_id 不一致"
        elapsed = response.headers.get("x-elapsed-ms")
        return f"request_id={header[:8]}…，耗时 {elapsed}ms"

    def check_openapi(self) -> str:
        response = self.client.get("/openapi.json")
        assert response.status_code == 200, response.status_code
        schema: Dict[str, Any] = response.json()
        paths = [p for p in schema.get("paths", {}) if p.startswith("/api/v1")]
        assert len(paths) >= 40, f"仅 {len(paths)} 条接口"
        return f"{len(paths)} 条 /api/v1 接口"

    # ------------------------------------------------------------------
    def report(self) -> int:
        width = max(len(name) for name, _, _ in self.results)
        failures = 0
        print(f"\n端到端验收：{self.base}\n")
        for name, ok, detail in self.results:
            flag = PASS if ok else FAIL
            print(f"  [{flag}] {name.ljust(width)}  {detail}")
            if not ok:
                failures += 1
        passed = len(self.results) - failures
        print(f"\n{passed}/{len(self.results)} 项通过")
        self.client.close()
        return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="PaperOpenClaw 端到端验收")
    parser.add_argument("--base-url", default="http://127.0.0.1:8787")
    args = parser.parse_args()
    try:
        return Checker(args.base_url).run()
    except httpx.ConnectError as exc:
        print(f"无法连接 {args.base_url}：{exc}", file=sys.stderr)
        print("请先启动服务：python -m paperopenclaw serve", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
