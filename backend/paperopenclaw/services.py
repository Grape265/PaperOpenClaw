"""Service container: the single composition root for the application.

Everything is constructed once and shared: repositories, the provider registry,
the vector store, the embedding provider, the retriever, the ingest pipeline and
the agent runtime. Gateway routers depend on this object rather than building
their own collaborators, which keeps request handling free of wiring.

The embedding provider is resolved lazily and cached against the embedding
configuration, because it can be the most expensive object in the process (a
local transformer model) and the settings that choose it are editable at runtime.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import PROVIDER_PRESETS, Settings, SettingsStore, bootstrap_settings
from .core.logging import get_logger
from .core.paths import DataPaths
from .ingest.pipeline import IngestPipeline
from .llm.registry import ProviderRegistry
from .memory.embeddings import EmbeddingProvider, build_embedding_provider
from .memory.retriever import PyramidRetriever
from .memory.vector_store import VectorStore
from .storage.db import Database
from .storage.repos import (
    JobRepository,
    KnowledgeGraphRepository,
    FishboneRepository,
    PaperRepository,
    RagRepository,
    SessionRepository,
    SkillRepository,
)

logger = get_logger(__name__)


class Services:
    """Owns every long-lived collaborator."""

    def __init__(self, paths: DataPaths, settings: SettingsStore) -> None:
        self.paths = paths
        self.settings = settings

        self.db = Database(paths.sqlite_file).initialise()
        self.jobs = JobRepository(self.db)
        self.papers = PaperRepository(self.db)
        self.rag = RagRepository(self.db)
        self.kg = KnowledgeGraphRepository(self.db)
        self.fishbone = FishboneRepository(self.db)
        self.sessions = SessionRepository(self.db)
        self.skills = SkillRepository(self.db)

        # A crash mid-ingest leaves rows marked running; nothing can still be
        # running at startup, so mark them failed rather than lying forever.
        stale = self.jobs.mark_stale_running_as_failed()
        if stale:
            logger.warning("已将 %s 个中断的任务标记为失败", stale)

        self.registry = ProviderRegistry(settings)
        self.vectors = VectorStore(paths.vectors_dir)

        self._embedder_lock = threading.RLock()
        self._embedder: Optional[EmbeddingProvider] = None
        self._embedder_key: Optional[str] = None

        self.retriever = PyramidRetriever(
            self.papers, self.rag, self.vectors, _EmbedderProxy(self), settings
        )
        self.ingest = IngestPipeline(
            paths=paths,
            settings=settings,
            papers=self.papers,
            rag=self.rag,
            kg=self.kg,
            fishbone=self.fishbone,
            jobs=self.jobs,
            registry=self.registry,
            vectors=self.vectors,
            embedder_provider=lambda: self.embedder,
        )

        # Imported lazily to avoid a circular import at module load.
        from .agent import AgentRuntime
        from .agent.skill_loader import SkillLoader

        self.skill_loader = SkillLoader(paths, self.skills)
        self.agents = AgentRuntime(self)

        self.fishbone.ensure_spine_nodes()

    # ------------------------------------------------------------------
    # embeddings
    # ------------------------------------------------------------------
    def _embedding_key(self) -> str:
        config = self.settings.current.embedding
        return "|".join([
            config.provider, config.model, config.model_path,
            str(config.dimension), config.remote_model, config.base_url,
        ])

    @property
    def embedder(self) -> EmbeddingProvider:
        """Current embedding provider, rebuilt when its configuration changes."""
        key = self._embedding_key()
        with self._embedder_lock:
            if self._embedder is not None and self._embedder_key == key:
                return self._embedder
            config = self.settings.current.embedding
            remote_client = None
            if config.provider in ("auto", "remote"):
                remote_client = self.registry.try_client("chat")
            provider = build_embedding_provider(
                config,
                models_dirs=self.models_dirs,
                remote_client=remote_client if config.base_url or config.remote_model else None,
            )
            self._embedder = provider
            self._embedder_key = key
            logger.info("嵌入提供方已就绪：%s", provider.describe())
            return provider

    def refresh_embedder(self) -> EmbeddingProvider:
        with self._embedder_lock:
            self._embedder = None
            self._embedder_key = None
        return self.embedder

    @property
    def models_dirs(self) -> List[Path]:
        """Every location searched for a locally-downloaded embedding model.

        Ordered by specificity: the data directory first (a user-supplied model
        for this corpus), then the checkout's own cache, then the machine's
        HuggingFace cache. Searching all of them means a model that is already on
        the machine is found rather than silently ignored.
        """
        repository_root = Path(__file__).resolve().parents[2]
        candidates = [
            self.paths.models_dir,
            repository_root / ".cache" / "models",
            Path.cwd() / ".cache" / "models",
            Path.home() / ".cache" / "huggingface" / "hub",
        ]
        seen: set = set()
        ordered: List[Path] = []
        for candidate in candidates:
            resolved = Path(candidate)
            key = str(resolved)
            if key in seen:
                continue
            seen.add(key)
            ordered.append(resolved)
        return ordered

    # ------------------------------------------------------------------
    # diagnostics
    # ------------------------------------------------------------------
    def counts(self) -> Dict[str, int]:
        counts = self.db.table_counts()
        try:
            vectors = self.vectors.count()
        except Exception:  # pragma: no cover - vector store unavailable
            vectors = {}
        counts["vectors_a"] = vectors.get("A", 0)
        counts["vectors_b"] = vectors.get("B", 0)
        counts["vectors_c"] = vectors.get("C", 0)
        return counts

    def health(self) -> Dict[str, Any]:
        state = self.settings.current
        embedder_info: Dict[str, Any] = {"provider": state.embedding.provider,
                                         "model": state.embedding.model}
        try:
            provider = self.embedder
            embedder_info = provider.describe()
        except Exception as exc:  # pragma: no cover - defensive
            embedder_info["error"] = str(exc)

        return {
            "status": "ok",
            "version": _version(),
            "data_dir": str(self.paths.root),
            "providers": self.registry.status(),
            "embedding": embedder_info,
            "vectors": self.vectors.health(),
            "counts": self.counts(),
        }

    def doctor(self) -> Dict[str, Any]:
        """Deeper diagnostics backing ``GET /doctor``."""
        checks: List[Dict[str, Any]] = []

        def check(name: str, ok: bool, detail: str = "") -> None:
            checks.append({"name": name, "ok": bool(ok), "detail": detail})

        writable = False
        try:
            probe = self.paths.root / ".write-probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            writable = True
        except Exception as exc:  # noqa: BLE001
            check("数据目录可写", False, str(exc))
        else:
            check("数据目录可写", writable, str(self.paths.root))

        check("SQLite 完整性", self.db.integrity_check() == "ok",
              f"{self.db.count('papers')} 篇论文")

        vectors = self.vectors.health()
        check("向量库可用", bool(vectors.get("ok")), str(vectors.get("counts") or vectors.get("error", "")))

        try:
            embedder = self.embedder
            vector = embedder.encode_one("健康检查")
            check("嵌入提供方可用", bool(vector),
                  f"{embedder.name} dim={len(vector)}")
        except Exception as exc:  # noqa: BLE001
            check("嵌入提供方可用", False, str(exc))

        routes = self.registry.status()
        check("解析模型已配置", bool(routes.get("parse", {}).get("configured")),
              str(routes.get("parse", {}).get("model", "")))
        check("问答模型已配置", bool(routes.get("chat", {}).get("configured")),
              str(routes.get("chat", {}).get("model", "")))

        okf_concepts = self.db.count("okf_concepts")
        check("OKF 概念索引", okf_concepts >= 0, f"{okf_concepts} 个概念")

        return {
            "ok": all(item["ok"] for item in checks if item["name"] != "嵌入提供方可用"
                      or item["ok"]),
            "checks": checks,
            "counts": self.counts(),
        }

    def rebuild_knowledge_graphs(self, hashes: Optional[List[str]] = None) -> int:
        """Rebuild both graph stores from the RAG pyramid, for given or all papers.

        The fishbone graph is derived from layer B, the keyword knowledge graph
        from layer A — neither needs a model call, so this regenerates both
        stores whenever the extraction logic changes.
        """
        from .ingest.fishbone_builder import FishboneBuilder
        from .ingest.kg_builder import KnowledgeGraphBuilder

        fishbone_builder = FishboneBuilder(self.papers, self.fishbone, self.rag)
        kg_builder = KnowledgeGraphBuilder(self.papers, self.kg, self.rag)
        if hashes:
            targets = [h for h in hashes if self.papers.get(h) is not None]
        else:
            # 全量重建：先清空关键词图，避免旧结构（spine/content/asset）残留。
            self.kg.delete_all_keywords()
            targets = self.papers.all_hashes()

        rebuilt = 0
        for hash_value in targets:
            paper = self.papers.get(hash_value)
            if paper is None:
                continue
            assets = self.papers.list_assets(hash_value)
            fulltext_path = self.paths.paper_okf_dir(hash_value) / "fulltext.md"
            fulltext = ""
            if fulltext_path.is_file():
                try:
                    from .memory.okf_fs import load_concept
                    fulltext = str(load_concept(fulltext_path).get("body") or "")
                except Exception:  # noqa: BLE001
                    fulltext = ""
            digest_client = self.registry.try_client("digest") or self.registry.try_client("chat")
            fishbone_builder.build_from_okf(
                hash_value, title=paper.get("title", ""), full_text=fulltext,
                assets=assets, client=digest_client,
            )
            kg_builder.build_from_okf(
                hash_value, title=paper.get("title", ""), full_text=fulltext,
                client=digest_client,
            )
            rebuilt += 1
        logger.info("双图库重建完成：%s 篇", rebuilt)
        return rebuilt

    def okf_fulltext(self, hash_value: str, max_chars: int = 2400) -> str:
        """Read the OKF ``fulltext.md`` body (verbatim text) for one paper.

        This is the deepest, most faithful source; the RAG pyramid distils it and
        the QA flow consults it when the distilled layers do not reach the detail
        level (i.e. "有必要时查看 OKF").
        """
        from .memory.okf_fs import load_concept

        try:
            path = self.paths.paper_okf_dir(hash_value) / "fulltext.md"
            if not path.is_file():
                return ""
            parsed = load_concept(path)
            body = str(parsed.get("body") or "")
            return body if len(body) <= max_chars else body[:max_chars] + "…（已截断）"
        except Exception as exc:  # noqa: BLE001 - optional grounding source
            logger.warning("读取 OKF 原文失败（%s）：%s", hash_value[:8], exc)
            return ""

    def close(self) -> None:
        try:
            self.vectors.close()
        except Exception:  # pragma: no cover - defensive
            pass
        self.db.close()


class _EmbedderProxy(EmbeddingProvider):
    """Late-binding embedder so the retriever survives provider swaps."""

    name = "proxy"

    def __init__(self, services: Services) -> None:
        self._services = services

    @property
    def _inner(self) -> EmbeddingProvider:
        return self._services.embedder

    @property
    def dimension(self) -> int:
        return self._inner.dimension

    @property
    def model(self) -> str:  # type: ignore[override]
        return self._inner.model

    def encode(self, texts, *, is_query: bool = False):  # type: ignore[override]
        return self._inner.encode(texts, is_query=is_query)

    def describe(self) -> Dict[str, Any]:
        return self._inner.describe()


def _version() -> str:
    from . import __version__

    return __version__


def create_services(data_dir: Optional[str] = None,
                    settings: Optional[Settings] = None) -> Services:
    """Build the container from an optional explicit data directory."""
    paths, store = bootstrap_settings(data_dir)
    if settings is not None:
        store.replace(settings)
    return Services(paths, store)


def provider_presets() -> List[Dict[str, Any]]:
    return PROVIDER_PRESETS
