"""Graph endpoints: spine definition, fishbone, keyword graph, export.

Two independent graph stores are exposed here:
  * ``/kg/fishbone/...`` serves the Fishbone Graph (layer-B points, 7-spine);
  * ``/kg/graph`` and friends serve the Knowledge Graph (layer-A keywords).
"""

from __future__ import annotations

import io
import json
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from ...core.errors import PaperNotFound
from ...kg.graph import build_graph, export_graphml, export_json, path_between, stats, to_payload
from ...kg.skeleton import label_of, spine_payload
from ...services import Services
from ..deps import get_services
from ..responses import success

router = APIRouter(tags=["kg"])


@router.get("/kg/sections")
def sections(request: Request) -> Dict[str, Any]:
    """The fixed seven-dimension fishbone definition (FR-7.2)."""
    return success({"items": spine_payload()}, request)


@router.get("/kg/fishbone/{hash_value}")
def fishbone(request: Request, hash_value: str,
             services: Services = Depends(get_services)) -> Dict[str, Any]:
    paper = services.papers.get(hash_value)
    if paper is None:
        raise PaperNotFound(f"论文不存在：{hash_value}")
    spine = services.fishbone.fishbone(hash_value)
    return success({
        "hash": hash_value,
        "title": paper.get("title", ""),
        "spine": spine,
        "counts": {"sections": sum(1 for item in spine if item["nodes"]),
                   "nodes": sum(len(item["nodes"]) for item in spine),
                   "assets": sum(len(node["assets"]) for item in spine for node in item["nodes"])},
    }, request)


@router.get("/kg/graph")
def graph(request: Request, hashes: str = "", limit: int = 5000,
          services: Services = Depends(get_services)) -> Dict[str, Any]:
    hash_list = [item.strip() for item in hashes.split(",") if item.strip()] if hashes else None
    built = build_graph(services.kg, hashes=hash_list, limit=limit)
    payload = to_payload(built)
    payload["stats"] = stats(built)
    return success(payload, request)


@router.get("/kg/search")
def search(request: Request, q: str = "", hashes: str = "",
           limit: int = 50, services: Services = Depends(get_services)) -> Dict[str, Any]:
    hash_list = [item.strip() for item in hashes.split(",") if item.strip()] if hashes else None
    nodes = services.kg.search(q, hashes=hash_list, limit=limit)
    for node in nodes:
        node["section_label"] = label_of(node.get("section") or "")
    return success({"items": nodes, "total": len(nodes)}, request)


@router.get("/kg/path")
def shortest_path(request: Request, source: str, target: str,
                  services: Services = Depends(get_services)) -> Dict[str, Any]:
    built = build_graph(services.kg, limit=5000)
    nodes = path_between(built, source, target)
    return success({
        "source": source,
        "target": target,
        "found": bool(nodes),
        "path": [
            {"key": key, "label": built.nodes[key].get("label", ""),
             "kind": built.nodes[key].get("kind", ""),
             "hash": built.nodes[key].get("hash", "")}
            for key in nodes
        ],
    }, request)


@router.get("/kg/export")
def export(request: Request, hashes: str = "", format: str = "json",
           services: Services = Depends(get_services)) -> Response:
    hash_list = [item.strip() for item in hashes.split(",") if item.strip()] if hashes else None
    built = build_graph(services.kg, hashes=hash_list, limit=5000)

    suffix = "graphml" if format.lower() == "graphml" else "json"
    with tempfile.TemporaryDirectory(prefix="kg_export_") as tmp:
        path = Path(tmp) / f"paperopenclaw_knowledge_graph.{suffix}"
        if suffix == "graphml":
            export_graphml(built, path)
            media = "application/xml"
        else:
            export_json(built, path)
            media = "application/json"
        data = path.read_bytes()

    return Response(
        content=data,
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="paperopenclaw_knowledge_graph.{suffix}"'},
    )


@router.get("/kg/stats")
def graph_stats(request: Request, hashes: str = "",
                services: Services = Depends(get_services)) -> Dict[str, Any]:
    hash_list = [item.strip() for item in hashes.split(",") if item.strip()] if hashes else None
    built = build_graph(services.kg, hashes=hash_list, limit=5000)
    return success(stats(built), request)
