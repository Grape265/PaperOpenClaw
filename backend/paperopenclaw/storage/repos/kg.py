"""Knowledge graph persistence: keyword graph from layer A (FR-7.1 重设计).

This is a *separate* store from the Fishbone Graph (see ``repos.fishbone``).
The knowledge graph is the floating "bubble" graph: paper nodes plus keyword
nodes extracted from layer-A detailed digests, joined by ``has_keyword`` edges.
It is what the Obsidian-style graph page renders.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from ...core.jsonutil import safe_json_loads
from ..db import Database, utc_now

NODE_KINDS = ("paper", "keyword")
EDGE_RELATIONS = ("has_keyword",)


def paper_key(hash_value: str) -> str:
    return f"paper:{hash_value}"


def keyword_key(keyword: str) -> str:
    # A keyword node is *global* (shared across papers) so two papers that share a
    # keyword are connected through it — the Obsidian-style "tag" topology.
    return f"keyword:{keyword}"


class KnowledgeGraphRepository:
    """Reads and writes for ``kg_nodes`` and ``kg_edges`` (keyword graph)."""

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
                node["node_key"], node.get("paper_hash") or None, node.get("kind", "keyword"),
                node.get("section", ""), node.get("label", ""), node.get("detail", ""),
                int(node.get("ordinal", 0)), None, node.get("unit_id", ""),
                json.dumps(node.get("meta") or {}, ensure_ascii=False), now,
            )
            for node in nodes
        ]
        with self.db.transaction() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO kg_nodes (node_key, paper_hash, kind, section, label, detail, "
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
                "INSERT OR REPLACE INTO kg_edges (src_key, dst_key, relation, weight, meta, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                rows,
            )
        return len(rows)

    def delete_paper(self, paper_hash: str) -> None:
        """Remove a paper node, its edges, and any keyword orphaned by the removal."""
        key = paper_key(paper_hash)
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM kg_edges WHERE src_key = ?", (key,))
            conn.execute("DELETE FROM kg_nodes WHERE node_key = ?", (key,))
            # Drop global keyword nodes that no paper references any more.
            conn.execute(
                "DELETE FROM kg_nodes WHERE kind = 'keyword' AND node_key NOT IN "
                "(SELECT dst_key FROM kg_edges WHERE relation = 'has_keyword')"
            )

    def delete_all_keywords(self) -> None:
        """Clear every node/edge so a full rebuild starts from a clean slate."""
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM kg_edges")
            conn.execute("DELETE FROM kg_nodes")

    @staticmethod
    def _node_row(row: Any) -> Dict[str, Any]:
        item = dict(row)
        item["paper_hash"] = item.get("paper_hash") or ""
        item["meta"] = safe_json_loads(item.get("meta"), {})
        return item

    # ==================================================================
    # queries
    # ==================================================================
    def subgraph(
        self,
        hashes: Optional[Sequence[str]] = None,
        sections: Optional[Sequence[str]] = None,
        kinds: Optional[Sequence[str]] = None,
        limit: int = 5000,
    ) -> Dict[str, Any]:
        """Return paper + keyword nodes and the ``has_keyword`` edges joining them.

        Keyword nodes are global, so a hash-filtered subgraph reaches the keywords
        through the edges rather than through ``paper_hash`` on the keyword row.
        """
        if hashes:
            placeholders = ",".join("?" for _ in hashes)
            paper_rows = self.db.query_all(
                f"SELECT * FROM kg_nodes WHERE kind = 'paper' AND paper_hash IN ({placeholders})",
                list(hashes),
            )
            paper_keys = [row["node_key"] for row in paper_rows]
            if not paper_keys:
                return {"nodes": [], "edges": []}
            key_ph = ",".join("?" for _ in paper_keys)
            edge_rows = self.db.query_all(
                f"SELECT * FROM kg_edges WHERE src_key IN ({key_ph})", paper_keys,
            )
            keyword_keys = sorted({
                row["dst_key"] for row in edge_rows if row["relation"] == "has_keyword"
            })
            keyword_rows = []
            if keyword_keys:
                kw_ph = ",".join("?" for _ in keyword_keys)
                keyword_rows = self.db.query_all(
                    f"SELECT * FROM kg_nodes WHERE node_key IN ({kw_ph})", keyword_keys,
                )
            nodes = [self._node_row(row) for row in paper_rows + keyword_rows]
            edges = []
            for row in edge_rows:
                item = dict(row)
                item["meta"] = safe_json_loads(item.get("meta"), {})
                edges.append(item)
            return {"nodes": nodes, "edges": edges}

        # No filter: the whole graph.
        rows = self.db.query_all(
            f"SELECT * FROM kg_nodes ORDER BY kind, paper_hash, ordinal LIMIT ?",
            [max(1, int(limit))],
        )
        nodes = [self._node_row(row) for row in rows]
        keys = {node["node_key"] for node in nodes}
        if not keys:
            return {"nodes": [], "edges": []}
        edges: List[Dict[str, Any]] = []
        key_list = list(keys)
        chunk = 400
        for start in range(0, len(key_list), chunk):
            batch = key_list[start : start + chunk]
            placeholders = ",".join("?" for _ in batch)
            edge_rows = self.db.query_all(
                f"SELECT * FROM kg_edges WHERE src_key IN ({placeholders}) AND dst_key IN ({placeholders})",
                batch + batch,
            )
            for row in edge_rows:
                item = dict(row)
                item["meta"] = safe_json_loads(item.get("meta"), {})
                edges.append(item)
        return {"nodes": nodes, "edges": edges}

    def search(
        self,
        query: str,
        hashes: Optional[Sequence[str]] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        like = f"%{(query or '').strip()}%"
        rows = self.db.query_all(
            "SELECT * FROM kg_nodes WHERE (label LIKE ? OR detail LIKE ?) ORDER BY kind, ordinal LIMIT ?",
            [like, like, max(1, int(limit))],
        )
        return [self._node_row(row) for row in rows]

    def stats(self) -> Dict[str, int]:
        node_count = int(self.db.query_scalar("SELECT COUNT(*) FROM kg_nodes", default=0) or 0)
        edge_count = int(self.db.query_scalar("SELECT COUNT(*) FROM kg_edges", default=0) or 0)
        return {"nodes": node_count, "edges": edge_count}
