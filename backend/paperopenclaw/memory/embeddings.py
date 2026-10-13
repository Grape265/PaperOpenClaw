"""Embedding providers with graceful degradation (FR-14.4, ADR-7, NFR-6).

Resolution order for ``provider='auto'``:

1. **local** - a Sentence-Transformers style model on disk. Offline, no data
   leaves the machine, best quality. Uses ``transformers`` + ``torch`` directly
   rather than ``sentence-transformers``: the extra package drags in a
   TensorFlow/Keras import chain that breaks on common installs, and BGE-style
   models need only CLS pooling plus L2 normalisation.
2. **remote** - an OpenAI-compatible ``/embeddings`` endpoint.
3. **lexical** - pure-Python feature hashing (:mod:`paperopenclaw.memory.lexical`).
   Always available, so indexing never fails outright.

The chosen provider and its dimension are recorded in ``embedding_meta`` so a
model swap is detectable instead of silently mixing vector spaces.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from ..config import EmbeddingConfig
from ..core.logging import get_logger
from .lexical import DEFAULT_DIM, hashed_embeddings

logger = get_logger(__name__)

# Must be set before `transformers` is imported, otherwise its lazy module
# loader pulls in TensorFlow and raises on Keras 3 installs.
os.environ.setdefault("USE_TF", "0")
os.environ.setdefault("USE_FLAX", "0")
os.environ.setdefault("TRANSFORMERS_NO_TF", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")


class EmbeddingProvider:
    """Common interface. Implementations must be safe to call from threads."""

    name = "base"
    model = ""

    @property
    def dimension(self) -> int:  # pragma: no cover - overridden
        raise NotImplementedError

    def encode(self, texts: Sequence[str], *, is_query: bool = False) -> List[List[float]]:
        raise NotImplementedError

    def encode_one(self, text: str, *, is_query: bool = False) -> List[float]:
        vectors = self.encode([text], is_query=is_query)
        return vectors[0] if vectors else []

    def describe(self) -> Dict[str, Any]:
        return {"provider": self.name, "model": self.model, "dimension": self.dimension}


class LexicalEmbedding(EmbeddingProvider):
    """Always-available fallback: signed feature hashing, no model, no network."""

    name = "lexical"

    def __init__(self, dimension: int = DEFAULT_DIM, model: str = "hashed-ngram") -> None:
        self._dimension = max(64, int(dimension or DEFAULT_DIM))
        self.model = model

    @property
    def dimension(self) -> int:
        return self._dimension

    def encode(self, texts: Sequence[str], *, is_query: bool = False) -> List[List[float]]:
        return hashed_embeddings(list(texts), dim=self._dimension, normalize=True)

    def describe(self) -> Dict[str, Any]:
        info = super().describe()
        info["offline"] = True
        return info


class LocalTransformerEmbedding(EmbeddingProvider):
    """Local model via ``transformers`` + ``torch`` with CLS pooling.

    BGE and most retrieval encoders are trained to put the sentence
    representation in the ``[CLS]`` token, so that is what we pool. The
    alternative (mean pooling) measurably degrades BGE retrieval.
    """

    name = "local"

    def __init__(
        self,
        model_path: str,
        *,
        dimension: int = 0,
        max_length: int = 512,
        batch_size: int = 32,
        device: str = "",
        query_prefix: str = "",
    ) -> None:
        self.model = Path(model_path).name or model_path
        self.model_path = str(model_path)
        self.max_length = int(max_length or 512)
        self.batch_size = max(1, int(batch_size or 32))
        self.query_prefix = query_prefix or ""
        self._dimension = int(dimension or 0)
        self._lock = threading.RLock()
        self._tokenizer = None
        self._model = None
        self._torch = None
        self._device = device or "cpu"

    # ------------------------------------------------------------------
    def _lazy_load(self) -> None:
        with self._lock:
            if self._model is not None:
                return
            import torch  # noqa: PLC0415 - deliberately lazy and heavy
            from transformers import AutoModel, AutoTokenizer  # noqa: PLC0415

            logger.info("加载本地嵌入模型：%s", self.model_path)
            self._tokenizer = AutoTokenizer.from_pretrained(self.model_path)
            self._model = AutoModel.from_pretrained(self.model_path)
            self._model.eval()
            if self._device == "auto":
                self._device = "cuda" if torch.cuda.is_available() else "cpu"
            try:
                self._model.to(self._device)
            except Exception:  # pragma: no cover - device mismatch
                self._device = "cpu"
                self._model.to("cpu")
            self._torch = torch
            if not self._dimension:
                config_dim = int(getattr(self._model.config, "hidden_size", 0) or 0)
                self._dimension = config_dim or DEFAULT_DIM
            logger.info("嵌入模型就绪：dim=%s device=%s", self._dimension, self._device)

    @property
    def dimension(self) -> int:
        if not self._dimension:
            self._lazy_load()
        return self._dimension

    # ------------------------------------------------------------------
    def encode(self, texts: Sequence[str], *, is_query: bool = False) -> List[List[float]]:
        if not texts:
            return []
        self._lazy_load()
        torch = self._torch
        assert torch is not None and self._tokenizer is not None and self._model is not None

        # BGE-zh and BGE-en use an instruction prefix only for queries.
        prepared = [
            (self.query_prefix + text) if (is_query and self.query_prefix) else (text or " ")
            for text in texts
        ]
        vectors: List[List[float]] = []
        with self._lock, torch.no_grad():
            for start in range(0, len(prepared), self.batch_size):
                batch = prepared[start : start + self.batch_size]
                encoded = self._tokenizer(
                    batch, padding=True, truncation=True, max_length=self.max_length,
                    return_tensors="pt",
                )
                encoded = {key: value.to(self._device) for key, value in encoded.items()}
                output = self._model(**encoded)
                hidden = output.last_hidden_state
                pooled = hidden[:, 0]  # CLS
                normalised = torch.nn.functional.normalize(pooled, p=2, dim=1)
                vectors.extend(normalised.detach().cpu().numpy().astype("float32").tolist())
        return vectors

    def describe(self) -> Dict[str, Any]:
        info = super().describe()
        info["path"] = self.model_path
        info["device"] = self._device
        info["offline"] = True
        return info


class RemoteEmbedding(EmbeddingProvider):
    """OpenAI-compatible ``/embeddings`` endpoint."""

    name = "remote"

    def __init__(self, client: Any, model: str = "", dimension: int = 0) -> None:
        self._client = client
        self.model = model or getattr(client, "model", "")
        self._dimension = int(dimension or 0)

    @property
    def dimension(self) -> int:
        if not self._dimension:
            probe = self.encode(["dimension probe"])
            self._dimension = len(probe[0]) if probe else DEFAULT_DIM
        return self._dimension

    def encode(self, texts: Sequence[str], *, is_query: bool = False) -> List[List[float]]:
        if not texts:
            return []
        vectors = self._client.embed(list(texts), model=self.model or None)
        if vectors and not self._dimension:
            self._dimension = len(vectors[0])
        return vectors


# ---------------------------------------------------------------------------
# factory
# ---------------------------------------------------------------------------

def _hf_hub_candidates(hub_dir: Path, model: str) -> List[str]:
    """Resolve a model inside a HuggingFace hub cache directory.

    The hub layout is ``<hub>/models--<org>--<name>/snapshots/<rev>/``. The
    configured model name is often the short form (``bge-small-zh-v1.5``) with no
    organisation, while the directory carries one (``models--BAAI--…``), so the
    match is done with a glob rather than by string construction. The revision
    directory is a hash and is discovered rather than assumed.
    """
    if not model or not hub_dir.is_dir():
        return []
    if "/" in model:
        patterns = [f"models--{model.replace('/', '--')}"]
    else:
        # Organisation-qualified first, then an unqualified folder.
        patterns = [f"models--*--{model}", f"models--{model}"]

    found: List[str] = []
    for pattern in patterns:
        for repo_dir in sorted(hub_dir.glob(pattern)):
            snapshots = repo_dir / "snapshots"
            if not snapshots.is_dir():
                continue
            for revision in sorted(snapshots.iterdir(), reverse=True):
                if revision.is_dir():
                    found.append(str(revision))
    return found


def _candidate_model_paths(config: EmbeddingConfig,
                           models_dirs: Optional[Sequence[Path]] = None) -> List[str]:
    """Where to look for a local model, most specific first.

    Several locations are searched because the model may have been placed by the
    user, downloaded by this app, or already present in the machine's HuggingFace
    cache — all three are common and requiring a specific one would make local
    embeddings look broken when they are merely elsewhere.
    """
    candidates: List[str] = []
    if config.model_path:
        candidates.append(config.model_path)

    name = config.model or "bge-small-zh-v1.5"
    for directory in models_dirs or ():
        if directory is None:
            continue
        root = Path(directory)
        candidates.append(str(root / name))
        # A hub-style cache nested under the models directory.
        candidates.extend(_hf_hub_candidates(root, name))
        candidates.extend(_hf_hub_candidates(root / "hub", name))

    if config.model:
        candidates.append(config.model)

    # De-duplicate while preserving order.
    seen: set = set()
    unique: List[str] = []
    for item in candidates:
        if item and item not in seen:
            seen.add(item)
            unique.append(item)
    return unique


def _looks_like_model_dir(path: str) -> bool:
    directory = Path(path)
    if not directory.is_dir():
        return False
    if not (directory / "config.json").is_file():
        # A hub snapshot can also be resolved via its revision directory; if the
        # caller passed the repo root instead, step into the snapshot.
        snapshots = directory / "snapshots"
        if snapshots.is_dir():
            for revision in snapshots.iterdir():
                if revision.is_dir() and (revision / "config.json").is_file():
                    return True
        return False
    has_weights = any(
        (directory / name).is_file()
        for name in ("model.safetensors", "pytorch_model.bin", "model.onnx")
    )
    has_tokenizer = any(
        (directory / name).is_file()
        for name in ("tokenizer.json", "vocab.txt", "spiece.model", "tokenizer.model")
    )
    return has_weights and has_tokenizer


def _resolve_model_dir(path: str) -> Optional[str]:
    """Return a directory that directly contains a loadable model."""
    directory = Path(path)
    if not directory.is_dir():
        return None
    if (directory / "config.json").is_file():
        return str(directory)
    snapshots = directory / "snapshots"
    if snapshots.is_dir():
        for revision in sorted(snapshots.iterdir(), reverse=True):
            if revision.is_dir() and (revision / "config.json").is_file():
                return str(revision)
    return None


def build_embedding_provider(
    config: EmbeddingConfig,
    *,
    models_dirs: Optional[Sequence[Path]] = None,
    models_dir: Optional[Path] = None,
    remote_client: Any = None,
) -> EmbeddingProvider:
    """Resolve the configured provider, degrading instead of failing.

    ``provider='auto'`` tries local, then remote, then lexical and logs which one
    won, so a user can always see why retrieval quality changed.
    """
    mode = (config.provider or "auto").lower()
    failures: List[str] = []

    search_dirs: List[Path] = []
    if models_dirs:
        search_dirs.extend(Path(item) for item in models_dirs if item is not None)
    if models_dir is not None:
        search_dirs.append(Path(models_dir))

    if mode in ("auto", "local"):
        for candidate in _candidate_model_paths(config, search_dirs):
            resolved = _resolve_model_dir(candidate)
            if resolved is None:
                continue
            try:
                provider = LocalTransformerEmbedding(
                    resolved,
                    dimension=config.dimension if mode == "local" else 0,
                    batch_size=config.batch_size,
                )
                provider.encode(["连接测试"])  # force a real load, not just a path check
                logger.info("嵌入提供方：local（%s）", resolved)
                return provider
            except Exception as exc:  # noqa: BLE001 - any failure means "try next"
                failures.append(f"local:{resolved}: {exc}")
                logger.warning("本地嵌入模型不可用（%s）：%s", resolved, exc)
        if mode == "local":
            logger.warning("已指定 local 嵌入但未找到可用模型，回退到远程或词法方案")

    if mode in ("auto", "remote") and remote_client is not None and (config.remote_model or config.model):
        try:
            provider = RemoteEmbedding(
                remote_client,
                model=config.remote_model or config.model,
                dimension=config.dimension,
            )
            provider.encode(["连接测试"])
            logger.info("嵌入提供方：remote（%s）", provider.model)
            return provider
        except Exception as exc:  # noqa: BLE001
            failures.append(f"remote: {exc}")
            logger.warning("远程嵌入不可用：%s", exc)
        if mode == "remote":
            logger.warning("已指定 remote 嵌入但调用失败，回退到词法方案")

    if failures:
        logger.warning("嵌入降级到离线词法方案；此前失败原因：%s", "；".join(failures[:3]))
    else:
        logger.info("嵌入提供方：lexical（离线词法，无需模型）")
    return LexicalEmbedding(dimension=config.dimension or DEFAULT_DIM)
