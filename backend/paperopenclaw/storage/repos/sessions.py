"""Sessions, isolated sub-agents, messages and the shared blackboard (FR-10).

Isolation is enforced by *shape*: messages carry an ``agent_id``, each agent
row owns its own tool allowlist and paper scope, and the blackboard is the only
table any agent may read across the session (FR-10.5).
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ...core.jsonutil import safe_json_loads
from ..db import Database, utc_now


class SessionRepository:
    """Persistence for conversations and their agent roster."""

    def __init__(self, db: Database) -> None:
        self.db = db

    # ==================================================================
    # sessions
    # ==================================================================
    def create(
        self,
        session_id: str,
        *,
        kind: str = "qa",
        title: str = "",
        topic: str = "",
        paper_hashes: Sequence[str] = (),
        meta: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        now = utc_now()
        self.db.execute(
            "INSERT INTO sessions (id, kind, title, topic, paper_hashes, status, rounds, "
            "created_at, updated_at, meta) VALUES (?, ?, ?, ?, ?, 'idle', 0, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET title = excluded.title, topic = excluded.topic, "
            "paper_hashes = excluded.paper_hashes, updated_at = excluded.updated_at",
            (
                session_id, kind, title or topic or "", topic or "",
                json.dumps(list(paper_hashes), ensure_ascii=False), now, now,
                json.dumps(meta or {}, ensure_ascii=False),
            ),
        )
        return self.get(session_id) or {}

    def get(self, session_id: str) -> Optional[Dict[str, Any]]:
        row = self.db.query_one("SELECT * FROM sessions WHERE id = ?", (session_id,))
        if not row:
            return None
        item = dict(row)
        item["paper_hashes"] = safe_json_loads(item.get("paper_hashes"), [])
        item["meta"] = safe_json_loads(item.get("meta"), {})
        return item

    def get_or_raise(self, session_id: str) -> Dict[str, Any]:
        from ...core.errors import SessionNotFound

        session = self.get(session_id)
        if session is None:
            raise SessionNotFound(f"会话不存在：{session_id}")
        return session

    def update(self, session_id: str, **fields: Any) -> None:
        allowed = {"title", "topic", "status", "rounds", "meta", "paper_hashes"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return
        for key in ("meta", "paper_hashes"):
            if key in updates and not isinstance(updates[key], str):
                updates[key] = json.dumps(updates[key], ensure_ascii=False)
        updates["updated_at"] = utc_now()
        assignments = ", ".join(f"{key} = ?" for key in updates)
        self.db.execute(
            f"UPDATE sessions SET {assignments} WHERE id = ?",
            list(updates.values()) + [session_id],
        )

    def list(self, kind: str = "", page: int = 1, page_size: int = 20) -> Tuple[List[Dict[str, Any]], int]:
        where = " WHERE kind = ?" if kind else ""
        params: List[Any] = [kind] if kind else []
        total = int(self.db.query_scalar(f"SELECT COUNT(*) FROM sessions{where}", params, 0) or 0)
        page = max(1, int(page or 1))
        page_size = max(1, min(int(page_size or 20), 200))
        rows = self.db.query_all(
            f"SELECT * FROM sessions{where} ORDER BY updated_at DESC LIMIT ? OFFSET ?",
            params + [page_size, (page - 1) * page_size],
        )
        items = []
        for row in rows:
            item = dict(row)
            item["paper_hashes"] = safe_json_loads(item.get("paper_hashes"), [])
            item["meta"] = safe_json_loads(item.get("meta"), {})
            item["message_count"] = int(self.db.query_scalar(
                "SELECT COUNT(*) FROM messages WHERE session_id = ?", (row["id"],), 0) or 0)
            items.append(item)
        return items, total

    def delete(self, session_id: str) -> bool:
        cursor = self.db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        return cursor.rowcount > 0

    # ==================================================================
    # agents
    # ==================================================================
    def add_agent(
        self,
        session_id: str,
        agent_id: str,
        *,
        name: str = "",
        persona: str = "",
        profile_id: str = "",
        provider: str = "",
        model: str = "",
        tools: Sequence[str] = (),
        paper_hashes: Sequence[str] = (),
        workspace: str = "",
        meta: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        self.db.execute(
            "INSERT OR REPLACE INTO agents (id, session_id, name, persona, profile_id, provider, "
            "model, tools, paper_hashes, workspace, status, created_at, meta) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'idle', ?, ?)",
            (
                agent_id, session_id, name or agent_id, persona or "", profile_id or agent_id,
                provider or "", model or "",
                json.dumps(list(tools), ensure_ascii=False),
                json.dumps(list(paper_hashes), ensure_ascii=False),
                workspace or "", utc_now(),
                json.dumps(meta or {}, ensure_ascii=False),
            ),
        )
        return self.get_agent(session_id, agent_id) or {}

    def get_agent(self, session_id: str, agent_id: str) -> Optional[Dict[str, Any]]:
        row = self.db.query_one(
            "SELECT * FROM agents WHERE session_id = ? AND id = ?", (session_id, agent_id)
        )
        return self._agent_row(row) if row else None

    def list_agents(self, session_id: str) -> List[Dict[str, Any]]:
        rows = self.db.query_all(
            "SELECT * FROM agents WHERE session_id = ? ORDER BY created_at, id", (session_id,)
        )
        return [self._agent_row(row) for row in rows]

    def set_agent_status(self, session_id: str, agent_id: str, status: str) -> None:
        self.db.execute(
            "UPDATE agents SET status = ? WHERE session_id = ? AND id = ?",
            (status, session_id, agent_id),
        )

    @staticmethod
    def _agent_row(row: Any) -> Dict[str, Any]:
        item = dict(row)
        item["tools"] = safe_json_loads(item.get("tools"), [])
        item["paper_hashes"] = safe_json_loads(item.get("paper_hashes"), [])
        item["meta"] = safe_json_loads(item.get("meta"), {})
        return item

    # ==================================================================
    # messages
    # ==================================================================
    def add_message(
        self,
        session_id: str,
        *,
        role: str,
        content: str = "",
        agent_id: str = "",
        round_index: int = 0,
        tool_calls: Optional[Sequence[Dict[str, Any]]] = None,
        tool_call_id: str = "",
        tool_name: str = "",
        citations: Optional[Sequence[Dict[str, Any]]] = None,
        usage: Optional[Dict[str, Any]] = None,
    ) -> int:
        cursor = self.db.execute(
            "INSERT INTO messages (session_id, agent_id, round, role, content, tool_calls, "
            "tool_call_id, tool_name, citations, usage, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id, agent_id or "", int(round_index), role, content or "",
                json.dumps(list(tool_calls or []), ensure_ascii=False),
                tool_call_id or "", tool_name or "",
                json.dumps(list(citations or []), ensure_ascii=False),
                json.dumps(usage or {}, ensure_ascii=False), utc_now(),
            ),
        )
        self.db.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (utc_now(), session_id))
        return int(cursor.lastrowid or 0)

    def list_messages(
        self,
        session_id: str,
        agent_id: str = "",
        round_index: Optional[int] = None,
        limit: int = 1000,
    ) -> List[Dict[str, Any]]:
        clauses = ["session_id = ?"]
        params: List[Any] = [session_id]
        if agent_id:
            clauses.append("agent_id = ?")
            params.append(agent_id)
        if round_index is not None:
            clauses.append("round = ?")
            params.append(int(round_index))
        rows = self.db.query_all(
            f"SELECT * FROM messages WHERE {' AND '.join(clauses)} ORDER BY id LIMIT ?",
            params + [int(limit)],
        )
        out = []
        for row in rows:
            item = dict(row)
            item["tool_calls"] = safe_json_loads(item.get("tool_calls"), [])
            item["citations"] = safe_json_loads(item.get("citations"), [])
            item["usage"] = safe_json_loads(item.get("usage"), {})
            out.append(item)
        return out

    def history_for_agent(self, session_id: str, agent_id: str, limit: int = 40) -> List[Dict[str, Any]]:
        """Chat history visible to one agent: only its own turns (FR-10.2)."""
        rows = self.db.query_all(
            "SELECT * FROM messages WHERE session_id = ? AND agent_id = ? "
            "AND role IN ('user','assistant','tool') ORDER BY id DESC LIMIT ?",
            (session_id, agent_id, int(limit)),
        )
        return [dict(row) for row in reversed(rows)]

    # ==================================================================
    # blackboard (the only cross-agent channel)
    # ==================================================================
    def add_blackboard(
        self,
        session_id: str,
        *,
        round_index: int,
        content: str,
        author_agent: str = "",
        entry_type: str = "note",
        citations: Optional[Sequence[Dict[str, Any]]] = None,
    ) -> int:
        cursor = self.db.execute(
            "INSERT INTO blackboard (session_id, round, author_agent, entry_type, content, "
            "citations, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                session_id, int(round_index), author_agent or "", entry_type, content or "",
                json.dumps(list(citations or []), ensure_ascii=False), utc_now(),
            ),
        )
        return int(cursor.lastrowid or 0)

    def list_blackboard(self, session_id: str, round_index: Optional[int] = None) -> List[Dict[str, Any]]:
        if round_index is None:
            rows = self.db.query_all(
                "SELECT * FROM blackboard WHERE session_id = ? ORDER BY round, id", (session_id,)
            )
        else:
            rows = self.db.query_all(
                "SELECT * FROM blackboard WHERE session_id = ? AND round = ? ORDER BY id",
                (session_id, int(round_index)),
            )
        out = []
        for row in rows:
            item = dict(row)
            item["citations"] = safe_json_loads(item.get("citations"), [])
            out.append(item)
        return out

    def count_messages(self, session_id: str) -> int:
        return int(self.db.query_scalar(
            "SELECT COUNT(*) FROM messages WHERE session_id = ?", (session_id,), 0) or 0)
