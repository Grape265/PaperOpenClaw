"""ChromaDB-backed vector store: three collections, one per pyramid layer.

Document ids are identical to ``rag_units.id``, so the vector store and the
relational index are keyed the same way (FR-13.2). That makes a rebuild a
straight re-projection of the relational table rather than a data migration.

Chroma metadata values must be scalars, so anything structured is flattened to
a JSON string or dropped — the relational row remains the place to look for
full detail.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from ..core.logging import get_logger

logger = get_logger(__name__)

LAYER_COLLECTIONS = {"A": "rag_a", "B": "rag_b", "C": "rag_c"}
LAYERS = tuple(LAYER_COLLECTIONS.keys())


class VectorStoreError(RuntimeError):
    """Raised when the vector store cannot serve a request."""


def _flatten_metadata(meta: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Keep only Chroma-compatible scalar values."""
    flat: Dict[str, Any] = {}
    for key, value in (meta or {}).items():
        if value is None:
            continue
        if isinstance(value, bool):
            flat[key] = value
        elif isinstance(value, (int, float)):
            flat[key] = value
        elif isinstance(value, str):
            flat[key] = value[:2000]
        elif isinstance(value, (list, tuple, set)):
            flat[key] = ",".join(str(item) for item in value)[:2000]
        elif isinstance(value, dict):
            import json

            flat[key] = json.dumps(value, ensure_ascii=False)[:2000]
    return flat


class VectorStore:
    """Embedded, persistent vector index for the A/B/C pyramid."""

    def __init__(self, path: Path, *, anonymized_telemetry: bool = False) -> None:
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._client = None
        self._collections: Dict[str, Any] = {}
        self._dimension: Optional[int] = None
        try:
            import chromadb
            from chromadb.config import Settings as ChromaSettings

            self._client = chromadb.PersistentClient(
                path=str(self.path),
                settings=ChromaSettings(anonymized_telemetry=anonymized_telemetry,
                                        allow_reset=True),
            )
        except Exception as exc:  # pragma: no cover - dependency/environment issue
            raise VectorStoreError(f"无法初始化向量库：{exc}") from exc

    # ------------------------------------------------------------------
    # collections
    # ------------------------------------------------------------------
    def collection(self, layer: str):
        layer = layer.upper()
        name = LAYER_COLLECTIONS.get(layer)
        if name is None:
            raise VectorStoreError(f"未知的语义层：{layer}")
        with self._lock:
            cached = self._collections.get(layer)
            if cached is not None:
                return cached
            try:
                handle = self._client.get_or_create_collection(
                    name=name,
                    metadata={"hnsw:space": "cosine", "layer": layer},
                )
            except Exception as exc:  # pragma: no cover
                raise VectorStoreError(f"无法打开集合 {name}：{exc}") from exc
            self._collections[layer] = handle
            return handle

    @property
    def dimension(self) -> Optional[int]:
        """Dimension of the stored vectors, inferred from a sample when needed."""
        if self._dimension is not None:
            return self._dimension
        for layer in LAYERS:
            try:
                sample = self.collection(layer).get(limit=1, include=["embeddings"])
            except Exception:  # pragma: no cover - empty or unavailable
                continue
            embeddings = sample.get("embeddings")
            if embeddings is not None and len(embeddings):
                self._dimension = len(embeddings[0])
                return self._dimension
        return None

    # ------------------------------------------------------------------
    # writes
    # ------------------------------------------------------------------
    def upsert(
        self,
        layer: str,
        ids: Sequence[str],
        embeddings: Sequence[Sequence[float]],
        documents: Sequence[str],
        metadatas: Optional[Sequence[Dict[str, Any]]] = None,
    ) -> int:
        if not ids:
            return 0
        if not (len(ids) == len(embeddings) == len(documents)):
            raise VectorStoreError("ids、embeddings、documents 长度不一致")
        dimension = len(embeddings[0]) if embeddings else 0
        existing = self.dimension
        if existing and dimension and existing != dimension:
            raise VectorStoreError(
                f"向量维度不匹配：库中为 {existing}，本次为 {dimension}。"
                "更换嵌入模型后需要重建向量库。"
            )
        metas = [_flatten_metadata(item) for item in metadatas] if metadatas else None

        handle = self.collection(layer)
        written = 0
        batch = 256
        with self._lock:
            for start in range(0, len(ids), batch):
                stop = start + batch
                kwargs: Dict[str, Any] = {
                    "ids": list(ids[start:stop]),
                    "embeddings": [list(map(float, vector)) for vector in embeddings[start:stop]],
                    "documents": list(documents[start:stop]),
                }
                if metas is not None:
                    kwargs["metadatas"] = metas[start:stop]
                try:
                    handle.upsert(**kwargs)
                except Exception as exc:
                    raise VectorStoreError(f"写入向量失败：{exc}") from exc
                written += len(kwargs["ids"])
            if dimension and not self._dimension:
                self._dimension = dimension
        return written

    def delete_ids(self, layer: str, ids: Sequence[str]) -> int:
        if not ids:
            return 0
        handle = self.collection(layer)
        with self._lock:
            try:
                handle.delete(ids=list(ids))
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("删除向量失败（忽略）：%s", exc)
                return 0
        return len(ids)

    def delete_paper(self, hash_value: str, layers: Sequence[str] = LAYERS) -> int:
        """Remove every vector belonging to one paper (FR-13.5)."""
        removed = 0
        for layer in layers:
            handle = self.collection(layer)
            with self._lock:
                try:
                    handle.delete(where={"paper_hash": hash_value})
                    removed += 1
                except Exception as exc:  # pragma: no cover - empty collection
                    logger.debug("按论文删除向量为空或失败（%s/%s）：%s", layer, hash_value, exc)
        return removed

    def reset(self, layer: Optional[str] = None) -> None:
        layers = [layer.upper()] if layer else list(LAYERS)
        with self._lock:
            for item in layers:
                name = LAYER_COLLECTIONS.get(item)
                if not name:
                    continue
                try:
                    self._client.delete_collection(name)
                except Exception:  # pragma: no cover - did not exist
                    pass
                self._collections.pop(item, None)
            self._dimension = None

    # ------------------------------------------------------------------
    # reads
    # ------------------------------------------------------------------
    def query(
        self,
        layer: str,
        embedding: Sequence[float],
        top_k: int = 10,
        hashes: Optional[Sequence[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Return the ``top_k`` nearest units, best first, with cosine similarity."""
        if not embedding:
            return []
        handle = self.collection(layer)
        where: Optional[Dict[str, Any]] = None
        if hashes:
            where = {"paper_hash": {"$in": list(hashes)}} if len(hashes) > 1 else {"paper_hash": hashes[0]}
        try:
            result = handle.query(
                query_embeddings=[list(map(float, embedding))],
                n_results=max(1, int(top_k)),
                where=where,
                include=["documents", "metadatas", "distances"],
            )
        except Exception as exc:
            logger.warning("向量检索失败（%s）：%s", layer, exc)
            return []

        ids = (result.get("ids") or [[]])[0]
        documents = (result.get("documents") or [[]])[0]
        metadatas = (result.get("metadatas") or [[]])[0]
        distances = (result.get("distances") or [[]])[0]

        hits: List[Dict[str, Any]] = []
        for index, unit_id in enumerate(ids):
            distance = float(distances[index]) if index < len(distances) else 1.0
            hits.append({
                "id": unit_id,
                "text": documents[index] if index < len(documents) else "",
                "metadata": dict(metadatas[index]) if index < len(metadatas) and metadatas[index] else {},
                # Chroma cosine space returns 1 - similarity.
                "score": max(0.0, min(1.0, 1.0 - distance)),
            })
        return hits

    def get(self, layer: str, ids: Sequence[str]) -> List[Dict[str, Any]]:
        if not ids:
            return []
        handle = self.collection(layer)
        try:
            result = handle.get(ids=list(ids), include=["documents", "metadatas"])
        except Exception as exc:  # pragma: no cover
            logger.warning("读取向量失败（%s）：%s", layer, exc)
            return []
        out = []
        for index, unit_id in enumerate(result.get("ids") or []):
            documents = result.get("documents") or []
            metadatas = result.get("metadatas") or []
            out.append({
                "id": unit_id,
                "text": documents[index] if index < len(documents) else "",
                "metadata": dict(metadatas[index]) if index < len(metadatas) and metadatas[index] else {},
            })
        return out

    def count(self, layer: Optional[str] = None) -> Dict[str, int]:
        result: Dict[str, int] = {}
        for item in ([layer.upper()] if layer else list(LAYERS)):
            try:
                result[item] = int(self.collection(item).count())
            except Exception:  # pragma: no cover - unavailable collection
                result[item] = 0
        return result

    def health(self) -> Dict[str, Any]:
        try:
            counts = self.count()
            return {"ok": True, "path": str(self.path), "counts": counts,
                    "dimension": self.dimension}
        except Exception as exc:  # pragma: no cover - defensive
            return {"ok": False, "path": str(self.path), "error": str(exc)}

    def close(self) -> None:
        """Release the client and its file handles as far as the library allows.

        ChromaDB exposes no explicit close, so dropping the reference and
        forcing a collection pass is the best available. Needed mainly so that
        test teardown and data-directory moves do not trip over Windows file
        locking.
        """
        import gc

        with self._lock:
            self._collections.clear()
            self._client = None
            self._dimension = None
        gc.collect()
