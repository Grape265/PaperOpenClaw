"""Background job execution.

Ingest and brainstorm are long, blocking, and call out over the network, so they
must not occupy the event loop. A small bounded thread pool keeps the HTTP
response immediate while the work proceeds, and the progress events the workers
publish reach the browser through the SSE endpoints.
"""

from __future__ import annotations

import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable, Dict, Optional

from ..core.logging import get_logger

logger = get_logger(__name__)


class JobRunner:
    """Bounded executor with a cancellation flag per job."""

    def __init__(self, max_workers: int = 2) -> None:
        self._executor = ThreadPoolExecutor(max_workers=max(1, int(max_workers)),
                                            thread_name_prefix="poc-job")
        self._futures: Dict[str, Future] = {}
        self._lock = threading.RLock()

    @property
    def max_workers(self) -> int:
        return int(self._executor._max_workers)  # noqa: SLF001 - read-only introspection

    def submit(self, job_id: str, /, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Future:
        """Run ``fn`` in the pool, remembering the future for cancellation.

        ``job_id`` is positional-only so that a task may itself accept a
        ``job_id`` keyword (the ingest pipeline does) without the two colliding.
        """
        self.cancel(job_id, wait=False)

        def wrapper() -> Any:
            try:
                return fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 - the job records its own failure
                logger.warning("后台任务 %s 失败：%s", job_id, exc)
                return None

        future = self._executor.submit(wrapper)
        with self._lock:
            self._futures[job_id] = future
        future.add_done_callback(lambda _f: self._forget(job_id))
        return future

    def _forget(self, job_id: str) -> None:
        with self._lock:
            self._futures.pop(job_id, None)

    def is_running(self, job_id: str) -> bool:
        with self._lock:
            future = self._futures.get(job_id)
        return bool(future and not future.done())

    def cancel(self, job_id: str, /, wait: bool = False) -> bool:
        """Ask a job to stop.

        Python cannot interrupt a running thread safely, so cancellation is
        cooperative: the pipeline checks the job row between stages (see
        ``IngestPipeline._check_cancelled``). Here we drop it from the pre-start
        queue when it has not begun yet.
        """
        with self._lock:
            future = self._futures.get(job_id)
        if future is None:
            return False
        cancelled = future.cancel()
        if cancelled:
            self._forget(job_id)
        elif wait:
            try:
                future.result(timeout=1.0)
            except Exception:  # noqa: BLE001 - best effort
                pass
        return cancelled

    def active(self) -> Dict[str, bool]:
        with self._lock:
            items = list(self._futures.items())
        return {job_id: not future.done() for job_id, future in items}

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
