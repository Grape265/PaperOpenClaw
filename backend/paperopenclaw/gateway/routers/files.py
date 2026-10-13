"""Generic file access inside the data directory.

Every path is resolved through :meth:`DataPaths.resolve`, which refuses anything
escaping the data root, so a crafted ``path`` parameter cannot read arbitrary
files (``PATH_OUT_OF_SCOPE``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from ...core.errors import PathOutOfScope
from ...services import Services
from ..deps import get_services
from ..filestream import stream_file
from ..responses import success

router = APIRouter(tags=["files"])


@router.get("/files/download")
def download(request: Request, path: str,
             services: Services = Depends(get_services)) -> Response:
    target = services.paths.resolve(path)
    return stream_file(target, request, filename=target.name, inline=False)


@router.get("/files/raw")
def raw(request: Request, path: str,
        services: Services = Depends(get_services)) -> Response:
    target = services.paths.resolve(path)
    return stream_file(target, request, inline=True)


@router.get("/files/list")
def list_directory(request: Request, path: str = "",
                   services: Services = Depends(get_services)) -> Dict[str, Any]:
    """Directory listing used by the OKF browser's fallback view."""
    target = services.paths.resolve(path) if path else services.paths.root
    if not target.is_dir():
        raise PathOutOfScope(f"不是目录：{path}")
    entries = []
    for child in sorted(target.iterdir(), key=lambda item: (item.is_file(), item.name)):
        entries.append({
            "name": child.name,
            "is_dir": child.is_dir(),
            "size": child.stat().st_size if child.is_file() else 0,
            "path": services.paths.to_relative(child),
        })
    return success({"path": services.paths.to_relative(target), "entries": entries}, request)
