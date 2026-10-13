"""In-process event bus bridging worker threads to SSE subscribers.

Ingest runs in a worker thread (it is CPU/IO heavy and calls out to blocking
HTTP clients), while SSE consumers live on the asyncio event loop. The bus is
the single crossing point: :meth:`EventBus.publish` is safe from any thread and
marshals into the captured loop.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional

logger = logging.getLogger(__name__)

REPLAY_LIMIT = 200


class Event:
    """A single published event."""

    __slots__ = ("topic", "name", "data", "seq", "ts")

    def __init__(self, topic: str, name: str, data: Dict[str, Any], seq: int) -> None:
        self.topic = topic
        self.name = name
        self.data = data
        self.seq = seq
        self.ts = time.time()

    def to_sse(self) -> str:
        import json

        payload = json.dumps(self.data, ensure_ascii=False, default=str)
        return f"event: {self.name}\ndata: {payload}\n\n"

    def to_dict(self) -> Dict[str, Any]:
        return {"topic": self.topic, "name": self.name, "data": self.data, "seq": self.seq, "ts": self.ts}


class Subscription:
    """An asyncio-queue backed subscription, used as an async iterator."""

    def __init__(self, bus: "EventBus", topic: str, backlog: int = 0) -> None:
        self._bus = bus
        self.topic = topic
        self.queue: "asyncio.Queue[Optional[Event]]" = asyncio.Queue()
        if backlog:
            for event in bus.history(topic, backlog):
                self.queue.put_nowait(event)

    def __aiter__(self) -> "Subscription":
        return self

    async def __anext__(self) -> Event:
        item = await self.queue.get()
        if item is None:
            raise StopAsyncIteration
        return item

    async def get(self, timeout: Optional[float] = None) -> Optional[Event]:
        if timeout is None:
            return await self.queue.get()
        try:
            return await asyncio.wait_for(self.queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None

    def close(self) -> None:
        try:
            self.queue.put_nowait(None)
        except Exception:  # pragma: no cover - defensive
            pass


class EventBus:
    """Topic-based pub/sub with a bounded replay buffer."""

    def __init__(self) -> None:
        self._subscribers: Dict[str, List[Subscription]] = {}
        self._history: Dict[str, Deque[Event]] = {}
        self._lock = threading.RLock()
        self._seq = 0
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    # ------------------------------------------------------------------
    # loop binding
    # ------------------------------------------------------------------
    def bind_loop(self, loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
        """Capture the event loop that SSE consumers run on."""
        if loop is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
        self._loop = loop

    # ------------------------------------------------------------------
    # publish
    # ------------------------------------------------------------------
    def publish(self, topic: str, name: str, data: Optional[Dict[str, Any]] = None) -> Event:
        """Publish an event; safe to call from any thread."""
        with self._lock:
            self._seq += 1
            event = Event(topic, name, data or {}, self._seq)
            bucket = self._history.setdefault(topic, deque(maxlen=REPLAY_LIMIT))
            bucket.append(event)
            targets = list(self._subscribers.get(topic, ()))

        if not targets:
            return event

        loop = self._loop
        if loop is not None and loop.is_running():
            def _deliver() -> None:
                for sub in targets:
                    try:
                        sub.queue.put_nowait(event)
                    except Exception:  # pragma: no cover - defensive
                        pass

            try:
                loop.call_soon_threadsafe(_deliver)
                return event
            except RuntimeError:  # pragma: no cover - loop closed mid-flight
                pass

        for sub in targets:
            try:
                sub.queue.put_nowait(event)
            except Exception:  # pragma: no cover - defensive
                pass
        return event

    def close_topic(self, topic: str) -> None:
        """Signal end-of-stream to every subscriber of ``topic``."""
        with self._lock:
            targets = list(self._subscribers.pop(topic, ()))
        for sub in targets:
            sub.close()

    # ------------------------------------------------------------------
    # subscribe
    # ------------------------------------------------------------------
    def subscribe(self, topic: str, backlog: int = 0) -> Subscription:
        sub = Subscription(self, topic, backlog=backlog)
        with self._lock:
            self._subscribers.setdefault(topic, []).append(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        with self._lock:
            bucket = self._subscribers.get(sub.topic)
            if bucket and sub in bucket:
                bucket.remove(sub)
            if bucket is not None and not bucket:
                self._subscribers.pop(sub.topic, None)

    # ------------------------------------------------------------------
    # history
    # ------------------------------------------------------------------
    def history(self, topic: str, limit: int = REPLAY_LIMIT) -> List[Event]:
        with self._lock:
            bucket = self._history.get(topic)
            if not bucket:
                return []
            return list(bucket)[-limit:]

    def clear(self, topic: Optional[str] = None) -> None:
        with self._lock:
            if topic is None:
                self._history.clear()
                self._subscribers.clear()
            else:
                self._history.pop(topic, None)
                self._subscribers.pop(topic, None)

    def subscriber_count(self, topic: str) -> int:
        with self._lock:
            return len(self._subscribers.get(topic, ()))


# Topics used across the application.
TOPIC_JOB = "job:{job_id}"
TOPIC_SESSION = "session:{session_id}"


def job_topic(job_id: str) -> str:
    return TOPIC_JOB.format(job_id=job_id)


def session_topic(session_id: str) -> str:
    return TOPIC_SESSION.format(session_id=session_id)


# Process-wide bus. A single instance keeps SSE wiring trivial; the app is a
# single-process desktop service (NFR-1), so no cross-process fan-out is needed.
bus = EventBus()
