"""Paper master table, authors, affiliations, assets and OKF concept index.

Covers FR-2 (论文名 / 作者含单位 / 期刊 / 刊发时间 / 哈希), FR-4 (图表资产) and
FR-5 (OKF 概念索引). Everything below is keyed by the paper hash, so deleting
one paper cascades through every dependent table (FR-13.5).
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from ...core.hashing import short_hash
from ...core.jsonutil import as_str_list, safe_json_loads
from ..db import Database, utc_now

PAPER_COLUMNS = (
    "hash", "title", "title_original", "journal", "venue_type", "published_at",
    "year", "doi", "url", "abstract", "keywords", "language", "page_count",
    "file_size", "file_path", "okf_path", "status", "error", "parse_provider",
    "parse_model", "created_at", "updated_at",
)

SORTABLE = {
    "created_at": "p.created_at",
    "updated_at": "p.updated_at",
    "title": "p.title",
    "year": "p.year",
    "published_at": "p.published_at",
    "journal": "p.journal",
}


class PaperRepository:
    """All reads and writes touching the paper aggregate."""

    def __init__(self, db: Database) -> None:
        self.db = db

    # ==================================================================
    # papers
    # ==================================================================
    def exists(self, hash_value: str) -> bool:
        return self.db.query_one("SELECT 1 FROM papers WHERE hash = ?", (hash_value,)) is not None

    def create(
        self,
        hash_value: str,
        *,
        title: str = "",
        file_path: str = "",
        okf_path: str = "",
        file_size: int = 0,
        status: str = "pending",
    ) -> Dict[str, Any]:
        now = utc_now()
        self.db.execute(
            """
            INSERT INTO papers (hash, title, file_path, okf_path, file_size, status,
                                created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(hash) DO UPDATE SET
                file_path = excluded.file_path,
                okf_path  = excluded.okf_path,
                file_size = excluded.file_size,
                updated_at = excluded.updated_at
            """,
            (hash_value, title, file_path, okf_path, int(file_size), status, now, now),
        )
        return self.get(hash_value) or {}

    def update(self, hash_value: str, **fields: Any) -> None:
        """Update arbitrary paper columns; unknown keys are ignored."""
        allowed = {k: v for k, v in fields.items() if k in PAPER_COLUMNS and k != "hash"}
        if not allowed:
            return
        if "keywords" in allowed and not isinstance(allowed["keywords"], str):
            import json

            allowed["keywords"] = json.dumps(as_str_list(allowed["keywords"]), ensure_ascii=False)
        allowed["updated_at"] = utc_now()
        assignments = ", ".join(f"{key} = ?" for key in allowed)
        params = list(allowed.values()) + [hash_value]
        self.db.execute(f"UPDATE papers SET {assignments} WHERE hash = ?", params)

    def set_status(self, hash_value: str, status: str, error: str = "") -> None:
        self.db.execute(
            "UPDATE papers SET status = ?, error = ?, updated_at = ? WHERE hash = ?",
            (status, error or "", utc_now(), hash_value),
        )

    def get(self, hash_value: str) -> Optional[Dict[str, Any]]:
        row = self.db.query_one("SELECT * FROM papers WHERE hash = ?", (hash_value,))
        return dict(row) if row else None

    def get_or_raise(self, hash_value: str) -> Dict[str, Any]:
        from ...core.errors import PaperNotFound

        paper = self.get(hash_value)
        if paper is None:
            raise PaperNotFound(f"论文不存在：{hash_value}")
        return paper

    def delete(self, hash_value: str) -> bool:
        cursor = self.db.execute("DELETE FROM papers WHERE hash = ?", (hash_value,))
        return cursor.rowcount > 0

    # ------------------------------------------------------------------
    # listing / search (FR-2.4, FR-12.2)
    # ------------------------------------------------------------------
    def _build_filters(
        self,
        *,
        q: str = "",
        hash_value: str = "",
        hashes: Optional[Sequence[str]] = None,
        author: str = "",
        journal: str = "",
        year_from: Optional[int] = None,
        year_to: Optional[int] = None,
        status: str = "",
    ) -> Tuple[str, List[Any]]:
        clauses: List[str] = []
        params: List[Any] = []

        if q:
            like = f"%{q.strip()}%"
            clauses.append(
                "(p.title LIKE ? OR p.title_original LIKE ? OR p.journal LIKE ? "
                "OR p.hash LIKE ? OR p.abstract LIKE ? OR EXISTS ("
                "  SELECT 1 FROM authors a WHERE a.paper_hash = p.hash AND a.name LIKE ?"
                ") OR EXISTS ("
                "  SELECT 1 FROM affiliations f WHERE f.paper_hash = p.hash AND f.name LIKE ?"
                "))"
            )
            params.extend([like] * 7)

        if hash_value:
            clauses.append("p.hash LIKE ?")
            params.append(f"{hash_value.strip().lower()}%")

        if hashes:
            placeholders = ",".join("?" for _ in hashes)
            clauses.append(f"p.hash IN ({placeholders})")
            params.extend(list(hashes))

        if author:
            clauses.append(
                "EXISTS (SELECT 1 FROM authors a WHERE a.paper_hash = p.hash AND a.name LIKE ?)"
            )
            params.append(f"%{author.strip()}%")

        if journal:
            clauses.append("p.journal LIKE ?")
            params.append(f"%{journal.strip()}%")

        if year_from is not None:
            clauses.append("p.year >= ?")
            params.append(int(year_from))
        if year_to is not None:
            clauses.append("p.year <= ?")
            params.append(int(year_to))
        if status:
            clauses.append("p.status = ?")
            params.append(status)

        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        return where, params

    def list(
        self,
        *,
        q: str = "",
        hash_value: str = "",
        hashes: Optional[Sequence[str]] = None,
        author: str = "",
        journal: str = "",
        year_from: Optional[int] = None,
        year_to: Optional[int] = None,
        status: str = "",
        sort: str = "-created_at",
        page: int = 1,
        page_size: int = 20,
    ) -> Tuple[List[Dict[str, Any]], int]:
        where, params = self._build_filters(
            q=q, hash_value=hash_value, hashes=hashes, author=author, journal=journal,
            year_from=year_from, year_to=year_to, status=status,
        )
        total = int(self.db.query_scalar(f"SELECT COUNT(*) FROM papers p{where}", params, 0) or 0)

        descending = sort.startswith("-")
        column = SORTABLE.get(sort.lstrip("-"), SORTABLE["created_at"])
        direction = "DESC" if descending else "ASC"
        page = max(1, int(page or 1))
        page_size = max(1, min(int(page_size or 20), 200))
        offset = (page - 1) * page_size

        rows = self.db.query_all(
            f"SELECT p.* FROM papers p{where} ORDER BY {column} {direction}, p.hash LIMIT ? OFFSET ?",
            list(params) + [page_size, offset],
        )
        return [dict(row) for row in rows], total

    def all_hashes(self) -> List[str]:
        return [row["hash"] for row in self.db.query_all("SELECT hash FROM papers ORDER BY created_at")]

    def distinct_journals(self) -> List[str]:
        rows = self.db.query_all(
            "SELECT DISTINCT journal FROM papers WHERE journal <> '' ORDER BY journal"
        )
        return [row["journal"] for row in rows]

    def year_range(self) -> Tuple[Optional[int], Optional[int]]:
        row = self.db.query_one("SELECT MIN(year) AS lo, MAX(year) AS hi FROM papers WHERE year IS NOT NULL")
        if not row:
            return None, None
        return row["lo"], row["hi"]

    # ------------------------------------------------------------------
    # summaries used by list/detail endpoints
    # ------------------------------------------------------------------
    def counts_for(self, hash_value: str) -> Dict[str, int]:
        return {
            "figures": int(self.db.query_scalar(
                "SELECT COUNT(*) FROM assets WHERE paper_hash = ? AND kind = 'figure'", (hash_value,), 0) or 0),
            "tables": int(self.db.query_scalar(
                "SELECT COUNT(*) FROM assets WHERE paper_hash = ? AND kind = 'table'", (hash_value,), 0) or 0),
            "rag_a": int(self.db.query_scalar(
                "SELECT COUNT(*) FROM rag_units WHERE paper_hash = ? AND layer = 'A'", (hash_value,), 0) or 0),
            "rag_b": int(self.db.query_scalar(
                "SELECT COUNT(*) FROM rag_units WHERE paper_hash = ? AND layer = 'B'", (hash_value,), 0) or 0),
            "rag_c": int(self.db.query_scalar(
                "SELECT COUNT(*) FROM rag_units WHERE paper_hash = ? AND layer = 'C'", (hash_value,), 0) or 0),
            "kg_nodes": int(self.db.query_scalar(
                "SELECT COUNT(*) FROM kg_nodes WHERE paper_hash = ?", (hash_value,), 0) or 0),
            "okf_concepts": int(self.db.query_scalar(
                "SELECT COUNT(*) FROM okf_concepts WHERE paper_hash = ?", (hash_value,), 0) or 0),
            "authors": int(self.db.query_scalar(
                "SELECT COUNT(*) FROM authors WHERE paper_hash = ?", (hash_value,), 0) or 0),
        }

    def counts_for_many(self, hashes: Sequence[str]) -> Dict[str, Dict[str, int]]:
        """Batch version used by the list endpoint to avoid N+1 queries."""
        if not hashes:
            return {}
        placeholders = ",".join("?" for _ in hashes)
        result: Dict[str, Dict[str, int]] = {h: {} for h in hashes}

        def collect(sql: str, key: str, extra: str = "") -> None:
            params: List[Any] = list(hashes)
            rows = self.db.query_all(sql.format(ph=placeholders, extra=extra), params)
            for row in rows:
                result.setdefault(row["paper_hash"], {})[key] = int(row["n"])

        collect("SELECT paper_hash, COUNT(*) AS n FROM assets WHERE paper_hash IN ({ph}) AND kind='figure' GROUP BY paper_hash", "figures")
        collect("SELECT paper_hash, COUNT(*) AS n FROM assets WHERE paper_hash IN ({ph}) AND kind='table' GROUP BY paper_hash", "tables")
        collect("SELECT paper_hash, COUNT(*) AS n FROM rag_units WHERE paper_hash IN ({ph}) AND layer='A' GROUP BY paper_hash", "rag_a")
        collect("SELECT paper_hash, COUNT(*) AS n FROM rag_units WHERE paper_hash IN ({ph}) AND layer='B' GROUP BY paper_hash", "rag_b")
        collect("SELECT paper_hash, COUNT(*) AS n FROM rag_units WHERE paper_hash IN ({ph}) AND layer='C' GROUP BY paper_hash", "rag_c")
        collect("SELECT paper_hash, COUNT(*) AS n FROM kg_nodes WHERE paper_hash IN ({ph}) GROUP BY paper_hash", "kg_nodes")
        collect("SELECT paper_hash, COUNT(*) AS n FROM authors WHERE paper_hash IN ({ph}) GROUP BY paper_hash", "authors")
        for value in result.values():
            for key in ("figures", "tables", "rag_a", "rag_b", "rag_c", "kg_nodes", "authors"):
                value.setdefault(key, 0)
        return result

    def authors_for_many(self, hashes: Sequence[str]) -> Dict[str, List[Dict[str, Any]]]:
        """Authors per paper, each carrying its affiliation names.

        The batch counterpart of :meth:`list_authors`: the list endpoint and the
        ``paper_meta`` tool need institutions, and issuing one join per author
        would be an N+1 query.
        """
        if not hashes:
            return {}
        placeholders = ",".join("?" for _ in hashes)
        rows = self.db.query_all(
            f"SELECT * FROM authors WHERE paper_hash IN ({placeholders}) "
            "ORDER BY paper_hash, order_index",
            list(hashes),
        )
        grouped: Dict[str, List[Dict[str, Any]]] = {h: [] for h in hashes}
        by_id: Dict[int, Dict[str, Any]] = {}
        for row in rows:
            item = dict(row)
            item["affiliations"] = []
            by_id[item["id"]] = item
            grouped.setdefault(item["paper_hash"], []).append(item)

        if by_id:
            ids = list(by_id)
            chunk = 400
            for start in range(0, len(ids), chunk):
                batch = ids[start : start + chunk]
                marks = ",".join("?" for _ in batch)
                links = self.db.query_all(
                    f"SELECT aa.author_id, f.name FROM author_affiliations aa "
                    f"JOIN affiliations f ON f.id = aa.affiliation_id "
                    f"WHERE aa.author_id IN ({marks})",
                    batch,
                )
                for link in links:
                    author = by_id.get(link["author_id"])
                    if author is not None:
                        author["affiliations"].append(link["name"])
        return grouped

    # ------------------------------------------------------------------
    # author / affiliation persistence (FR-2.3)
    # ------------------------------------------------------------------
    def replace_authors_and_affiliations(
        self,
        hash_value: str,
        authors: Sequence[Dict[str, Any]],
        affiliations: Sequence[Dict[str, Any]] = (),
    ) -> None:
        """Rewrite authors and affiliations for a paper.

        Accepts the model's loose output: ``authors[].affiliations`` may be a
        list of names, of objects, or absent. The union of every referenced
        affiliation name is materialised so the many-to-many table is complete.
        """
        self.db.execute("DELETE FROM author_affiliations WHERE author_id IN "
                        "(SELECT id FROM authors WHERE paper_hash = ?)", (hash_value,))
        self.db.execute("DELETE FROM authors WHERE paper_hash = ?", (hash_value,))
        self.db.execute("DELETE FROM affiliations WHERE paper_hash = ?", (hash_value,))

        # 1. Collect affiliation catalogue, preserving order of first mention.
        catalogue: List[Dict[str, str]] = []
        index_by_name: Dict[str, int] = {}

        def add_affiliation(entry: Any) -> Optional[str]:
            if isinstance(entry, dict):
                name = str(entry.get("name") or entry.get("institution") or entry.get("org") or "").strip()
                department = str(entry.get("department") or entry.get("dept") or "").strip()
                country = str(entry.get("country") or "").strip()
                raw = str(entry.get("raw") or "").strip()
            else:
                name = str(entry or "").strip()
                department = country = raw = ""
            if not name:
                return None
            key = name.lower()
            if key not in index_by_name:
                index_by_name[key] = len(catalogue)
                catalogue.append({"name": name, "department": department, "country": country, "raw": raw})
            return key

        for entry in affiliations or ():
            add_affiliation(entry)
        for author in authors or ():
            if isinstance(author, dict):
                for entry in author.get("affiliations") or author.get("affiliation") or []:
                    add_affiliation(entry)

        now = utc_now()
        affiliation_ids: Dict[str, int] = {}
        for order, item in enumerate(catalogue):
            cursor = self.db.execute(
                "INSERT INTO affiliations (paper_hash, order_index, name, department, country, raw) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (hash_value, order, item["name"], item.get("department", ""),
                 item.get("country", ""), item.get("raw", "")),
            )
            affiliation_ids[item["name"].lower()] = int(cursor.lastrowid)

        # 2. Authors plus their links.
        with self.db.transaction() as conn:
            for order, author in enumerate(authors or ()):
                if isinstance(author, str):
                    author = {"name": author}
                if not isinstance(author, dict):
                    continue
                name = str(author.get("name") or author.get("name_en") or "").strip()
                if not name:
                    continue
                cursor = conn.execute(
                    "INSERT INTO authors (paper_hash, order_index, name, name_en, "
                    "is_corresponding, email, orcid) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        hash_value,
                        order,
                        name,
                        str(author.get("name_en") or "").strip(),
                        1 if author.get("corresponding") or author.get("is_corresponding") else 0,
                        str(author.get("email") or "").strip(),
                        str(author.get("orcid") or "").strip(),
                    ),
                )
                author_id = int(cursor.lastrowid)
                seen: set = set()
                for entry in author.get("affiliations") or author.get("affiliation") or []:
                    if isinstance(entry, dict):
                        key = str(entry.get("name") or entry.get("institution") or "").strip().lower()
                    else:
                        key = str(entry or "").strip().lower()
                    if not key or key in seen:
                        continue
                    seen.add(key)
                    target = affiliation_ids.get(key)
                    if target:
                        conn.execute(
                            "INSERT OR IGNORE INTO author_affiliations (author_id, affiliation_id) VALUES (?, ?)",
                            (author_id, target),
                        )

    def list_authors(self, hash_value: str) -> List[Dict[str, Any]]:
        rows = self.db.query_all(
            "SELECT * FROM authors WHERE paper_hash = ? ORDER BY order_index", (hash_value,)
        )
        authors = [dict(row) for row in rows]
        if not authors:
            return []
        links = self.db.query_all(
            "SELECT aa.author_id, f.name FROM author_affiliations aa "
            "JOIN affiliations f ON f.id = aa.affiliation_id "
            "JOIN authors a ON a.id = aa.author_id WHERE a.paper_hash = ?",
            (hash_value,),
        )
        by_author: Dict[int, List[str]] = {}
        for link in links:
            by_author.setdefault(link["author_id"], []).append(link["name"])
        for author in authors:
            author["affiliations"] = by_author.get(author["id"], [])
        return authors

    def list_affiliations(self, hash_value: str) -> List[Dict[str, Any]]:
        rows = self.db.query_all(
            "SELECT * FROM affiliations WHERE paper_hash = ? ORDER BY order_index", (hash_value,)
        )
        return [dict(row) for row in rows]

    # ==================================================================
    # assets (FR-4)
    # ==================================================================
    def replace_assets(self, hash_value: str, assets: Sequence[Dict[str, Any]]) -> None:
        self.db.execute("DELETE FROM assets WHERE paper_hash = ?", (hash_value,))
        now = utc_now()
        import json

        with self.db.transaction() as conn:
            for item in assets:
                conn.execute(
                    "INSERT OR REPLACE INTO assets (paper_hash, kind, label, ordinal, page, bbox, "
                    "caption, caption_source, content_text, section, file_path, width, height, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        hash_value,
                        item.get("kind", "figure"),
                        item["label"],
                        int(item.get("ordinal", 0)),
                        int(item.get("page", 0)),
                        json.dumps(item.get("bbox") or [], ensure_ascii=False),
                        item.get("caption", "") or "",
                        item.get("caption_source", "local"),
                        item.get("content_text", "") or "",
                        item.get("section", "") or "",
                        item["file_path"],
                        int(item.get("width", 0)),
                        int(item.get("height", 0)),
                        now,
                    ),
                )

    def list_assets(self, hash_value: str, kind: str = "") -> List[Dict[str, Any]]:
        if kind:
            rows = self.db.query_all(
                "SELECT * FROM assets WHERE paper_hash = ? AND kind = ? ORDER BY ordinal",
                (hash_value, kind),
            )
        else:
            rows = self.db.query_all(
                "SELECT * FROM assets WHERE paper_hash = ? ORDER BY kind DESC, ordinal",
                (hash_value,),
            )
        out = []
        for row in rows:
            item = dict(row)
            item["bbox"] = safe_json_loads(item.get("bbox"), [])
            out.append(item)
        return out

    def get_asset(self, hash_value: str, label: str) -> Optional[Dict[str, Any]]:
        row = self.db.query_one(
            "SELECT * FROM assets WHERE paper_hash = ? AND label = ?", (hash_value, label)
        )
        if not row:
            return None
        item = dict(row)
        item["bbox"] = safe_json_loads(item.get("bbox"), [])
        return item

    # ==================================================================
    # OKF concept index (FR-5)
    # ==================================================================
    def replace_okf_concepts(self, hash_value: str, concepts: Sequence[Dict[str, Any]]) -> None:
        self.db.execute("DELETE FROM okf_concepts WHERE paper_hash = ?", (hash_value,))
        now = utc_now()
        with self.db.transaction() as conn:
            for order, item in enumerate(concepts):
                conn.execute(
                    "INSERT OR REPLACE INTO okf_concepts (paper_hash, concept_id, type, title, "
                    "description, section, file_path, ordinal, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        hash_value,
                        item["concept_id"],
                        item.get("type", "Reference"),
                        item.get("title", "") or "",
                        item.get("description", "") or "",
                        item.get("section", "") or "",
                        item["file_path"],
                        int(item.get("ordinal", order)),
                        now,
                    ),
                )

    def list_okf_concepts(self, hash_value: str) -> List[Dict[str, Any]]:
        rows = self.db.query_all(
            "SELECT * FROM okf_concepts WHERE paper_hash = ? ORDER BY ordinal", (hash_value,)
        )
        return [dict(row) for row in rows]

    def get_okf_concept(self, hash_value: str, concept_id: str) -> Optional[Dict[str, Any]]:
        row = self.db.query_one(
            "SELECT * FROM okf_concepts WHERE paper_hash = ? AND concept_id = ?",
            (hash_value, concept_id),
        )
        return dict(row) if row else None

    # ==================================================================
    # presentation helper
    # ==================================================================
    @staticmethod
    def summarise(paper: Dict[str, Any], counts: Optional[Dict[str, int]] = None,
                  authors: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        """Shape a row into the list-item object the frontend consumes."""
        hash_value = paper.get("hash", "")
        return {
            "hash": hash_value,
            "hash_short": short_hash(hash_value),
            "title": paper.get("title") or "",
            "title_original": paper.get("title_original") or "",
            "journal": paper.get("journal") or "",
            "venue_type": paper.get("venue_type") or "",
            "published_at": paper.get("published_at") or "",
            "year": paper.get("year"),
            "doi": paper.get("doi") or "",
            "language": paper.get("language") or "en",
            "page_count": int(paper.get("page_count") or 0),
            "status": paper.get("status") or "pending",
            "error": paper.get("error") or "",
            "created_at": paper.get("created_at") or "",
            "updated_at": paper.get("updated_at") or "",
            "keywords": safe_json_loads(paper.get("keywords"), []),
            "authors": [
                {"name": a.get("name", ""), "affiliations": a.get("affiliations", []),
                 "is_corresponding": bool(a.get("is_corresponding"))}
                for a in (authors or [])
            ],
            "counts": counts or {
                "figures": 0, "tables": 0, "rag_a": 0, "rag_b": 0, "rag_c": 0,
                "kg_nodes": 0, "okf_concepts": 0, "authors": 0,
            },
        }
