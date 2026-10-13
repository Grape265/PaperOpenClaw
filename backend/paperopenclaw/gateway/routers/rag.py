"""RAG endpoints: cascade search, pyramid inspection, graph and rebuild."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Request

from ...core.errors import BadRequest
from ...core.events import bus, job_topic
from ...core.hashing import stable_id
from ...kg.skeleton import SPINE_LABELS
from ...memory.retriever import PyramidRetriever
from ...services import Services
from ...storage.db import utc_now
from ..deps import get_runner, get_services
from ..responses import success
from ..runner import JobRunner
from ..schemas import RagRebuildRequest, RagSearchRequest

router = APIRouter(tags=["rag"])


@router.post("/rag/search")
def search(request: Request, body: RagSearchRequest,
           services: Services = Depends(get_services)) -> Dict[str, Any]:
    if not (body.query or "").strip():
        raise BadRequest("query 不能为空")

    result = services.retriever.search(
        body.query,
        hashes=body.hashes or None,
        layers=body.layers or ("C", "B", "A"),
        top_k=body.top_k,
        min_score=body.min_score,
        cascade=body.cascade,
    )
    result["evidences"] = services.retriever.hydrate_evidence(result.get("evidences") or [])
    return success(result, request)


@router.get("/rag/pyramid/{hash_value}")
def pyramid(request: Request, hash_value: str,
            services: Services = Depends(get_services)) -> Dict[str, Any]:
    from ...core.errors import PaperNotFound

    if services.papers.get(hash_value) is None:
        raise PaperNotFound(f"论文不存在：{hash_value}")
    return success(services.rag.pyramid(hash_value), request)


@router.get("/rag/units/{unit_id:path}")
def get_unit(request: Request, unit_id: str,
             services: Services = Depends(get_services)) -> Dict[str, Any]:
    from ...core.errors import BadRequest as _BadRequest

    unit = services.rag.get_unit(unit_id)
    if unit is None:
        raise _BadRequest(f"语义单元不存在：{unit_id}")

    paper = services.papers.get(unit["paper_hash"]) or {}
    parents = services.rag.parents(unit_id)
    children = services.rag.children(unit_id)

    cross: List[Dict[str, Any]] = []
    if unit["layer"] == "C":
        cross = services.rag.cross_links(unit_id)

    return success({
        "unit": {
            **unit,
            "section_label": SPINE_LABELS.get(unit.get("section") or "", ""),
            "paper_title": paper.get("title", ""),
        },
        "parents": [
            {"unit_id": item["neighbour"]["unit_id"], "layer": item["neighbour"]["layer"],
             "title": item["neighbour"]["title"], "section": item["neighbour"]["section"],
             "weight": item["weight"], "relation": item["relation"]}
            for item in parents
        ],
        "children": [
            {"unit_id": item["neighbour"]["unit_id"], "layer": item["neighbour"]["layer"],
             "title": item["neighbour"]["title"], "section": item["neighbour"]["section"],
             "weight": item["weight"], "relation": item["relation"]}
            for item in children
        ],
        "cross_links": cross,
    }, request)


@router.get("/rag/graph")
def rag_graph(request: Request, hashes: str = "", layers: str = "C,B,A",
              services: Services = Depends(get_services)) -> Dict[str, Any]:
    """Nodes and edges of the pyramid, for the structure view."""
    hash_list = [item.strip() for item in hashes.split(",") if item.strip()] if hashes else None
    layer_list = [item.strip().upper() for item in layers.split(",") if item.strip()]

    units = services.rag.unit_lookup(hashes=hash_list, layers=layer_list)
    links = services.rag.all_links(hashes=hash_list)

    titles = {row["hash"]: row.get("title", "") for row in
              services.papers.list(hashes=hash_list, page_size=200)[0]} if hash_list else {}

    nodes = []
    for unit_id, unit in units.items():
        if unit["layer"] == "C":
            links_for_unit = [link for link in links
                              if link["relation"] == "c_to_c"
                              and (link["src_unit"] == unit_id or link["dst_unit"] == unit_id)]
        else:
            links_for_unit = []
        nodes.append({
            "unit_id": unit_id,
            "layer": unit["layer"],
            "hash": unit["paper_hash"],
            "paper_title": titles.get(unit["paper_hash"], ""),
            "title": unit.get("title", ""),
            "section": unit.get("section", ""),
            "section_label": SPINE_LABELS.get(unit.get("section") or "", ""),
            "chars": unit.get("char_count", 0),
            "cross_link_count": len(links_for_unit),
        })

    edges = [
        {"source": link["src_unit"], "target": link["dst_unit"],
         "relation": link["relation"], "weight": link["weight"]}
        for link in links
        if link["src_unit"] in units and link["dst_unit"] in units
    ]
    return success({
        "nodes": nodes,
        "edges": edges,
        "counts": {
            "nodes": len(nodes),
            "edges": len(edges),
            "by_layer": {layer: sum(1 for node in nodes if node["layer"] == layer)
                         for layer in ("C", "B", "A")},
        },
    }, request)


@router.get("/rag/overview")
def overview(request: Request, services: Services = Depends(get_services)) -> Dict[str, Any]:
    """Corpus-level pyramid statistics, used on the graph page header."""
    by_layer = services.rag.count_by_layer()
    cross = services.db.count("rag_links", "relation = 'c_to_c'")
    vectors = services.vectors.count()
    return success({
        "units": by_layer,
        "vectors": vectors,
        "cross_links": cross,
        "papers": services.db.count("papers"),
        "embedding": services.embedder.describe(),
        "sections": SPINE_LABELS,
    }, request)


@router.post("/rag/rebuild")
def rebuild(request: Request, body: RagRebuildRequest,
            services: Services = Depends(get_services),
            runner: JobRunner = Depends(get_runner)) -> Dict[str, Any]:
    """Re-embed the corpus, e.g. after switching the embedding provider."""
    job_id = f"job_{stable_id('rebuild', utc_now(), length=12)}"
    services.jobs.create(job_id, kind="rebuild", message="已排队等待重建向量库")

    def work() -> None:
        services.jobs.start(job_id)
        try:
            targets = body.hashes or services.papers.all_hashes()
            services.jobs.set_stage(job_id, "pyramid",
                                    f"正在重建 {len(targets)} 篇论文的向量", progress=0.1)
            bus.publish(job_topic(job_id), "stage",
                        {"stage": "pyramid", "progress": 0.1,
                         "message": f"正在重建 {len(targets)} 篇论文"})

            from ...ingest.pyramid import PyramidBuilder

            builder = PyramidBuilder(services.registry, services.settings, services.rag,
                                     services.vectors, services.embedder)
            total = 0
            for index, hash_value in enumerate(targets):
                units = services.rag.units_of_paper(hash_value)
                if not units:
                    continue
                total += builder.index_vectors(hash_value, units)
                ratio = 0.1 + 0.8 * (index + 1) / max(len(targets), 1)
                services.jobs.set_message(job_id, f"已重建 {index + 1}/{len(targets)}", ratio)
                bus.publish(job_topic(job_id), "progress",
                            {"message": f"已重建 {index + 1}/{len(targets)}", "progress": ratio})

            services.jobs.set_stage(job_id, "kg", "正在重算跨论文 C 层关联", progress=0.9)
            builder.rebuild_cross_links(targets)
            services.jobs.finish_stage(job_id, "kg", "跨论文关联已更新")
            services.jobs.complete(job_id, f"向量库重建完成，共写入 {total} 条")
            bus.publish(job_topic(job_id), "done", {"status": "done", "vectors": total})
        except Exception as exc:  # noqa: BLE001
            services.jobs.fail(job_id, str(exc))
            bus.publish(job_topic(job_id), "error", {"code": "INTERNAL_ERROR", "message": str(exc)})

    runner.submit(job_id, work)
    return success({"job_id": job_id}, request)
