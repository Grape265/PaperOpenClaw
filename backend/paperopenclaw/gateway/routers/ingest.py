"""Ingest and job endpoints: PDF upload, job listing, deletion and CSV export."""

from __future__ import annotations

import csv
import io
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import Response, StreamingResponse

from ...core.errors import BadRequest, DuplicatePaper, JobNotFound
from ...core.events import bus, job_topic
from ...core.hashing import hash_bytes, short_hash, stable_id
from ...core.logging import get_logger
from ...storage.db import utc_now
from ...services import Services
from ..deps import get_runner, get_services
from ..responses import success
from ..runner import JobRunner
from ..sse import from_subscription, sse_response

logger = get_logger(__name__)
router = APIRouter(tags=["ingest"])

MAX_UPLOAD_BYTES = 512 * 1024 * 1024


def _queue(services: Services, runner: JobRunner, data: bytes, filename: str,
           use_llm: bool = True) -> Dict[str, Any]:
    """Validate, deduplicate and queue one upload; return its bookkeeping."""
    content_hash = hash_bytes(data)
    existing = services.papers.get(content_hash)
    if existing is not None and existing.get("status") == "ready":
        return {
            "filename": filename,
            "hash": content_hash,
            "hash_short": short_hash(content_hash),
            "job_id": "",
            "duplicated": True,
            "title": existing.get("title", ""),
        }

    job_id = f"job_{stable_id('ingest', content_hash, utc_now(), length=12)}"
    services.jobs.create(job_id, kind="ingest", hash_value=content_hash,
                         filename=filename, message="已加入队列")
    runner.submit(job_id, services.ingest.ingest_bytes, data,
                  filename=filename, job_id=job_id, use_llm=use_llm)
    return {
        "filename": filename,
        "hash": content_hash,
        "hash_short": short_hash(content_hash),
        "job_id": job_id,
        "duplicated": False,
        "title": "",
    }


@router.post("/ingest/upload")
async def upload(request: Request,
                 files: List[UploadFile] = File(...),
                 use_llm: bool = True,
                 services: Services = Depends(get_services),
                 runner: JobRunner = Depends(get_runner)) -> Dict[str, Any]:
    if not files:
        raise BadRequest("没有收到任何文件")

    items: List[Dict[str, Any]] = []
    accepted = duplicated = 0
    for upload_file in files:
        name = upload_file.filename or "paper.pdf"
        if not name.lower().endswith(".pdf"):
            items.append({"filename": name, "error": "只支持 PDF 文件", "job_id": ""})
            continue
        data = await upload_file.read()
        if not data:
            items.append({"filename": name, "error": "文件为空", "job_id": ""})
            continue
        if len(data) > MAX_UPLOAD_BYTES:
            items.append({"filename": name, "error": "文件超过 512MB 上限", "job_id": ""})
            continue
        entry = _queue(services, runner, data, name, use_llm=use_llm)
        if entry["duplicated"]:
            duplicated += 1
        else:
            accepted += 1
        items.append(entry)

    return success({"items": items, "accepted": accepted, "duplicated": duplicated,
                    "use_llm": bool(use_llm),
                    "job_ids": [item["job_id"] for item in items if item.get("job_id")]},
                   request)


@router.get("/jobs")
def list_jobs(request: Request, status: str = "", hash: str = "",
              page: int = 1, page_size: int = 20,
              services: Services = Depends(get_services)) -> Dict[str, Any]:
    rows, total = services.jobs.list(status=status, hash_value=hash,
                                     page=page, page_size=page_size)
    return success({"items": rows, "total": total, "page": max(1, page),
                    "page_size": max(1, min(page_size, 200))}, request)


@router.get("/jobs/export")
def export_jobs(request: Request, services: Services = Depends(get_services)) -> Response:
    """Export the whole job audit trail as CSV (UTF-8 BOM for Excel)."""
    rows, _total = services.jobs.list(page=1, page_size=10000)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["job_id", "kind", "hash", "filename", "stage", "status",
                     "progress", "message", "error", "started_at", "finished_at", "created_at"])
    for row in rows:
        writer.writerow([
            row.get("id", ""), row.get("kind", ""), row.get("hash", ""),
            row.get("filename", ""), row.get("stage", ""), row.get("status", ""),
            row.get("progress", 0.0), row.get("message", ""), row.get("error", ""),
            row.get("started_at", ""), row.get("finished_at", ""), row.get("created_at", ""),
        ])
    data = ("\ufeff" + buffer.getvalue()).encode("utf-8")
    return Response(
        content=data,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="paperopenclaw_jobs.csv"'},
    )


@router.delete("/jobs/{job_id}")
def delete_job(request: Request, job_id: str,
               services: Services = Depends(get_services)) -> Dict[str, Any]:
    job = services.jobs.get(job_id)
    if job is None:
        raise JobNotFound(f"任务不存在：{job_id}")
    deleted = services.jobs.delete(job_id)
    return success({"job_id": job_id, "deleted": deleted}, request)


@router.get("/jobs/{job_id}")
def get_job(request: Request, job_id: str,
            services: Services = Depends(get_services)) -> Dict[str, Any]:
    job = services.jobs.get(job_id)
    if job is None:
        raise JobNotFound(f"任务不存在：{job_id}")
    return success(job, request)


@router.get("/jobs/{job_id}/events")
async def job_events(request: Request, job_id: str,
                     services: Services = Depends(get_services)) -> StreamingResponse:
    job = services.jobs.get(job_id)
    if job is None:
        raise JobNotFound(f"任务不存在：{job_id}")

    topic = job_topic(job_id)
    subscription = bus.subscribe(topic, backlog=200)

    async def generator():
        # Replay what already happened, then follow live, so a client that
        # connects late still sees the stages it missed.
        try:
            if job.get("status") in ("done", "failed", "cancelled"):
                yield ("event: stage\ndata: "
                       + _json({"stage": job.get("stage", ""),
                                "progress": job.get("progress", 1.0),
                                "message": job.get("message", "")}) + "\n\n")
                yield ("event: done\ndata: "
                       + _json({"status": job.get("status"),
                                "hash": job.get("hash", ""),
                                "error": job.get("error", "")}) + "\n\n")
                return
            async for chunk in from_subscription(subscription, request):
                yield chunk
        finally:
            bus.unsubscribe(subscription)

    return sse_response(generator())


@router.post("/jobs/{job_id}/cancel")
def cancel_job(request: Request, job_id: str,
               services: Services = Depends(get_services),
               runner: JobRunner = Depends(get_runner)) -> Dict[str, Any]:
    job = services.jobs.get(job_id)
    if job is None:
        raise JobNotFound(f"任务不存在：{job_id}")
    if job.get("status") in ("done", "failed", "cancelled"):
        return success({"job_id": job_id, "status": job["status"], "cancelled": False}, request)

    # Cooperative: the pipeline polls the job row between stages.
    services.jobs.cancel(job_id, "用户取消")
    queued_cancelled = runner.cancel(job_id)
    bus.publish(job_topic(job_id), "done", {"status": "cancelled", "hash": job.get("hash", "")})
    return success({"job_id": job_id, "status": "cancelled",
                    "cancelled": True, "queued": queued_cancelled}, request)


def _json(payload: Dict[str, Any]) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False, default=str)
