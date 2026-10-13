"""Built-in tools.

Each one is a thin, read-only projection over an existing service: retrieval,
the paper registry, the knowledge graph, OKF documents and assets. Scoping is
applied here rather than trusted from the caller, so an agent restricted to two
papers cannot reach a third by passing a hash (FR-10.2).
"""

from __future__ import annotations

import ast
import json
import operator
from typing import Any, Dict, List, Optional

from ...core.errors import AppError
from ...core.logging import get_logger
from ...kg.skeleton import SPINE_LABELS, SPINE_SLUGS, label_of
from ...memory.okf_fs import OKFError, load_concept
from . import Tool, ToolContext, ToolRegistry

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# rag_search (FR-11.2)
# ---------------------------------------------------------------------------

RAG_SEARCH_PARAMS = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "检索问题或关键词，使用论文原术语效果最好"},
        "layers": {
            "type": "array",
            "items": {"type": "string", "enum": ["C", "B", "A"]},
            "description": "检索的语义层。默认 C→B→A 级联，通常无需指定。",
        },
        "top_k": {"type": "integer", "description": "每层返回条数，默认使用全局配置"},
        "hashes": {
            "type": "array", "items": {"type": "string"},
            "description": "限定检索的论文哈希；不填则使用本智能体可见范围",
        },
    },
    "required": ["query"],
}


def _tool_rag_search(context: ToolContext, arguments: Dict[str, Any]) -> Dict[str, Any]:
    services = context.services
    query = str(arguments.get("query") or "").strip()
    if not query:
        return {"error": "query 不能为空"}
    layers = arguments.get("layers") or ["C", "B", "A"]
    top_k = arguments.get("top_k")
    hashes = context.scope(arguments.get("hashes"))

    result = services.retriever.search(
        query,
        hashes=hashes,
        layers=[str(item).upper() for item in layers],
        top_k={"c": int(top_k), "b": int(top_k), "a": int(top_k)} if top_k else None,
    )
    evidences = services.retriever.hydrate_evidence(result.get("evidences") or [])
    return {
        "query": result.get("query", query),
        "empty": result.get("empty", True),
        "layers_used": result.get("layers_used", []),
        "candidates": result.get("candidates", []),
        "evidence": [
            {
                "unit_id": item.get("unit_id"),
                "layer": item.get("layer"),
                "hash": item.get("hash"),
                "title": item.get("paper_title") or item.get("title"),
                "section": item.get("section_label") or item.get("section"),
                "score": item.get("score"),
                "asset_refs": item.get("asset_refs", []),
                "text": item.get("text", "")[:1500],
            }
            for item in evidences[:16]
        ],
        "note": "若 empty 为 true，说明库中无相关内容，不要凭常识作答。" if result.get("empty") else "",
    }


# ---------------------------------------------------------------------------
# paper_meta
# ---------------------------------------------------------------------------

PAPER_META_PARAMS = {
    "type": "object",
    "properties": {
        "hashes": {
            "type": "array", "items": {"type": "string"},
            "description": "要查看的论文哈希；不填则返回本智能体可见的全部论文",
        },
        "include_authors": {"type": "boolean", "description": "是否返回作者与单位，默认 true"},
    },
}


def _tool_paper_meta(context: ToolContext, arguments: Dict[str, Any]) -> Dict[str, Any]:
    services = context.services
    hashes = context.scope(arguments.get("hashes"))
    include_authors = arguments.get("include_authors", True)

    if hashes:
        rows, _total = services.papers.list(hashes=hashes, page_size=200)
    else:
        rows, _total = services.papers.list(page_size=50)

    counts = services.papers.counts_for_many([row["hash"] for row in rows])
    authors_map = services.papers.authors_for_many([row["hash"] for row in rows]) if include_authors else {}

    return {
        "count": len(rows),
        "papers": [
            {
                "hash": row["hash"],
                "title": row.get("title", ""),
                "journal": row.get("journal", ""),
                "published_at": row.get("published_at", ""),
                "year": row.get("year"),
                "doi": row.get("doi", ""),
                "language": row.get("language", ""),
                "status": row.get("status", ""),
                "abstract": (row.get("abstract") or "")[:1200],
                "authors": [
                    {"name": author.get("name", ""), "affiliations": author.get("affiliations", [])}
                    for author in authors_map.get(row["hash"], [])
                ] if include_authors else [],
                "counts": counts.get(row["hash"], {}),
            }
            for row in rows
        ],
    }


# ---------------------------------------------------------------------------
# kg_query (FR-7)
# ---------------------------------------------------------------------------

KG_QUERY_PARAMS = {
    "type": "object",
    "properties": {
        "mode": {
            "type": "string", "enum": ["fishbone", "search", "graph"],
            "description": "fishbone=单篇七维骨架；search=按关键词查内容节点；graph=返回子图",
        },
        "hash": {"type": "string", "description": "fishbone 模式下的论文哈希"},
        "query": {"type": "string", "description": "search 模式下的关键词"},
        "sections": {
            "type": "array", "items": {"type": "string", "enum": list(SPINE_SLUGS)},
            "description": "限定维度",
        },
        "hashes": {"type": "array", "items": {"type": "string"}, "description": "限定论文"},
    },
    "required": ["mode"],
}


def _tool_kg_query(context: ToolContext, arguments: Dict[str, Any]) -> Dict[str, Any]:
    services = context.services
    mode = str(arguments.get("mode") or "fishbone").lower()

    if mode == "fishbone":
        hash_value = str(arguments.get("hash") or "").strip()
        scope = context.scope([hash_value] if hash_value else None)
        if scope is not None:
            if not scope:
                return {"error": "该智能体无权访问指定论文"}
            hash_value = hash_value or scope[0]
        elif not hash_value:
            return {"error": "fishbone 模式需要提供 hash"}
        spine = services.fishbone.fishbone(hash_value)
        return {
            "hash": hash_value,
            "spine": [
                {
                    "slug": item["slug"],
                    "label": item["label"],
                    "order": item["order"],
                    "nodes": [
                        {
                            "label": node["label"],
                            "detail": node["detail"][:600],
                            "assets": [
                                {"label": asset["label"], "kind": asset["kind"], "url": asset["url"]}
                                for asset in node.get("assets", [])
                            ],
                        }
                        for node in item["nodes"]
                    ],
                }
                for item in spine
            ],
        }

    if mode == "search":
        query = str(arguments.get("query") or "").strip()
        if not query:
            return {"error": "search 模式需要提供 query"}
        hashes = context.scope(arguments.get("hashes"))
        nodes = services.kg.search(query, hashes=hashes, limit=30)
        return {
            "query": query,
            "count": len(nodes),
            "nodes": [
                {
                    "key": node["node_key"],
                    "hash": node.get("paper_hash", ""),
                    "section": node.get("section", ""),
                    "section_label": label_of(node.get("section") or ""),
                    "label": node["label"],
                    "detail": node["detail"][:800],
                }
                for node in nodes
            ],
        }

    hashes = context.scope(arguments.get("hashes"))
    payload = services.kg.subgraph(hashes=hashes, limit=300)
    from ...kg.graph import stats as graph_stats

    graph = _build_nx(payload)
    return {
        "stats": graph_stats(graph),
        "nodes": [
            {"key": node["node_key"], "kind": node["kind"], "label": node["label"],
             "section": node.get("section", ""), "hash": node.get("paper_hash", "")}
            for node in payload["nodes"][:120]
        ],
        "note": "节点已截断，如需细节请使用 search 或 fishbone 模式。",
    }


def _build_nx(payload: Dict[str, Any]):
    import networkx as nx

    graph = nx.DiGraph()
    for node in payload.get("nodes", []):
        graph.add_node(node["node_key"], **node)
    for edge in payload.get("edges", []):
        graph.add_edge(edge["src_key"], edge["dst_key"], relation=edge.get("relation", ""))
    return graph


# ---------------------------------------------------------------------------
# okf_read (FR-5, FR-8.2)
# ---------------------------------------------------------------------------

OKF_READ_PARAMS = {
    "type": "object",
    "properties": {
        "hash": {"type": "string", "description": "论文哈希"},
        "concept_id": {
            "type": "string",
            "description": "OKF 概念标识，如 paper、sections/method、figures/fig1；不填则返回概念目录树",
        },
    },
    "required": ["hash"],
}


def _tool_okf_read(context: ToolContext, arguments: Dict[str, Any]) -> Dict[str, Any]:
    services = context.services
    hash_value = str(arguments.get("hash") or "").strip()
    scope = context.scope([hash_value] if hash_value else None)
    if scope is not None and (not scope or hash_value not in scope):
        return {"error": "该智能体无权访问指定论文"}
    if not hash_value:
        return {"error": "hash 不能为空"}

    concept_id = str(arguments.get("concept_id") or "").strip()
    if not concept_id:
        return {
            "hash": hash_value,
            "concepts": [
                {"concept_id": item["concept_id"], "type": item["type"],
                 "title": item["title"], "section": item.get("section", "")}
                for item in services.papers.list_okf_concepts(hash_value)
            ],
        }

    record = services.papers.get_okf_concept(hash_value, concept_id)
    if record is None:
        return {"error": f"OKF 概念不存在：{concept_id}"}
    try:
        path = services.paths.resolve(record["file_path"])
        parsed = load_concept(path)
    except (OKFError, AppError) as exc:
        return {"error": str(exc)}
    body = parsed["body"]
    return {
        "concept_id": concept_id,
        "type": parsed["frontmatter"].get("type", ""),
        "title": parsed["frontmatter"].get("title", ""),
        "frontmatter": {
            key: value for key, value in parsed["frontmatter"].items()
            if key in ("type", "title", "description", "section", "section_label",
                       "authors", "journal", "published_at", "year", "doi",
                       "asset_label", "page", "tags")
        },
        "body": body[:6000],
        "links": parsed["links"][:40],
    }


# ---------------------------------------------------------------------------
# asset_list (FR-4, FR-8.3)
# ---------------------------------------------------------------------------

ASSET_LIST_PARAMS = {
    "type": "object",
    "properties": {
        "hash": {"type": "string", "description": "论文哈希；不填则返回可见范围内的全部图表"},
        "kind": {"type": "string", "enum": ["figure", "table"], "description": "只看图或只看表"},
    },
}


def _tool_asset_list(context: ToolContext, arguments: Dict[str, Any]) -> Dict[str, Any]:
    services = context.services
    hash_value = str(arguments.get("hash") or "").strip()
    kind = str(arguments.get("kind") or "").strip().lower()

    if hash_value:
        scope = context.scope([hash_value])
        if scope is not None and hash_value not in scope:
            return {"error": "该智能体无权访问指定论文"}
        targets = [hash_value]
    else:
        targets = context.scope(None) or [row["hash"] for row in
                                          services.papers.list(page_size=50)[0]]

    assets: List[Dict[str, Any]] = []
    for target in targets[:20]:
        for asset in services.papers.list_assets(target, kind=kind):
            assets.append({
                "hash": target,
                "label": asset["label"],
                "kind": asset["kind"],
                "page": asset.get("page", 0),
                "caption": (asset.get("caption") or "")[:600],
                "section": asset.get("section", ""),
                "section_label": label_of(asset.get("section") or ""),
                "url": f"/api/v1/papers/{target}/assets/{asset['label']}",
                "path": asset.get("file_path", ""),
            })
    return {"count": len(assets), "assets": assets}


# ---------------------------------------------------------------------------
# py_eval - controlled arithmetic only
# ---------------------------------------------------------------------------

PY_EVAL_PARAMS = {
    "type": "object",
    "properties": {
        "expression": {
            "type": "string",
            "description": "不含变量与函数的算术表达式，例如 (28.4 - 25.8) / 25.8 * 100",
        },
    },
    "required": ["expression"],
}

_ALLOWED_BINOPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod, ast.Pow: operator.pow,
}
_ALLOWED_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}


def _safe_eval(node: ast.AST) -> float:
    """Evaluate an arithmetic expression with no names, calls or attributes.

    ``eval`` is never used: allowing names or calls would turn a calculator into
    code execution inside the agent loop.
    """
    if isinstance(node, ast.Expression):
        return _safe_eval(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return float(node.value)
        raise ValueError("只允许数字常量")
    if isinstance(node, ast.BinOp) and type(node.op) in _ALLOWED_BINOPS:
        return _ALLOWED_BINOPS[type(node.op)](_safe_eval(node.left), _safe_eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _ALLOWED_UNARY:
        return _ALLOWED_UNARY[type(node.op)](_safe_eval(node.operand))
    raise ValueError("表达式包含不允许的语法")


def _tool_py_eval(context: ToolContext, arguments: Dict[str, Any]) -> Dict[str, Any]:
    expression = str(arguments.get("expression") or "").strip()
    if not expression:
        return {"error": "expression 不能为空"}
    if len(expression) > 500:
        return {"error": "表达式过长"}
    try:
        tree = ast.parse(expression, mode="eval")
        value = _safe_eval(tree)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"无法计算：{exc}"}
    return {"expression": expression, "result": value}


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------

BUILTIN_TOOL_NAMES = (
    "rag_search", "paper_meta", "kg_query", "okf_read", "asset_list", "py_eval",
)


def register_builtin_tools(registry: ToolRegistry) -> None:
    registry.register(Tool(
        name="rag_search",
        description=(
            "在论文金字塔语义库中检索证据。默认按 C（摘要）→B（维度要点）→A（章节详细大意）"
            "级联下钻，返回带来源编号的证据。回答任何论文相关问题前都应先调用本工具。"
        ),
        parameters=RAG_SEARCH_PARAMS,
        handler=_tool_rag_search,
    ))
    registry.register(Tool(
        name="paper_meta",
        description="查询论文元数据：标题、作者与单位、期刊、刊发时间、摘要与统计信息。",
        parameters=PAPER_META_PARAMS,
        handler=_tool_paper_meta,
    ))
    registry.register(Tool(
        name="kg_query",
        description=(
            "查询鱼骨知识图谱。fishbone 返回单篇论文的七个维度"
            "（背景/数据/方法/实验/结论/讨论/不足与展望）及其挂载内容与图表；"
            "search 按关键词查找内容节点；graph 返回子图统计。"
        ),
        parameters=KG_QUERY_PARAMS,
        handler=_tool_kg_query,
    ))
    registry.register(Tool(
        name="okf_read",
        description="读取论文的 OKF 概念文档（论文总览、章节、图表说明）或列出概念目录树。",
        parameters=OKF_READ_PARAMS,
        handler=_tool_okf_read,
    ))
    registry.register(Tool(
        name="asset_list",
        description="列出论文中裁剪出的图（figN）与表（tabN）及其图注、页码与访问地址。",
        parameters=ASSET_LIST_PARAMS,
        handler=_tool_asset_list,
    ))
    registry.register(Tool(
        name="py_eval",
        description="计算纯算术表达式，用于核对论文中的数值比较与百分比变化。不接受变量或函数。",
        parameters=PY_EVAL_PARAMS,
        handler=_tool_py_eval,
    ))
