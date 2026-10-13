"""SQLite connection management.

The application is a single-process desktop service, but ingest runs on worker
threads while requests are served on the event loop. sqlite3 connections are
not shareable across threads, so :class:`Database` keeps one connection per
thread and configures WAL so readers never block behind the ingest writer.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence

from ..core.logging import get_logger

logger = get_logger(__name__)

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


def utc_now() -> str:
    """ISO-8601 UTC timestamp with a trailing ``Z`` (OKF-compatible)."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class Database:
    """Thread-local SQLite access with schema bootstrap."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._local = threading.local()
        self._write_lock = threading.RLock()
        self._initialised = False
        self._init_lock = threading.Lock()

    # ------------------------------------------------------------------
    # connection handling
    # ------------------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), timeout=30.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA busy_timeout = 10000")
        conn.execute("PRAGMA temp_store = MEMORY")
        return conn

    @property
    def connection(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._connect()
            self._local.conn = conn
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:  # pragma: no cover - defensive
                pass
            self._local.conn = None

    # ------------------------------------------------------------------
    # schema
    # ------------------------------------------------------------------
    def initialise(self, schema_path: Optional[Path] = None) -> "Database":
        """Create every table. Safe to call repeatedly and from any thread."""
        with self._init_lock:
            if self._initialised:
                return self
            source = Path(schema_path or SCHEMA_PATH)
            sql = source.read_text(encoding="utf-8")
            conn = self.connection
            with self._write_lock:
                conn.executescript(sql)
                conn.commit()
            self._initialised = True
            logger.info("数据库结构就绪：%s", self.path)
            return self

    # ------------------------------------------------------------------
    # query helpers
    # ------------------------------------------------------------------
    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        with self._write_lock:
            cursor = self.connection.execute(sql, params)
            self.connection.commit()
            return cursor

    def executemany(self, sql: str, seq_params: Iterable[Sequence[Any]]) -> None:
        with self._write_lock:
            self.connection.executemany(sql, list(seq_params))
            self.connection.commit()

    def query_all(self, sql: str, params: Sequence[Any] = ()) -> List[sqlite3.Row]:
        return list(self.connection.execute(sql, params).fetchall())

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> Optional[sqlite3.Row]:
        return self.connection.execute(sql, params).fetchone()

    def query_scalar(self, sql: str, params: Sequence[Any] = (), default: Any = None) -> Any:
        row = self.query_one(sql, params)
        if row is None:
            return default
        value = row[0]
        return default if value is None else value

    def count(self, table: str, where: str = "", params: Sequence[Any] = ()) -> int:
        # `table` and `where` are internal constants, never user input.
        sql = f"SELECT COUNT(*) FROM {table}"
        if where:
            sql += f" WHERE {where}"
        return int(self.query_scalar(sql, params, 0) or 0)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Group several statements into one atomic unit."""
        conn = self.connection
        with self._write_lock:
            try:
                conn.execute("BEGIN")
                yield conn
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except Exception:  # pragma: no cover - defensive
                    pass
                raise

    # ------------------------------------------------------------------
    # maintenance
    # ------------------------------------------------------------------
    def vacuum(self) -> None:
        with self._write_lock:
            self.connection.execute("VACUUM")
            self.connection.commit()

    def table_counts(self) -> Dict[str, int]:
        tables = (
            "papers", "authors", "affiliations", "assets", "okf_concepts",
            "rag_units", "rag_links", "kg_nodes", "kg_edges", "skills",
            "sessions", "messages", "jobs", "blackboard",
        )
        return {name: self.count(name) for name in tables}

    def integrity_check(self) -> str:
        return str(self.query_scalar("PRAGMA integrity_check", default="unknown"))
