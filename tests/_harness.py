"""Shared, dependency-free test harness.

Every test module is runnable directly (``python tests/test_xxx.py``) as well as
under pytest. Running directly keeps the project verifiable on a machine where
pytest is unavailable, which matters because the runtime dependencies and the
test dependencies are not the same thing.

The harness also silences library logging: warnings written to stderr make
PowerShell report a non-zero exit code for an otherwise passing run.
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

REPO_ROOT = Path(__file__).resolve().parents[1]


def _use_workspace_temp() -> None:
    """Put test scratch space inside the repository.

    The system temp volume can be small or full — on Windows it is frequently
    both — and a test suite that cannot create a temporary directory reports
    every test as a failure, hiding the real result. Keeping scratch space next
    to the code removes that dependency entirely.
    """
    preferred = os.environ.get("PAPEROPENCLAW_TEST_TMP")
    candidates = [Path(preferred)] if preferred else []
    candidates.append(REPO_ROOT / ".tmp")

    for candidate in candidates:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            probe = candidate / ".probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except Exception:
            continue
        tempfile.tempdir = str(candidate)
        return


_use_workspace_temp()


class TempWorkspace:
    """Temporary directory that cleans up best-effort.

    ``tempfile.TemporaryDirectory`` raises if any file is still held open, and
    on Windows ChromaDB keeps its HNSW index files mapped for the lifetime of
    the process. A failing teardown would mask the real assertion result, so
    cleanup is explicitly best-effort here (``ignore_cleanup_errors`` only
    exists from Python 3.10).
    """

    def __init__(self, prefix: str = "poc_") -> None:
        self.path = Path(tempfile.mkdtemp(prefix=prefix))

    def __enter__(self) -> Path:
        return self.path

    def __exit__(self, *exc: Any) -> None:
        self.cleanup()

    def cleanup(self) -> None:
        shutil.rmtree(str(self.path), ignore_errors=True)

    def __str__(self) -> str:  # pragma: no cover - debugging aid
        return str(self.path)


def quiet_logging(level: int = logging.CRITICAL) -> None:
    logging.disable(level)


class LiveApp:
    """Run an ASGI app on a real ephemeral HTTP port.

    Preferred over ``fastapi.testclient.TestClient`` for two reasons: the pinned
    Starlette/httpx combination is incompatible (Starlette's TestClient passes an
    ``app`` argument that httpx >= 0.28 removed), and testing over a real socket
    exercises exactly what the product depends on — HTTP Range for the PDF
    viewer, and genuinely streamed SSE rather than a buffered in-process call.
    """

    def __init__(self, app: Any, timeout: float = 25.0) -> None:
        self.app = app
        self.timeout = timeout
        self._server = None
        self._thread = None
        self._client = None

    @property
    def base_url(self) -> str:
        if self._client is None:  # pragma: no cover - misuse guard
            raise RuntimeError("LiveApp 尚未启动")
        return str(self._client.base_url).rstrip("/")

    def __enter__(self):
        import threading
        import time
        import uvicorn

        config = uvicorn.Config(
            self.app, host="127.0.0.1", port=0, log_level="error",
            access_log=False, lifespan="on",
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True,
                                        name="poc-test-server")
        self._thread.start()

        deadline = time.time() + self.timeout
        while time.time() < deadline:
            if self._server.started and self._server.servers:
                break
            time.sleep(0.05)
        else:  # pragma: no cover - startup failure
            raise RuntimeError("测试服务器启动超时")

        sockets = getattr(self._server.servers[0], "sockets", None) or []
        if not sockets:  # pragma: no cover - unexpected uvicorn internals
            raise RuntimeError("无法获取测试服务器端口")
        port = sockets[0].getsockname()[1]

        import httpx

        self._client = httpx.Client(base_url=f"http://127.0.0.1:{port}",
                                    timeout=60.0, follow_redirects=True)
        return self._client

    def __exit__(self, *exc: Any) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:  # noqa: BLE001 - teardown must not mask results
                pass
            self._client = None
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=10.0)
            self._thread = None


def collect(namespace: Dict[str, Any]) -> List[Tuple[str, Any]]:
    return [
        (name, obj)
        for name, obj in sorted(namespace.items())
        if name.startswith("test_") and callable(obj)
    ]


def run_all(namespace: Dict[str, Any], verbose: bool = False) -> int:
    """Execute every ``test_*`` callable in ``namespace``; return an exit code."""
    quiet_logging()
    tests = collect(namespace)
    failures: List[str] = []
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
        except Exception as exc:  # noqa: BLE001 - the harness reports everything
            failures.append(name)
            print(f"  FAIL  {name}: {type(exc).__name__}: {exc}")
            if verbose:
                traceback.print_exc()
    passed = len(tests) - len(failures)
    print(f"\n{passed}/{len(tests)} passed")
    if failures:
        print("失败用例：" + "、".join(failures))
    return 1 if failures else 0
