"""Paper endpoints: listing, detail, deletion, PDF/asset/OKF access, export."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from ...core.errors import AssetNotFound, BadRequest, ConceptNotFound, PaperNotFound
from ...core.hashing import short_hash
from ...memory.okf_fs import OKFError, build_tree, load_concept, validate_bundle
from ...services import Services
from ...storage.repos.papers import PaperRepository
from ..deps import get_services, get_runner
from ..filestream import stream_file
from ..responses import success
from ..runner import JobRunner
from ..schemas import ReparseRequest

router = APIRouter(tags=["papers"])


def _require_paper(services: Services, hash_value: str) -> Dict[str, Any]:
    paper = services.papers.get(hash_value)
    if paper is None:
        raise PaperNotFound(f"论文不存在：{hash_value}")
    return paper


# ---------------------------------------------------------------------------
# listing and detail
# ---------------------------------------------------------------------------

@router.get("/papers")
def list_papers(
    request: Request,
    q: str = "",
    hash: str = "",
    hashes: str = "",
    author: str = "",
    journal: str = "",
    year_from: Optional[int] = None,
    year_to: Optional[int] = None,
    status: str = "",
    sort: str = "-created_at",
    page: int = 1,
    page_size: int = 20,
    services: Services = Depends(get_services),
) -> Dict[str, Any]:
    hash_list = [item.strip() for item in hashes.split(",") if item.strip()] if hashes else None
    rows, total = services.papers.list(
        q=q, hash_value=hash, hashes=hash_list, author=author, journal=journal,
        year_from=year_from, year_to=year_to, status=status,
        sort=sort, page=page, page_size=page_size,
    )
    keys = [row["hash"] for row in rows]
    counts = services.papers.counts_for_many(keys)
    authors = services.papers.authors_for_many(keys)

    items = [
        PaperRepository.summarise(row, counts.get(row["hash"]), authors.get(row["hash"]))
        for row in rows
    ]
    return success(
        {"items": items, "total": total, "page": max(1, page),
         "page_size": max(1, min(page_size, 200))},
        request,
    )


@router.get("/papers/facets")
def paper_facets(request: Request, services: Services = Depends(get_services)) -> Dict[str, Any]:
    """Filter values for the library page: journals, year range, statuses."""
    low, high = services.papers.year_range()
    statuses = [
        {"value": row["status"], "count": int(row["n"])}
        for row in services.db.query_all(
            "SELECT status, COUNT(*) AS n FROM papers GROUP BY status ORDER BY status")
    ]
    return success({
        "journals": services.papers.distinct_journals(),
        "year_from": low,
        "year_to": high,
        "statuses": statuses,
        "total": services.db.count("papers"),
    }, request)


@router.get("/papers/{hash_value}")
def get_paper(request: Request, hash_value: str,
              services: Services = Depends(get_services)) -> Dict[str, Any]:
    paper = _require_paper(services, hash_value)

    assets = services.papers.list_assets(hash_value)
    for asset in assets:
        asset["url"] = f"/api/v1/papers/{hash_value}/assets/{asset['label']}"

    concepts = services.papers.list_okf_concepts(hash_value)
    pyramid = services.rag.pyramid(hash_value)

    spine = services.fishbone.fishbone(hash_value)
    kg_summary = {
        "nodes": sum(len(item["nodes"]) for item in spine),
        "sections": [item["slug"] for item in spine if item["nodes"]],
        "spine": [{"slug": item["slug"], "label": item["label"],
                   "count": len(item["nodes"])} for item in spine],
    }

    ingest_info: Dict[str, Any] = {}
    meta_path = services.paths.paper_meta(hash_value)
    if meta_path.is_file():
        try:
            ingest_info = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - a corrupt snapshot must not 500
            ingest_info = {"error": "meta.json 无法解析"}

    job_rows, _total = services.jobs.list(hash_value=hash_value, page_size=5)

    return success({
        "paper": PaperRepository.summarise(
            paper,
            services.papers.counts_for(hash_value),
            services.papers.list_authors(hash_value),
        ),
        "abstract": paper.get("abstract", ""),
        "doi": paper.get("doi", ""),
        "url": paper.get("url", ""),
        "affiliations": services.papers.list_affiliations(hash_value),
        "assets": assets,
        "okf": {
            "root": paper.get("okf_path", ""),
            "concepts": concepts,
        },
        "pyramid": {
            "counts": pyramid.get("counts", {}),
            "C": pyramid.get("C"),
            "B": pyramid.get("B", []),
            "A": pyramid.get("A", []),
        },
        "kg_summary": kg_summary,
        "ingest": ingest_info,
        "recent_jobs": job_rows,
    }, request)


@router.get("/papers/{hash_value}/status")
def get_paper_status(request: Request, hash_value: str,
                     services: Services = Depends(get_services)) -> Dict[str, Any]:
    paper = _require_paper(services, hash_value)
    return success({
        "hash": hash_value,
        "status": paper.get("status", ""),
        "error": paper.get("error", ""),
        "counts": services.papers.counts_for(hash_value),
        "updated_at": paper.get("updated_at", ""),
    }, request)


@router.delete("/papers/{hash_value}")
def delete_paper(request: Request, hash_value: str,
                 services: Services = Depends(get_services)) -> Dict[str, Any]:
    _require_paper(services, hash_value)
    outcome = services.ingest.delete(hash_value)
    return success(outcome, request)


@router.post("/papers/{hash_value}/reparse")
def reparse_paper(request: Request, hash_value: str, body: ReparseRequest,
                  services: Services = Depends(get_services),
                  runner: JobRunner = Depends(get_runner)) -> Dict[str, Any]:
    _require_paper(services, hash_value)
    if not services.paths.paper_pdf(hash_value).is_file():
        raise BadRequest("原始 PDF 不存在，无法重新解析")

    from ...core.hashing import stable_id
    from ...storage.db import utc_now

    job_id = f"job_{stable_id('reparse', hash_value, utc_now(), length=12)}"
    services.jobs.create(job_id, kind="reparse", hash_value=hash_value,
                         message="已排队等待重新解析")
    runner.submit(job_id, services.ingest.reparse, hash_value,
                  from_stage=body.from_stage, force=body.force)
    return success({"job_id": job_id, "hash": hash_value}, request)


# ---------------------------------------------------------------------------
# files
# ---------------------------------------------------------------------------

@router.get("/papers/{hash_value}/pdf")
def get_paper_pdf(request: Request, hash_value: str,
                  services: Services = Depends(get_services)) -> Response:
    paper = _require_paper(services, hash_value)
    path = services.paths.paper_pdf(hash_value)
    if not path.is_file():
        stored = paper.get("file_path") or ""
        if stored:
            path = services.paths.resolve(stored)
    return stream_file(path, request, media_type="application/pdf",
                       filename=f"{paper.get('title') or hash_value}.pdf", inline=True)


@router.get("/papers/{hash_value}/meta.json")
def get_paper_meta(request: Request, hash_value: str,
                   services: Services = Depends(get_services)) -> Dict[str, Any]:
    _require_paper(services, hash_value)
    path = services.paths.paper_meta(hash_value)
    if not path.is_file():
        return success({}, request)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise BadRequest(f"meta.json 无法解析：{exc}") from exc
    return success(payload, request)


@router.get("/papers/{hash_value}/assets")
def list_assets(request: Request, hash_value: str, kind: str = "",
                services: Services = Depends(get_services)) -> Dict[str, Any]:
    _require_paper(services, hash_value)
    assets = services.papers.list_assets(hash_value, kind=kind)
    items = []
    for asset in assets:
        item = dict(asset)
        item["url"] = f"/api/v1/papers/{hash_value}/assets/{asset['label']}"
        items.append(item)
    return success({"items": items}, request)


@router.get("/papers/{hash_value}/assets/{label}")
def get_asset(request: Request, hash_value: str, label: str,
              services: Services = Depends(get_services)) -> Response:
    _require_paper(services, hash_value)
    asset = services.papers.get_asset(hash_value, label)
    if asset is None:
        raise AssetNotFound(f"图表不存在：{label}")

    path: Optional[Path] = None
    if asset.get("file_path"):
        candidate = services.paths.resolve(asset["file_path"])
        if candidate.is_file():
            path = candidate
    if path is None:
        fallback = services.paths.asset_file(hash_value, label)
        if fallback.is_file():
            path = fallback
    if path is None:
        raise AssetNotFound(f"图表文件缺失：{label}")

    return stream_file(path, request, filename=f"{label}.png", inline=True)


# ---------------------------------------------------------------------------
# OKF
# ---------------------------------------------------------------------------

@router.get("/papers/{hash_value}/okf")
def get_okf_tree(request: Request, hash_value: str,
                 services: Services = Depends(get_services)) -> Dict[str, Any]:
    paper = _require_paper(services, hash_value)
    bundle = services.paths.paper_okf_dir(hash_value)
    tree = build_tree(bundle)
    return success({
        "okf_version": "0.2",
        "root": paper.get("okf_path", "") or services.paths.to_relative(bundle),
        "exists": bundle.is_dir(),
        "tree": tree,
    }, request)


@router.get("/papers/{hash_value}/okf/verify")
def verify_okf(request: Request, hash_value: str,
               services: Services = Depends(get_services)) -> Dict[str, Any]:
    _require_paper(services, hash_value)
    report = validate_bundle(services.paths.paper_okf_dir(hash_value))
    return success(report, request)


@router.get("/papers/{hash_value}/okf/{concept_id:path}")
def get_okf_concept(request: Request, hash_value: str, concept_id: str,
                    services: Services = Depends(get_services)) -> Dict[str, Any]:
    _require_paper(services, hash_value)
    concept_id = concept_id[:-3] if concept_id.endswith(".md") else concept_id
    record = services.papers.get_okf_concept(hash_value, concept_id)

    path: Optional[Path] = None
    if record is not None:
        candidate = services.paths.resolve(record["file_path"])
        if candidate.is_file():
            path = candidate
    if path is None:
        candidate = (services.paths.paper_okf_dir(hash_value) / f"{concept_id}.md").resolve()
        if services.paths.is_inside(candidate) and candidate.is_file():
            path = candidate
    if path is None:
        raise ConceptNotFound(f"OKF 概念不存在：{concept_id}")

    try:
        parsed = load_concept(path)
    except OKFError as exc:
        raise ConceptNotFound(f"OKF 概念无法解析：{exc}") from exc

    return success({
        "concept_id": concept_id,
        "file_path": services.paths.to_relative(path),
        "frontmatter": parsed["frontmatter"],
        "body": parsed["body"],
        "raw": parsed["raw"],
        "links": parsed["links"],
    }, request)


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------

@router.get("/papers/{hash_value}/export")
def export_paper(request: Request, hash_value: str,
                 services: Services = Depends(get_services)) -> Response:
    """Bundle the PDF, assets and OKF tree into a zip."""
    paper = _require_paper(services, hash_value)
    directory = services.paths.paper_dir(hash_value)
    if not directory.is_dir():
        raise PaperNotFound(f"论文目录不存在：{hash_value}")

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(directory.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(directory).as_posix())
    buffer.seek(0)

    name = f"{paper.get('title') or short_hash(hash_value)}.zip"
    from urllib.parse import quote

    return Response(
        content=buffer.getvalue(),
        media_type="application/zip",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}",
            "Content-Length": str(len(buffer.getvalue())),
        },
    )
