"""Dependency injection.

Routers depend on the process-wide :class:`Services` container and the job
runner held on ``app.state``. Nothing is constructed per request, which keeps
handlers free of wiring and makes the expensive objects (vector store, embedding
model, provider clients) genuinely shared.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import Request

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..services import Services
    from .runner import JobRunner


def get_services(request: Request) -> "Services":
    return request.app.state.services


def get_runner(request: Request) -> "JobRunner":
    return request.app.state.runner
