"""Skill registry (FR-9).

The filesystem is authoritative: ``skills/<id>/SKILL.md`` is the manifest.
This table is a cache so the API can list skills without walking the disk on
every request, and so enable/disable state survives a restart.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from ...core.jsonutil import safe_json_loads
from ..db import Database, utc_now


class SkillRepository:
    """Reads and writes for :class:`skills`."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def upsert(
        self,
        skill_id: str,
        *,
        name: str = "",
        description: str = "",
        version: str = "",
        author: str = "",
        path: str = "",
        tools: Optional[List[str]] = None,
        instructions: str = "",
        frontmatter: Optional[Dict[str, Any]] = None,
        enabled: Optional[bool] = None,
        source: str = "",
    ) -> Dict[str, Any]:
        now = utc_now()
        existing = self.get(skill_id)
        if existing is None:
            self.db.execute(
                "INSERT INTO skills (id, name, description, version, author, path, enabled, tools, "
                "instructions, frontmatter, installed_at, updated_at, source) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    skill_id, name or skill_id, description or "", version or "", author or "",
                    path or "", 1 if (enabled is None or enabled) else 0,
                    json.dumps(tools or [], ensure_ascii=False), instructions or "",
                    json.dumps(frontmatter or {}, ensure_ascii=False), now, now, source or "",
                ),
            )
        else:
            self.db.execute(
                "UPDATE skills SET name = ?, description = ?, version = ?, author = ?, path = ?, "
                "enabled = ?, tools = ?, instructions = ?, frontmatter = ?, updated_at = ?, "
                "source = ? WHERE id = ?",
                (
                    name or skill_id, description or "", version or "", author or "", path or "",
                    1 if (enabled if enabled is not None else existing["enabled"]) else 0,
                    json.dumps(tools or [], ensure_ascii=False), instructions or "",
                    json.dumps(frontmatter or {}, ensure_ascii=False), now, source or "", skill_id,
                ),
            )
        return self.get(skill_id) or {}

    def get(self, skill_id: str) -> Optional[Dict[str, Any]]:
        row = self.db.query_one("SELECT * FROM skills WHERE id = ?", (skill_id,))
        return self._row(row) if row else None

    def list(self, enabled_only: bool = False) -> List[Dict[str, Any]]:
        if enabled_only:
            rows = self.db.query_all("SELECT * FROM skills WHERE enabled = 1 ORDER BY name")
        else:
            rows = self.db.query_all("SELECT * FROM skills ORDER BY name")
        return [self._row(row) for row in rows]

    def set_enabled(self, skill_id: str, enabled: bool) -> Optional[Dict[str, Any]]:
        self.db.execute(
            "UPDATE skills SET enabled = ?, updated_at = ? WHERE id = ?",
            (1 if enabled else 0, utc_now(), skill_id),
        )
        return self.get(skill_id)

    def delete(self, skill_id: str) -> bool:
        cursor = self.db.execute("DELETE FROM skills WHERE id = ?", (skill_id,))
        return cursor.rowcount > 0

    def declared_tools(self, enabled_only: bool = True) -> List[str]:
        names: List[str] = []
        for skill in self.list(enabled_only=enabled_only):
            for tool in skill.get("tools", []):
                if tool not in names:
                    names.append(tool)
        return names

    @staticmethod
    def _row(row: Any) -> Dict[str, Any]:
        item = dict(row)
        item["enabled"] = bool(item.get("enabled"))
        item["tools"] = safe_json_loads(item.get("tools"), [])
        item["frontmatter"] = safe_json_loads(item.get("frontmatter"), {})
        return item
