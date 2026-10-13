"""RAG pyramid persistence: semantic units plus the links that make it a pyramid.

Units are identified by a stable, meaningful id so that re-running a stage is
idempotent and so the vector store (keyed by the same id) stays in lockstep
with this table (FR-13.2).

    c:<hash>              one abstract per paper          (apex of the pyramid)
    b:<hash>:<section>    one per fishbone dimension      (middle)
    a:<hash>:<ordinal>    one per source section          (base)
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from ...core.hashing import stable_id
from ...core.jsonutil import safe_json_loads
from ...core.text import word_count
from ..db import Database, utc_now

LAYERS = ("A", "B", "C")
RELATIONS = ("a_to_b", "b_to_c", "c_to_c", "a_to_c")


def unit_id_a(hash_value: str, ordinal: int) -> str:
    return f"a:{hash_value}:{int(ordinal)}"


def unit_id_b(hash_value: str, section: str) -> str:
    return f"b:{hash_value}:{section}"


def unit_id_c(hash_value: str) -> str:
    return f"c:{hash_value}"


def parse_unit_id(unit_id: str) -> Tuple[str, str, str]:
    """Split ``a:<hash>:<rest>`` into ``(layer, hash, rest)``."""
    parts = str(unit_id).split(":", 2)
    if len(parts) < 2:
        return "", "", ""
    layer = parts[0].upper()
    hash_value = parts[1]
    rest = parts[2] if len(parts) > 2 else ""
    return layer, hash_value, rest


class RagRepository:
    """Reads and writes for :class:`rag_units` and :class:`rag_links`."""

    def __init__(self, db: Database) -> None:
        self.db = db

    # ==================================================================
    # units
    # ==================================================================
    def upsert_unit(
        self,
        unit_id: str,
        *,
        layer: str,
        paper_hash: str,
        text: str,
        ordinal: int = 0,
        title: str = "",
        section: str = "",
        source_ref: str = "",
        meta: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.db.execute(
            """
            INSERT INTO rag_units (id, layer, paper_hash, ordinal, title, text, section,
                                   source_ref, meta, char_count, token_count, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                layer = excluded.layer, paper_hash = excluded.paper_hash,
                ordinal = excluded.ordinal, title = excluded.title, text = excluded.text,
                section = excluded.section, source_ref = excluded.source_ref,
                meta = excluded.meta,
                char_count = excluded.char_count, token_count = excluded.token_count
            """,
            (
                unit_id, layer.upper(), paper_hash, int(ordinal), title or "", text or "",
                section or "", source_ref or "",
                json.dumps(meta or {}, ensure_ascii=False),
                len(text or ""), word_count(text or ""), utc_now(),
            ),
        )

    def replace_units(self, paper_hash: str, units: Sequence[Dict[str, Any]]) -> None:
        """Replace every unit and link belonging to one paper, atomically."""
        with self.db.transaction() as conn:
            conn.execute(
                "DELETE FROM rag_links WHERE src_unit IN (SELECT id FROM rag_units WHERE paper_hash = ?) "
                "OR dst_unit IN (SELECT id FROM rag_units WHERE paper_hash = ?)",
                (paper_hash, paper_hash),
            )
            conn.execute("DELETE FROM rag_units WHERE paper_hash = ?", (paper_hash,))
            now = utc_now()
            for unit in units:
                conn.execute(
                    "INSERT OR REPLACE INTO rag_units (id, layer, paper_hash, ordinal, title, text, "
                    "section, source_ref, meta, char_count, token_count, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        unit["id"], unit["layer"].upper(), paper_hash, int(unit.get("ordinal", 0)),
                        unit.get("title", "") or "", unit.get("text", "") or "",
                        unit.get("section", "") or "", unit.get("source_ref", "") or "",
                        json.dumps(unit.get("meta") or {}, ensure_ascii=False),
                        len(unit.get("text", "") or ""), word_count(unit.get("text", "") or ""), now,
                    ),
                )

    def get_unit(self, unit_id: str) -> Optional[Dict[str, Any]]:
        row = self.db.query_one("SELECT * FROM rag_units WHERE id = ?", (unit_id,))
        return self._unit_row(row) if row else None

    def get_units(self, unit_ids: Sequence[str]) -> List[Dict[str, Any]]:
        if not unit_ids:
            return []
        placeholders = ",".join("?" for _ in unit_ids)
        rows = self.db.query_all(
            f"SELECT * FROM rag_units WHERE id IN ({placeholders})", list(unit_ids)
        )
        by_id = {row["id"]: self._unit_row(row) for row in rows}
        return [by_id[uid] for uid in unit_ids if uid in by_id]

    def units_of_paper(self, paper_hash: str, layer: str = "") -> List[Dict[str, Any]]:
        if layer:
            rows = self.db.query_all(
                "SELECT * FROM rag_units WHERE paper_hash = ? AND layer = ? ORDER BY ordinal",
                (paper_hash, layer.upper()),
            )
        else:
            rows = self.db.query_all(
                "SELECT * FROM rag_units WHERE paper_hash = ? "
                "ORDER BY CASE layer WHEN 'C' THEN 0 WHEN 'B' THEN 1 ELSE 2 END, ordinal",
                (paper_hash,),
            )
        return [self._unit_row(row) for row in rows]

    def units_of_layer(self, layer: str, hashes: Optional[Sequence[str]] = None) -> List[Dict[str, Any]]:
        if hashes:
            placeholders = ",".join("?" for _ in hashes)
            rows = self.db.query_all(
                f"SELECT * FROM rag_units WHERE layer = ? AND paper_hash IN ({placeholders})",
                [layer.upper()] + list(hashes),
            )
        else:
            rows = self.db.query_all("SELECT * FROM rag_units WHERE layer = ?", (layer.upper(),))
        return [self._unit_row(row) for row in rows]

    def unit_lookup(self, hashes: Optional[Sequence[str]] = None,
                    layers: Optional[Sequence[str]] = None) -> Dict[str, Dict[str, Any]]:
        """Map unit id -> unit for the subset the retriever should consider."""
        clauses: List[str] = []
        params: List[Any] = []
        if layers:
            clauses.append("layer IN (" + ",".join("?" for _ in layers) + ")")
            params.extend([layer.upper() for layer in layers])
        if hashes:
            clauses.append("paper_hash IN (" + ",".join("?" for _ in hashes) + ")")
            params.extend(list(hashes))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self.db.query_all(f"SELECT * FROM rag_units{where}", params)
        return {row["id"]: self._unit_row(row) for row in rows}

    @staticmethod
    def _unit_row(row: Any) -> Dict[str, Any]:
        item = dict(row)
        item["meta"] = safe_json_loads(item.get("meta"), {})
        return item

    def delete_units_of_paper(self, paper_hash: str) -> None:
        self.db.execute("DELETE FROM rag_units WHERE paper_hash = ?", (paper_hash,))

    def count_by_layer(self) -> Dict[str, int]:
        rows = self.db.query_all("SELECT layer, COUNT(*) AS n FROM rag_units GROUP BY layer")
        return {row["layer"]: int(row["n"]) for row in rows}

    # ==================================================================
    # links
    # ==================================================================
    def add_link(self, src_unit: str, dst_unit: str, relation: str,
                 weight: float = 1.0, meta: Optional[Dict[str, Any]] = None) -> None:
        if src_unit == dst_unit:
            return
        self.db.execute(
            "INSERT OR REPLACE INTO rag_links (src_unit, dst_unit, relation, weight, meta, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (src_unit, dst_unit, relation, float(weight),
             json.dumps(meta or {}, ensure_ascii=False), utc_now()),
        )

    def add_links(self, links: Iterable[Tuple[str, str, str, float, Optional[Dict[str, Any]]]]) -> int:
        now = utc_now()
        rows = [
            (src, dst, relation, float(weight),
             json.dumps(meta or {}, ensure_ascii=False), now)
            for src, dst, relation, weight, meta in links
            if src and dst and src != dst
        ]
        if not rows:
            return 0
        with self.db.transaction() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO rag_links (src_unit, dst_unit, relation, weight, meta, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                rows,
            )
        return len(rows)

    def parents(self, unit_id: str, relation: str = "") -> List[Dict[str, Any]]:
        """Units one layer *up* (more condensed) from ``unit_id``.

        Links always point from the more detailed layer to the more condensed
        one (``a_to_b``: A->B, ``b_to_c``: B->C), so the parent of a unit is the
        *destination* of the links it originates.
        """
        clauses = ["l.src_unit = ?"]
        params: List[Any] = [unit_id]
        if relation:
            clauses.append("l.relation = ?")
            params.append(relation)
        rows = self.db.query_all(
            "SELECT l.*, u.layer AS dst_layer, u.paper_hash AS dst_hash, u.title AS dst_title, "
            "u.section AS dst_section, u.text AS dst_text "
            f"FROM rag_links l JOIN rag_units u ON u.id = l.dst_unit "
            f"WHERE {' AND '.join(clauses)} ORDER BY l.weight DESC",
            params,
        )
        return [self._link_row(row, prefix="dst") for row in rows]

    def children(self, unit_id: str, relation: str = "") -> List[Dict[str, Any]]:
        """Units one layer *down* (more detailed) from ``unit_id``."""
        clauses = ["l.dst_unit = ?"]
        params: List[Any] = [unit_id]
        if relation:
            clauses.append("l.relation = ?")
            params.append(relation)
        rows = self.db.query_all(
            "SELECT l.*, u.layer AS src_layer, u.paper_hash AS src_hash, u.title AS src_title, "
            "u.section AS src_section, u.text AS src_text "
            f"FROM rag_links l JOIN rag_units u ON u.id = l.src_unit "
            f"WHERE {' AND '.join(clauses)} ORDER BY l.weight DESC",
            params,
        )
        return [self._link_row(row, prefix="src") for row in rows]

    def parent_ids(self, unit_ids: Sequence[str], relation: str) -> List[str]:
        """Ids one layer up; see :meth:`parents` for the direction convention."""
        return self.expand(unit_ids, relation)

    def child_ids(self, unit_ids: Sequence[str], relation: str) -> List[str]:
        """Ids one layer down; see :meth:`children` for the direction convention."""
        return self.expand_reverse(unit_ids, relation)

    def expand(self, unit_ids: Sequence[str], relation: str) -> List[str]:
        """Return ``dst_unit`` for links starting at any of ``unit_ids``."""
        if not unit_ids:
            return []
        placeholders = ",".join("?" for _ in unit_ids)
        rows = self.db.query_all(
            f"SELECT DISTINCT dst_unit FROM rag_links WHERE src_unit IN ({placeholders}) AND relation = ?",
            list(unit_ids) + [relation],
        )
        return [row["dst_unit"] for row in rows]

    def expand_reverse(self, unit_ids: Sequence[str], relation: str) -> List[str]:
        """Return ``src_unit`` for links ending at any of ``unit_ids``."""
        if not unit_ids:
            return []
        placeholders = ",".join("?" for _ in unit_ids)
        rows = self.db.query_all(
            f"SELECT DISTINCT src_unit FROM rag_links WHERE dst_unit IN ({placeholders}) AND relation = ?",
            list(unit_ids) + [relation],
        )
        return [row["src_unit"] for row in rows]

    def cross_links(self, unit_id: str) -> List[Dict[str, Any]]:
        """C-C links in either direction, for the cross-paper view (FR-6.6).

        A pair of papers is linked in both directions for symmetric scoring, so
        the two rows are collapsed into one neighbour entry keyed by the other
        unit; the strongest weight wins.
        """
        rows = self.db.query_all(
            "SELECT l.src_unit, l.dst_unit, l.weight, l.meta, "
            "       u.id AS other_unit, u.paper_hash AS other_hash, u.title AS other_title "
            "FROM rag_links l JOIN rag_units u "
            "  ON u.id = CASE WHEN l.src_unit = ? THEN l.dst_unit ELSE l.src_unit END "
            "WHERE l.relation = 'c_to_c' AND (l.src_unit = ? OR l.dst_unit = ?)",
            (unit_id, unit_id, unit_id),
        )
        merged: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            item = dict(row)
            other = item["other_unit"]
            if other == unit_id:
                continue
            weight = float(item.get("weight") or 0.0)
            existing = merged.get(other)
            if existing is None or weight > existing["weight"]:
                merged[other] = {
                    "unit_id": other,
                    "hash": item.get("other_hash") or "",
                    "title": item.get("other_title") or "",
                    "weight": weight,
                    "meta": safe_json_loads(item.get("meta"), {}),
                }
        return sorted(merged.values(), key=lambda entry: entry["weight"], reverse=True)

    def all_links(self, hashes: Optional[Sequence[str]] = None,
                  relations: Optional[Sequence[str]] = None) -> List[Dict[str, Any]]:
        clauses: List[str] = []
        params: List[Any] = []
        if hashes:
            placeholders = ",".join("?" for _ in hashes)
            clauses.append(
                f"(l.src_unit IN (SELECT id FROM rag_units WHERE paper_hash IN ({placeholders})) "
                f"OR l.dst_unit IN (SELECT id FROM rag_units WHERE paper_hash IN ({placeholders})))"
            )
            params.extend(list(hashes) * 2)
        if relations:
            clauses.append("l.relation IN (" + ",".join("?" for _ in relations) + ")")
            params.extend(list(relations))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self.db.query_all(f"SELECT l.* FROM rag_links l{where}", params)
        return [dict(row) for row in rows]

    def delete_cross_links(self, paper_hash: str) -> None:
        self.db.execute(
            "DELETE FROM rag_links WHERE relation = 'c_to_c' AND (src_unit = ? OR dst_unit = ?)",
            (unit_id_c(paper_hash), unit_id_c(paper_hash)),
        )

    @staticmethod
    def _link_row(row: Any, prefix: str) -> Dict[str, Any]:
        item = dict(row)
        item["meta"] = safe_json_loads(item.get("meta"), {})
        item["neighbour"] = {
            "unit_id": item.get(f"{prefix}_unit"),
            "layer": item.get(f"{prefix}_layer"),
            "hash": item.get(f"{prefix}_hash"),
            "title": item.get(f"{prefix}_title", ""),
            "section": item.get(f"{prefix}_section", ""),
            "text": item.get(f"{prefix}_text", ""),
        }
        return item

    # ==================================================================
    # presentation
    # ==================================================================
    def pyramid(self, paper_hash: str) -> Dict[str, Any]:
        """Assemble the A/B/C structure with links, for the pyramid view."""
        units = self.units_of_paper(paper_hash)
        by_layer: Dict[str, List[Dict[str, Any]]] = {"A": [], "B": [], "C": []}
        for unit in units:
            # The API contract exposes `unit_id`; the column is `id`. Emitting
            # both keeps SQL-shaped callers and contract-shaped clients happy.
            unit["unit_id"] = unit["id"]
            by_layer.setdefault(unit["layer"], []).append(unit)

        c_unit = by_layer["C"][0] if by_layer["C"] else None
        b_units = by_layer["B"]
        a_units = by_layer["A"]

        # Attach neighbour ids so the frontend can draw the pyramid edges.
        # Direction convention: links run from the detailed layer up to the
        # condensed one, so a unit's parent is the destination of its links.
        for unit in b_units:
            unit["parents"] = self.parent_ids([unit["id"]], "b_to_c")
            unit["children"] = self.child_ids([unit["id"]], "a_to_b")
        for unit in a_units:
            unit["parents"] = self.parent_ids([unit["id"]], "a_to_b")
            unit["children"] = []

        result: Dict[str, Any] = {
            "hash": paper_hash,
            "C": None,
            "B": b_units,
            "A": a_units,
            "counts": {"A": len(a_units), "B": len(b_units), "C": 1 if c_unit else 0},
        }
        if c_unit:
            result["C"] = {
                "unit_id": c_unit["id"],
                "text": c_unit["text"],
                "title": c_unit.get("title", ""),
                "children": self.child_ids([c_unit["id"]], "b_to_c"),
                "cross_links": [
                    {
                        "hash": link.get("hash", ""),
                        "title": link.get("title", ""),
                        "score": round(float(link.get("weight") or 0.0), 4),
                    }
                    for link in self.cross_links(c_unit["id"])
                ],
            }
        return result
