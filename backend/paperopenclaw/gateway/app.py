"""Application factory: middleware, error handling, routers and static serving.

One process serves both the API and the built frontend, which is what makes the
desktop story simple: ``python -m paperopenclaw serve`` opens a browser at one
URL and there is nothing else to run (NFR-1).
"""

from __future__ import annotations

import os
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Dict, Optional

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from .. import __version__
from ..config import bootstrap_settings
from ..core.errors import AppError
from ..core.events import bus
from ..core.logging import get_logger, setup_logging
from ..services import Services
from .responses import envelope_handler, error_response, failure
from .router_registry import API_PREFIX, include_routers
from .runner import JobRunner

logger = get_logger(__name__)

WEB_DIST_CANDIDATES = (
    Path(__file__).resolve().parents[3] / "web" / "dist",
    Path.cwd() / "web" / "dist",
)


def _web_dist() -> Optional[Path]:
    candidates = []
    from_env = os.environ.get("PAPEROPENCLAW_WEB_DIST")
    if from_env:
        candidates.append(Path(from_env))
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        candidates.append(Path(sys._MEIPASS) / "web" / "dist")
    candidates.extend(WEB_DIST_CANDIDATES)
    for candidate in candidates:
        if (candidate / "index.html").is_file():
            return candidate
    return None


def create_app(data_dir: Optional[str] = None, *, services: Optional[Services] = None,
               enable_docs: bool = True) -> FastAPI:
    """Build the FastAPI application."""
    if services is None:
        # Bootstrapping the data directory also redirects `tempfile.tempdir` into
        # it (see core/paths.py). Only do that when we actually own the container;
        # when one is injected we must not bootstrap a *second* data directory,
        # which would leak a global tempdir redirect into whoever is embedding us.
        paths, store = bootstrap_settings(data_dir)
        setup_logging(log_dir=paths.logs_dir)
        services = Services(paths, store)
    else:
        paths = services.paths
        store = services.settings
        setup_logging(log_dir=paths.logs_dir)

    runner = JobRunner(max_workers=max(1, store.current.ingest.max_concurrent_jobs))

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # The event bus needs the loop SSE consumers run on so worker threads
        # can marshal events into it (see core/events.py).
        import asyncio

        bus.bind_loop(asyncio.get_running_loop())
        logger.info("PaperOpenClaw %s 已启动，数据目录：%s", __version__, paths.root)
        try:
            services.skill_loader.scan(register_tools=True, registry=services.agents.registry)
        except Exception as exc:  # noqa: BLE001 - skills are optional
            logger.warning("启动时装载技能失败：%s", exc)
        try:
            yield
        finally:
            runner.shutdown()
            services.close()
            logger.info("PaperOpenClaw 已停止")

    app = FastAPI(
        title="PaperOpenClaw",
        version=__version__,
        description="基于 OpenClaw 式智能体架构的论文知识库：OKF 存储、金字塔 RAG、鱼骨知识图谱。",
        docs_url="/docs" if enable_docs else None,
        redoc_url="/redoc" if enable_docs else None,
        openapi_url="/openapi.json" if enable_docs else None,
        lifespan=lifespan,
    )
    app.state.services = services
    app.state.runner = runner
    app.state.paths = paths

    # ---- CORS ---------------------------------------------------------
    origins = store.current.server.cors_origins or ["*"]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["Content-Range", "Content-Length", "Accept-Ranges", "Content-Disposition"],
    )

    # ---- request id + timing -----------------------------------------
    @app.middleware("http")
    async def add_request_context(request: Request, call_next):
        from .responses import request_id

        request.state.request_id = request_id(request)
        started = time.monotonic()
        response = await call_next(request)
        response.headers["X-Request-Id"] = request.state.request_id
        response.headers["X-Elapsed-Ms"] = str(int((time.monotonic() - started) * 1000))
        return response

    # ---- optional shared-secret auth ---------------------------------
    @app.middleware("http")
    async def enforce_token(request: Request, call_next):
        token = store.current.server.auth_token
        if token and request.url.path.startswith(API_PREFIX):
            supplied = request.headers.get("x-auth-token") or ""
            if not supplied:
                header = request.headers.get("authorization") or ""
                if header.lower().startswith("bearer "):
                    supplied = header[7:]
            if supplied != token:
                return JSONResponse(
                    status_code=401,
                    content=failure("UNAUTHORIZED", "访问令牌缺失或错误", request),
                )
        return await call_next(request)

    # ---- error handling ----------------------------------------------
    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        if exc.http_status >= 500:
            logger.exception("请求处理失败：%s", exc.message)
        return error_response(exc, request)

    @app.exception_handler(RequestValidationError)
    async def handle_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        details = [
            {"loc": ".".join(str(part) for part in item.get("loc", [])),
             "message": item.get("msg", "")}
            for item in exc.errors()[:12]
        ]
        return JSONResponse(
            status_code=400,
            content=failure("BAD_REQUEST", "请求参数校验失败", request,
                            {"fields": details}),
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {401: "UNAUTHORIZED", 403: "UNAUTHORIZED", 404: "NOT_FOUND",
                405: "BAD_REQUEST", 409: "DUPLICATE_PAPER"}.get(exc.status_code, "INTERNAL_ERROR")
        if exc.status_code < 500:
            code = {404: "NOT_FOUND", 405: "BAD_REQUEST"}.get(exc.status_code, "BAD_REQUEST")
        return JSONResponse(
            status_code=exc.status_code,
            content=failure(code, str(exc.detail or ""), request),
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        return envelope_handler(exc, request)

    # ---- API routes ---------------------------------------------------
    include_routers(app)

    # ---- frontend -----------------------------------------------------
    dist = _web_dist()
    if dist is not None:
        assets = dist / "assets"
        if assets.is_dir():
            app.mount("/assets", StaticFiles(directory=str(assets)), name="assets")

        @app.get("/", include_in_schema=False)
        async def index() -> FileResponse:
            return FileResponse(dist / "index.html")

        @app.get("/{full_path:path}", include_in_schema=False)
        async def spa(full_path: str):
            # Serve real files directly, everything else falls through to the
            # SPA shell so client-side routes survive a hard refresh.
            if full_path.startswith(("api/", "docs", "redoc", "openapi.json")):
                return JSONResponse(
                    status_code=404,
                    content={"ok": False, "data": None,
                             "error": {"code": "NOT_FOUND", "message": "接口不存在"},
                             "meta": {}},
                )
            candidate = (dist / full_path).resolve()
            if dist in candidate.parents and candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(dist / "index.html")
    else:
        @app.get("/", include_in_schema=False)
        async def placeholder() -> Dict[str, Any]:
            return {
                "ok": True,
                "data": {
                    "name": "PaperOpenClaw",
                    "version": __version__,
                    "message": "前端尚未构建。请在 web/ 目录执行 npm install && npm run build，"
                               "或使用 npm run dev 进行开发。",
                    "api": API_PREFIX,
                    "docs": "/docs",
                },
                "error": None,
                "meta": {},
            }

    logger.info("应用已创建：数据目录 %s，前端 %s", paths.root, dist or "未构建")
    return app


def app_factory() -> FastAPI:
    """Entry point for ``uvicorn paperopenclaw.gateway.app:app_factory --factory``."""
    return create_app()
