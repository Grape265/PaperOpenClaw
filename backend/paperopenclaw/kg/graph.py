"""NetworkX view over the persisted knowledge graph.

The relational tables are the source of truth (see
:mod:`paperopenclaw.storage.repos.kg`); this module materialises a graph for
traversal, centrality and export (FR-7.6). It never writes back.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import networkx as nx

from ..storage.repos.kg import KnowledgeGraphRepository


def build_graph(
    repo: KnowledgeGraphRepository,
    hashes: Optional[Sequence[str]] = None,
    sections: Optional[Sequence[str]] = None,
    kinds: Optional[Sequence[str]] = None,
    limit: int = 2000,
) -> nx.DiGraph:
    """Materialise a directed graph from the persisted subgraph."""
    payload = repo.subgraph(hashes=hashes, sections=sections, kinds=kinds, limit=limit)
    graph = nx.DiGraph()
    for node in payload["nodes"]:
        graph.add_node(
            node["node_key"],
            kind=node.get("kind", "content"),
            label=node.get("label", ""),
            detail=node.get("detail", ""),
            section=node.get("section", ""),
            hash=node.get("paper_hash", ""),
            ordinal=node.get("ordinal", 0),
            meta=node.get("meta", {}),
        )
    for edge in payload["edges"]:
        graph.add_edge(
            edge["src_key"],
            edge["dst_key"],
            relation=edge.get("relation", ""),
            weight=float(edge.get("weight", 1.0)),
            meta=edge.get("meta", {}),
        )
    return graph


def to_payload(graph: nx.DiGraph) -> Dict[str, Any]:
    """Serialise a graph into the ``{nodes, edges}`` shape the API returns."""
    nodes = []
    for key, attrs in graph.nodes(data=True):
        nodes.append({
            "key": key,
            "kind": attrs.get("kind", "content"),
            "label": attrs.get("label", ""),
            "detail": attrs.get("detail", ""),
            "section": attrs.get("section", ""),
            "hash": attrs.get("hash", ""),
            "ordinal": attrs.get("ordinal", 0),
            "meta": attrs.get("meta", {}),
        })
    edges = []
    for source, target, attrs in graph.edges(data=True):
        edges.append({
            "source": source,
            "target": target,
            "relation": attrs.get("relation", ""),
            "weight": float(attrs.get("weight", 1.0)),
        })
    return {"nodes": nodes, "edges": edges}


def stats(graph: nx.DiGraph) -> Dict[str, Any]:
    """Cheap structural summary used by the graph page header."""
    if graph.number_of_nodes() == 0:
        return {"nodes": 0, "edges": 0, "components": 0, "density": 0.0, "top_degree": []}
    undirected = graph.to_undirected()
    components = nx.number_connected_components(undirected) if undirected.number_of_nodes() else 0
    degree = sorted(graph.degree, key=lambda pair: pair[1], reverse=True)[:10]
    return {
        "nodes": graph.number_of_nodes(),
        "edges": graph.number_of_edges(),
        "components": components,
        "density": round(nx.density(graph), 6),
        "top_degree": [
            {
                "key": key,
                "degree": int(value),
                "label": graph.nodes[key].get("label", ""),
                "kind": graph.nodes[key].get("kind", ""),
            }
            for key, value in degree
        ],
    }


def path_between(graph: nx.DiGraph, source: str, target: str) -> List[str]:
    """Shortest undirected path between two nodes, or an empty list."""
    if source not in graph or target not in graph:
        return []
    try:
        return nx.shortest_path(graph.to_undirected(), source, target)
    except nx.NetworkXNoPath:
        return []
    except nx.NodeNotFound:  # pragma: no cover - defensive
        return []


def shared_sections(graph: nx.DiGraph, hash_a: str, hash_b: str) -> List[str]:
    """Spine dimensions that both papers actually populate.

    This is what turns a multi-paper graph into a comparison view (FR-7.5).
    """
    def sections_of(hash_value: str) -> set:
        return {
            attrs.get("section", "")
            for _, attrs in graph.nodes(data=True)
            if attrs.get("hash") == hash_value and attrs.get("kind") == "content"
        }

    return sorted(sections_of(hash_a) & sections_of(hash_b))


def export_json(graph: nx.DiGraph, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(to_payload(graph), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def export_graphml(graph: nx.DiGraph, path: Path) -> Path:
    """Write GraphML, flattening nested attribute maps to strings.

    GraphML only accepts scalar attribute values, so ``meta`` dicts are
    serialised to JSON strings rather than dropped.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    flat = nx.DiGraph()
    for key, attrs in graph.nodes(data=True):
        flat.add_node(key, **{
            "kind": str(attrs.get("kind", "")),
            "label": str(attrs.get("label", "")),
            "detail": str(attrs.get("detail", ""))[:4000],
            "section": str(attrs.get("section", "")),
            "hash": str(attrs.get("hash", "")),
            "ordinal": int(attrs.get("ordinal", 0) or 0),
            "meta": json.dumps(attrs.get("meta", {}), ensure_ascii=False),
        })
    for source, target, attrs in graph.edges(data=True):
        flat.add_edge(source, target,
                      relation=str(attrs.get("relation", "")),
                      weight=float(attrs.get("weight", 1.0)))
    nx.write_graphml(flat, str(path))
    return path
