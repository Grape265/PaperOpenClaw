"""Fishbone graph persistence: 七维鱼骨 (FR-7.1/7.2/7.4).

This is a *separate* store from the Knowledge Graph (see ``repos.kg``). The
fishbone is built from layer-B points: each point becomes one content node with
its full sentence as both ``label`` and ``detail`` — one node per point, no
keyword fan-out, so a sentence can never be duplicated across several nodes.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from ...core.jsonutil import safe_json_loads
from ..db import Database, utc_now


def spine_key(section: str) -> str:
    return f"spine:{section}"


def paper_key(hash_value: str) -> str:
    return f"paper:{hash_value}"


def content_key(hash_value: str, section: str, ordinal: int) -> str:
    return f"content:{hash_value}:{section}:{int(ordinal)}"


def asset_key(hash_value: str, label: str) -> str:
    return f"asset:{hash_value}:{label}"


class FishboneRepository:
    """Reads and writes for ``fishbone_nodes`` and ``fishbone_edges``."""

    def __init__(self, db: Database) -> None:
        self.db = db

    # ==================================================================
    # writes
    # ==================================================================
    def upsert_nodes(self, nodes: Sequence[Dict[str, Any]]) -> int:
        if not nodes:
            return 0
        now = utc_now()
        rows = [
            (
                node["node_key"], node.get("paper_hash") or None, node.get("kind", "content"),
                node.get("section", ""), node.get("label", ""), node.get("detail", ""),
                int(node.get("ordinal", 0)), node.get("asset_id"), node.get("unit_id", ""),
                json.dumps(node.get("meta") or {}, ensure_ascii=False), now,
            )
            for node in nodes
        ]
        with self.db.transaction() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO fishbone_nodes (node_key, paper_hash, kind, section, label, detail, "
                "ordinal, asset_id, unit_id, meta, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
        return len(rows)

    def add_edges(self, edges: Iterable[Tuple[str, str, str, float, Optional[Dict[str, Any]]]]) -> int:
        rows = [
            (src, dst, relation, float(weight),
             json.dumps(meta or {}, ensure_ascii=False), utc_now())
            for src, dst, relation, weight, meta in edges
            if src and dst
        ]
        if not rows:
            return 0
        with self.db.transaction() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO fishbone_edges (src_key, dst_key, relation, weight, meta, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                rows,
            )
        return len(rows)

    def delete_paper(self, paper_hash: str) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "DELETE FROM fishbone_edges WHERE src_key IN (SELECT node_key FROM fishbone_nodes WHERE paper_hash = ?) "
                "OR dst_key IN (SELECT node_key FROM fishbone_nodes WHERE paper_hash = ?)",
                (paper_hash, paper_hash),
            )
            conn.execute("DELETE FROM fishbone_nodes WHERE paper_hash = ?", (paper_hash,))

    @staticmethod
    def _node_row(row: Any) -> Dict[str, Any]:
        item = dict(row)
        item["paper_hash"] = item.get("paper_hash") or ""
        item["meta"] = safe_json_loads(item.get("meta"), {})
        return item

    # ==================================================================
    # queries
    # ==================================================================
    def _node_rows(self, hashes: Optional[Sequence[str]], limit: int) -> List[Dict[str, Any]]:
        clauses: List[str] = []
        params: List[Any] = []
        if hashes:
            placeholders = ",".join("?" for _ in hashes)
            clauses.append(f"(paper_hash IN ({placeholders}) OR kind = 'spine')")
            params.extend(list(hashes))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self.db.query_all(
            f"SELECT * FROM fishbone_nodes{where} ORDER BY kind, paper_hash, ordinal LIMIT ?",
            params + [max(1, int(limit))],
        )
        return [self._node_row(row) for row in rows]

    def fishbone(self, paper_hash: str) -> List[Dict[str, Any]]:
        """Seven spine slots with their content points and asset links."""
        from ...kg.skeleton import SPINE

        nodes = self._node_rows([paper_hash], 5000)
        by_key = {node["node_key"]: node for node in nodes}

        assets_by_content: Dict[str, List[Dict[str, Any]]] = {}
        content_by_section: Dict[str, List[Dict[str, Any]]] = {}
        for node in nodes:
            if node["kind"] == "content" and node["paper_hash"] == paper_hash:
                content_by_section.setdefault(node["section"], []).append(node)

        content_keys = [node["node_key"] for node in nodes if node["kind"] == "content"]
        if content_keys:
            placeholders = ",".join("?" for _ in content_keys)
            rows = self.db.query_all(
                f"SELECT * FROM fishbone_edges WHERE relation = 'has_asset' AND src_key IN ({placeholders})",
                content_keys,
            )
            for row in rows:
                target = by_key.get(row["dst_key"])
                if target is None:
                    continue
                assets_by_content.setdefault(row["src_key"], []).append({
                    "key": target["node_key"],
                    "label": target["label"],
                    "kind": target.get("meta", {}).get("asset_kind", "figure"),
                    "path": target.get("meta", {}).get("path", ""),
                    "url": target.get("meta", {}).get("url", ""),
                    "caption": target.get("detail", ""),
                })

        spine: List[Dict[str, Any]] = []
        for definition in SPINE:
            slug = definition["slug"]
            children = sorted(content_by_section.get(slug, []), key=lambda n: n["ordinal"])
            spine.append({
                "slug": slug,
                "label": definition["label"],
                "order": definition["order"],
                "nodes": [
                    {
                        "key": child["node_key"],
                        "label": child["label"],
                        "detail": child["detail"],
                        "ordinal": child["ordinal"],
                        "unit_id": child.get("unit_id", ""),
                        "assets": assets_by_content.get(child["node_key"], []),
                        "meta": child.get("meta", {}),
                    }
                    for child in children
                ],
            })
        return spine

    def stats(self) -> Dict[str, int]:
        node_count = int(self.db.query_scalar("SELECT COUNT(*) FROM fishbone_nodes", default=0) or 0)
        edge_count = int(self.db.query_scalar("SELECT COUNT(*) FROM fishbone_edges", default=0) or 0)
        return {"nodes": node_count, "edges": edge_count}

    def ensure_spine_nodes(self) -> int:
        """Create the seven shared spine nodes and their ordering edges."""
        from ...kg.skeleton import SPINE

        nodes = []
        for definition in SPINE:
            nodes.append({
                "node_key": spine_key(definition["slug"]),
                "kind": "spine",
                "section": definition["slug"],
                "label": definition["label"],
                "detail": definition.get("description", ""),
                "ordinal": definition["order"],
                "paper_hash": "",
                "meta": {"order": definition["order"], "keywords": definition.get("keywords", [])},
            })
        count = self.upsert_nodes(nodes)

        edges = []
        for previous, current in zip(SPINE, SPINE[1:]):
            edges.append((spine_key(previous["slug"]), spine_key(current["slug"]),
                          "spine_order", 1.0, {"order": current["order"]}))
        self.add_edges(edges)
        return count
