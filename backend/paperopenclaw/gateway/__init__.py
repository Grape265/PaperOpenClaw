"""Gateway layer: HTTP/SSE entry point implementing docs/03-api-contract.md."""

from __future__ import annotations

from .app import create_app

__all__ = ["create_app"]
