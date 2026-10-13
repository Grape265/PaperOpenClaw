"""Job registry backing the ingest progress API and the audit trail (FR-1.5, NFR-5)."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ...core.jsonutil import safe_json_loads
from ..db import Database, utc_now

STAGES: List[Tuple[str, str]] = [
    ("receive", "接收文件"),
    ("layout", "版面分析与图表裁剪"),
    ("vision", "大模型解析"),
    ("okf", "OKF 落盘"),
    ("pyramid", "金字塔 RAG 构建"),
    ("kg", "知识图谱构建"),
    ("finalize", "完成入库"),
]
STAGE_INDEX: Dict[str, int] = {name: index for index, (name, _) in enumerate(STAGES)}
STAGE_TOTAL = len(STAGES)

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"


class JobRepository:
    """CRUD plus progress helpers for ingest jobs."""

    def __init__(self, db: Database) -> None:
        self.db = db

    # ==================================================================
    # lifecycle
    # ==================================================================
    def create(
        self,
        job_id: str,
        *,
        kind: str = "ingest",
        hash_value: str = "",
        filename: str = "",
        message: str = "",
    ) -> Dict[str, Any]:
        now = utc_now()
        self.db.execute(
            "INSERT OR REPLACE INTO jobs (id, kind, hash, filename, stage, stage_index, stage_total, "
            "status, progress, message, error, stages, usage, started_at, finished_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, 0, ?, ?, 0.0, ?, '', '[]', '{}', ?, '', ?)",
            (
                job_id, kind, hash_value or "", filename or "", STAGES[0][0], STAGE_TOTAL,
                STATUS_PENDING, message or "", now, now,
            ),
        )
        return self.get(job_id) or {}

    def get(self, job_id: str) -> Optional[Dict[str, Any]]:
        row = self.db.query_one("SELECT * FROM jobs WHERE id = ?", (job_id,))
        return self._row(row) if row else None

    def get_or_raise(self, job_id: str) -> Dict[str, Any]:
        from ...core.errors import JobNotFound

        job = self.get(job_id)
        if job is None:
            raise JobNotFound(f"任务不存在：{job_id}")
        return job

    def list(
        self,
        status: str = "",
        hash_value: str = "",
        page: int = 1,
        page_size: int = 20,
    ) -> Tuple[List[Dict[str, Any]], int]:
        clauses: List[str] = []
        params: List[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if hash_value:
            clauses.append("hash = ?")
            params.append(hash_value)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        total = int(self.db.query_scalar(f"SELECT COUNT(*) FROM jobs{where}", params, 0) or 0)
        page = max(1, int(page or 1))
        page_size = max(1, min(int(page_size or 20), 200))
        rows = self.db.query_all(
            f"SELECT * FROM jobs{where} ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            params + [page_size, (page - 1) * page_size],
        )
        return [self._row(row) for row in rows], total

    def delete(self, job_id: str) -> bool:
        return self.db.execute("DELETE FROM jobs WHERE id = ?", (job_id,)).rowcount > 0

    def prune_finished(self, keep: int = 200) -> int:
        """Bounds the table so a long-lived install does not grow without limit."""
        rows = self.db.query_all(
            "SELECT id FROM jobs WHERE status IN ('done','failed','cancelled') "
            "ORDER BY created_at DESC LIMIT -1 OFFSET ?",
            (max(0, int(keep)),),
        )
        if not rows:
            return 0
        ids = [row["id"] for row in rows]
        placeholders = ",".join("?" for _ in ids)
        cursor = self.db.execute(f"DELETE FROM jobs WHERE id IN ({placeholders})", ids)
        return cursor.rowcount

    # ==================================================================
    # progress
    # ==================================================================
    def start(self, job_id: str) -> None:
        self.db.execute(
            "UPDATE jobs SET status = ?, started_at = ? WHERE id = ?",
            (STATUS_RUNNING, utc_now(), job_id),
        )

    def set_stage(self, job_id: str, stage: str, message: str = "", progress: Optional[float] = None) -> None:
        """Advance the stage pointer and record when the stage began."""
        index = STAGE_INDEX.get(stage, 0)
        if progress is None:
            progress = round(index / float(STAGE_TOTAL), 4)
        job = self.get(job_id)
        stages = (job or {}).get("stages", [])
        if not any(item.get("stage") == stage for item in stages):
            stages.append({"stage": stage, "started_at": utc_now(), "finished_at": None,
                           "duration_ms": None, "message": message})
        self.db.execute(
            "UPDATE jobs SET stage = ?, stage_index = ?, progress = ?, message = ?, stages = ? WHERE id = ?",
            (stage, index, float(progress), message or "",
             json.dumps(stages, ensure_ascii=False), job_id),
        )

    def finish_stage(self, job_id: str, stage: str, message: str = "") -> None:
        job = self.get(job_id)
        if job is None:
            return
        stages = job.get("stages", [])
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        for item in stages:
            if item.get("stage") == stage and item.get("finished_at") is None:
                item["finished_at"] = utc_now()
                try:
                    started = datetime.fromisoformat(str(item["started_at"]).replace("Z", "+00:00"))
                    item["duration_ms"] = int((now - started).total_seconds() * 1000)
                except Exception:  # pragma: no cover - defensive
                    item["duration_ms"] = None
                if message:
                    item["message"] = message
        index = STAGE_INDEX.get(stage, 0)
        self.db.execute(
            "UPDATE jobs SET stages = ?, progress = ?, message = ? WHERE id = ?",
            (json.dumps(stages, ensure_ascii=False), round((index + 1) / float(STAGE_TOTAL), 4),
             message or job.get("message", ""), job_id),
        )

    def set_message(self, job_id: str, message: str, progress: Optional[float] = None) -> None:
        if progress is None:
            self.db.execute("UPDATE jobs SET message = ? WHERE id = ?", (message or "", job_id))
        else:
            self.db.execute(
                "UPDATE jobs SET message = ?, progress = ? WHERE id = ?",
                (message or "", float(progress), job_id),
            )

    def add_usage(self, job_id: str, usage: Dict[str, Any]) -> None:
        job = self.get(job_id)
        if job is None:
            return
        merged = dict(job.get("usage") or {})
        for key, value in (usage or {}).items():
            if isinstance(value, (int, float)) and isinstance(merged.get(key), (int, float)):
                merged[key] = merged[key] + value
            else:
                merged[key] = value
        self.db.execute(
            "UPDATE jobs SET usage = ? WHERE id = ?",
            (json.dumps(merged, ensure_ascii=False), job_id),
        )

    def complete(self, job_id: str, message: str = "入库完成") -> None:
        self.db.execute(
            "UPDATE jobs SET status = ?, progress = 1.0, message = ?, finished_at = ? WHERE id = ?",
            (STATUS_DONE, message, utc_now(), job_id),
        )

    def fail(self, job_id: str, error: str) -> None:
        self.db.execute(
            "UPDATE jobs SET status = ?, error = ?, message = ?, finished_at = ? WHERE id = ?",
            (STATUS_FAILED, error or "未知错误", error or "入库失败", utc_now(), job_id),
        )

    def cancel(self, job_id: str, message: str = "已取消") -> None:
        self.db.execute(
            "UPDATE jobs SET status = ?, message = ?, finished_at = ? WHERE id = ?",
            (STATUS_CANCELLED, message, utc_now(), job_id),
        )

    def is_cancelled(self, job_id: str) -> bool:
        status = self.db.query_scalar("SELECT status FROM jobs WHERE id = ?", (job_id,), "")
        return status == STATUS_CANCELLED

    def running_jobs(self) -> List[Dict[str, Any]]:
        rows = self.db.query_all("SELECT * FROM jobs WHERE status = ?", (STATUS_RUNNING,))
        return [self._row(row) for row in rows]

    def mark_stale_running_as_failed(self, reason: str = "服务重启导致任务中断") -> int:
        """Called at startup: no job can legitimately be running yet."""
        cursor = self.db.execute(
            "UPDATE jobs SET status = ?, error = ?, message = ?, finished_at = ? WHERE status IN (?, ?)",
            (STATUS_FAILED, reason, reason, utc_now(), STATUS_RUNNING, STATUS_PENDING),
        )
        return cursor.rowcount

    @staticmethod
    def _row(row: Any) -> Dict[str, Any]:
        item = dict(row)
        item["stages"] = safe_json_loads(item.get("stages"), [])
        item["usage"] = safe_json_loads(item.get("usage"), {})
        item["progress"] = float(item.get("progress") or 0.0)
        return item
