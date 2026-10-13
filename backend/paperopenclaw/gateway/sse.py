"""Server-Sent Events plumbing (contract section 11).

SSE is used rather than WebSocket because every stream here is one-directional
(progress, tokens, agent utterances) and HTTP semantics keep proxies and the
desktop shell simple (ADR-9).

Two details decide whether a stream actually feels live:

* **disabling buffering** — ``X-Accel-Buffering: no`` plus ``Cache-Control:
  no-cache`` stops intermediaries from holding the stream until it ends;
* **honouring disconnects** — a client that navigates away must stop the
  generator, otherwise a long brainstorm keeps producing into a dead socket.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator, Callable, Dict, Optional

from fastapi import Request
from fastapi.responses import StreamingResponse

from ..core.events import Subscription

HEARTBEAT_SECONDS = 15.0

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def format_event(name: str, data: Any) -> str:
    payload = json.dumps(data, ensure_ascii=False, default=str)
    return f"event: {name}\ndata: {payload}\n\n"


def sse_response(generator: AsyncIterator[str], *, headers: Optional[Dict[str, str]] = None) -> StreamingResponse:
    merged = dict(SSE_HEADERS)
    if headers:
        merged.update(headers)
    return StreamingResponse(generator, media_type="text/event-stream", headers=merged)


async def from_subscription(
    subscription: Subscription,
    request: Request,
    *,
    terminal_events: tuple = ("done", "error"),
    on_end: Optional[Callable[[], None]] = None,
) -> AsyncIterator[str]:
    """Relay bus events as SSE, with heartbeats and disconnect handling."""
    try:
        while True:
            if await request.is_disconnected():
                break
            event = await subscription.get(timeout=HEARTBEAT_SECONDS)
            if event is None:
                # A comment line keeps the connection warm without emitting an
                # event the client would have to ignore.
                yield ": ping\n\n"
                continue
            yield event.to_sse()
            if event.name in terminal_events:
                break
    finally:
        if on_end is not None:
            on_end()


async def from_events(generator: AsyncIterator[Dict[str, Any]],
                      request: Request) -> AsyncIterator[str]:
    """Relay a generator of ``{"type": ..., ...}`` dicts as SSE.

    ``stop`` marks a terminal frame; a frame whose type is ``stop`` is emitted
    and then the stream closes.
    """
    try:
        async for chunk in generator:
            if await request.is_disconnected():
                break
            name = str(chunk.pop("type", "message"))
            yield format_event(name, chunk)
            if name in ("done", "error"):
                break
    except asyncio.CancelledError:  # pragma: no cover - client went away
        raise
    except Exception as exc:  # noqa: BLE001 - the stream must report, not die
        yield format_event("error", {"code": "INTERNAL_ERROR", "message": str(exc)})
