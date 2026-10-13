"""Uniform response envelope (contract section 0.1).

Every JSON endpoint answers with ``{ok, data, error, meta}``. Centralising the
shape here means a router never hand-builds an error payload, and the frontend
can branch on ``ok`` and switch on ``error.code`` instead of parsing messages.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, Optional

from fastapi import Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from ..core.errors import AppError


def request_id(request: Optional[Request] = None) -> str:
    if request is None:
        return uuid.uuid4().hex[:16]
    existing = getattr(request.state, "request_id", None)
    if existing:
        return str(existing)
    generated = uuid.uuid4().hex[:16]
    try:
        request.state.request_id = generated
    except Exception:  # pragma: no cover - defensive
        pass
    return generated


def success(data: Any = None, request: Optional[Request] = None,
            meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "ok": True,
        "data": jsonable_encoder(data),
        "error": None,
        "meta": {"request_id": request_id(request), **(meta or {})},
    }
    return payload


def failure(code: str, message: str, request: Optional[Request] = None,
            details: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    error: Dict[str, Any] = {"code": code, "message": message}
    if details:
        error["details"] = jsonable_encoder(details)
    return {
        "ok": False,
        "data": None,
        "error": error,
        "meta": {"request_id": request_id(request)},
    }


def error_response(exc: AppError, request: Optional[Request] = None) -> JSONResponse:
    return JSONResponse(
        status_code=exc.http_status,
        content=failure(exc.code, exc.message, request, exc.details),
    )


def envelope_handler(exc: Exception, request: Request) -> JSONResponse:
    """Fallback handler: never leak a stack trace to the client."""
    from ..core.logging import get_logger

    logger = get_logger(__name__)
    logger.exception("未处理的请求异常：%s", exc)
    return JSONResponse(
        status_code=500,
        content=failure("INTERNAL_ERROR", f"{type(exc).__name__}: {exc}", request),
    )
