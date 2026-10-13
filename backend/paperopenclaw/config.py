"""Application settings: providers, model routing, ingest and retrieval tuning.

Settings live in ``<data_dir>/config.json``. The data directory itself is
resolved from CLI/env/default (see :func:`paperopenclaw.core.paths.resolve_data_dir`)
so there is no chicken-and-egg problem; the file records it for display only.

Secrets never leave the process in clear text: :meth:`Settings.masked_dict`
replaces API keys with ``sk-…abcd`` style masks for the frontend (FR-14.3).
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator

from .core.paths import DataPaths, resolve_data_dir

CONFIG_VERSION = 1

# ---------------------------------------------------------------------------
# provider presets - offered in the settings UI for one-click setup
# ---------------------------------------------------------------------------

PROVIDER_PRESETS: List[Dict[str, Any]] = [
    {
        "id": "deepseek",
        "name": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "models": ["deepseek-chat", "deepseek-reasoner"],
        "vision": False,
        "notes": "文本能力强、价格低；不支持 PDF 图像直读，需搭配支持视觉的提供方做解析。",
    },
    {
        "id": "dashscope",
        "name": "阿里云百炼（通义千问）",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "models": ["qwen-vl-max", "qwen-vl-plus", "qwen-max", "qwen-plus", "text-embedding-v3"],
        "vision": True,
        "notes": "qwen-vl 系列支持图片输入，适合承担 PDF 解析。",
    },
    {
        "id": "moonshot",
        "name": "Moonshot（Kimi）",
        "base_url": "https://api.moonshot.cn/v1",
        "models": ["moonshot-v1-128k-vision-preview", "moonshot-v1-128k", "kimi-latest"],
        "vision": True,
        "notes": "长上下文，vision-preview 可用于解析。",
    },
    {
        "id": "zhipu",
        "name": "智谱 GLM",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "models": ["glm-4v-plus", "glm-4-plus", "embedding-3"],
        "vision": True,
        "notes": "glm-4v 支持图像输入。",
    },
    {
        "id": "siliconflow",
        "name": "SiliconFlow（硅基流动）",
        "base_url": "https://api.siliconflow.cn/v1",
        "models": ["Qwen/Qwen2.5-VL-72B-Instruct", "deepseek-ai/DeepSeek-V3", "BAAI/bge-m3"],
        "vision": True,
        "notes": "同时提供对话、视觉与嵌入模型，可一处配齐。",
    },
    {
        "id": "openai",
        "name": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "models": ["gpt-4o", "gpt-4o-mini", "text-embedding-3-small"],
        "vision": True,
        "notes": "gpt-4o 系列原生支持 PDF/图片输入。",
    },
    {
        "id": "ollama",
        "name": "Ollama（本地）",
        "base_url": "http://127.0.0.1:11434/v1",
        "models": ["qwen2.5vl:7b", "llama3.1:8b", "nomic-embed-text"],
        "vision": True,
        "notes": "本地推理，无需 API Key；需先 ollama pull 对应模型。",
    },
]


# ---------------------------------------------------------------------------
# models
# ---------------------------------------------------------------------------

class ProviderConfig(BaseModel):
    """An OpenAI-compatible endpoint."""

    id: str
    name: str = ""
    base_url: str
    api_key: str = ""
    models: List[str] = Field(default_factory=list)
    vision: bool = False
    timeout: float = 600.0
    max_retries: int = 2
    extra_headers: Dict[str, str] = Field(default_factory=dict)
    enabled: bool = True

    @field_validator("base_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return (value or "").strip().rstrip("/")


class RouteConfig(BaseModel):
    """Which provider/model serves one capability."""

    provider: str = ""
    model: str = ""
    temperature: float = 0.2
    max_tokens: int = 8192

    @property
    def configured(self) -> bool:
        return bool(self.provider and self.model)


class EmbeddingConfig(BaseModel):
    """Embedding provider selection (FR-14.4, ADR-7).

    ``provider`` is one of:
      ``local``   - sentence-transformers model resolved from a local path or id
      ``remote``  - OpenAI-compatible ``/embeddings`` endpoint
      ``lexical`` - offline hashed n-gram vectors, needs no model at all
      ``auto``    - try local, then remote, then lexical
    """

    provider: str = "auto"
    model: str = "bge-small-zh-v1.5"
    model_path: str = ""
    dimension: int = 512
    batch_size: int = 32
    base_url: str = ""
    api_key: str = ""
    remote_model: str = ""
    normalize: bool = True

    @field_validator("provider")
    @classmethod
    def _known_provider(cls, value: str) -> str:
        allowed = {"auto", "local", "remote", "lexical"}
        text = (value or "auto").strip().lower()
        return text if text in allowed else "auto"


class IngestConfig(BaseModel):
    """Ingest pipeline tuning."""

    # How the PDF reaches the model. "image" renders pages to PNG and sends them
    # (the default, per requirement); "file" sends the document itself; "auto"
    # prefers PNG images and falls back to the native file when a provider cannot
    # accept images. When the model call fails entirely, the pipeline falls back
    # to a local PyMuPDF extraction.
    pdf_mode: str = "image"
    vision_batch_pages: int = 20
    image_dpi: int = 200
    render_dpi: int = 150
    max_image_edge: int = 2200
    image_format: str = "png"
    max_retry: int = 2
    max_pages: int = 400
    render_scale: float = 2.0
    min_asset_area_ratio: float = 0.012
    caption_search_window: int = 90
    # Ask the provider for a JSON response format. Off by default: several
    # OpenAI-compatible gateways reject the field outright.
    use_json_mode: bool = False
    keep_page_images: bool = False
    digest_max_chars_a: int = 1400
    digest_max_chars_b: int = 600
    max_concurrent_jobs: int = 1


class RetrievalConfig(BaseModel):
    """Cascade retrieval tuning (FR-11.2)."""

    top_k_c: int = 5
    top_k_b: int = 8
    top_k_a: int = 8
    min_score: float = 0.22
    rrf_k: int = 60
    vector_weight: float = 0.6
    lexical_weight: float = 0.4
    max_candidates: int = 200
    cross_link_top_k: int = 5
    cross_link_min_score: float = 0.45
    stop_at_b: bool = False


class AgentConfig(BaseModel):
    """Agent runtime tuning (FR-10, FR-11)."""

    max_steps: int = 8
    temperature: float = 0.3
    max_tokens: int = 4096
    brainstorm_rounds: int = 2
    brainstorm_max_agents: int = 6
    tool_timeout: float = 120.0
    request_timeout: float = 600.0
    max_context_chars: int = 24000


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8787
    cors_origins: List[str] = Field(default_factory=lambda: ["*"])
    auth_token: str = ""
    open_browser: bool = True


class Settings(BaseModel):
    """Root settings document."""

    version: int = CONFIG_VERSION
    data_dir: str = ""
    providers: List[ProviderConfig] = Field(default_factory=list)
    routing: Dict[str, RouteConfig] = Field(
        default_factory=lambda: {
            "parse": RouteConfig(temperature=0.0, max_tokens=16000),
            "chat": RouteConfig(temperature=0.3, max_tokens=4096),
            "digest": RouteConfig(temperature=0.2, max_tokens=4096),
        }
    )
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    ingest: IngestConfig = Field(default_factory=IngestConfig)
    retrieval: RetrievalConfig = Field(default_factory=RetrievalConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)

    # ------------------------------------------------------------------
    # lookups
    # ------------------------------------------------------------------
    def provider(self, provider_id: str) -> Optional[ProviderConfig]:
        for item in self.providers:
            if item.id == provider_id:
                return item
        return None

    def route(self, kind: str) -> RouteConfig:
        return self.routing.get(kind) or RouteConfig()

    def resolved_route(self, kind: str):
        """Return ``(provider, route)`` or ``(None, route)`` when unconfigured."""
        route = self.route(kind)
        if not route.configured:
            return None, route
        provider = self.provider(route.provider)
        if provider is None or not provider.enabled:
            return None, route
        return provider, route

    def has_any_provider(self) -> bool:
        return any(p.enabled and p.api_key or (p.enabled and p.base_url.startswith("http://127.0.0.1")) for p in self.providers)

    # ------------------------------------------------------------------
    # masking / serialisation
    # ------------------------------------------------------------------
    @staticmethod
    def mask_secret(value: str) -> str:
        if not value:
            return ""
        if len(value) <= 8:
            return "****"
        return f"{value[:3]}…{value[-4:]}"

    def masked_dict(self) -> Dict[str, Any]:
        payload = self.model_dump()
        payload["data_dir"] = self.data_dir
        for provider in payload.get("providers", []):
            provider["api_key"] = self.mask_secret(provider.get("api_key", ""))
            provider["api_key_set"] = bool(provider.get("api_key"))
        payload["embedding"]["api_key"] = self.mask_secret(self.embedding.api_key)
        payload["embedding"]["api_key_set"] = bool(self.embedding.api_key)
        payload["server"]["auth_token"] = self.mask_secret(self.server.auth_token)
        return payload

    def apply_patch(self, patch: Dict[str, Any]) -> "Settings":
        """Merge a partial update, preserving secrets that arrive masked.

        The frontend echoes back the masked key it received; treating that as a
        real value would silently destroy the credential.
        """
        data = self.model_dump()
        patch = dict(patch or {})

        incoming_providers = patch.pop("providers", None)
        if isinstance(incoming_providers, list):
            existing = {p["id"]: p for p in data.get("providers", [])}
            merged: List[Dict[str, Any]] = []
            for item in incoming_providers:
                if not isinstance(item, dict):
                    continue
                pid = item.get("id") or ""
                base = dict(existing.get(pid, {}))
                for key, value in item.items():
                    if key in ("api_key_set",):
                        continue
                    if key == "api_key":
                        if value in (None, "") and base.get("api_key"):
                            continue  # keep existing secret
                        if isinstance(value, str) and ("…" in value or value == "****"):
                            continue  # masked echo, keep existing secret
                    base[key] = value
                merged.append(base)
            data["providers"] = merged

        for section in ("embedding",):
            incoming = patch.get(section)
            if isinstance(incoming, dict):
                base = dict(data.get(section) or {})
                for key, value in incoming.items():
                    if key in ("api_key_set",):
                        continue
                    if key == "api_key":
                        if value in (None, "") and base.get("api_key"):
                            continue
                        if isinstance(value, str) and ("…" in value or value == "****"):
                            continue
                    base[key] = value
                data[section] = base
                patch.pop(section, None)

        for key, value in patch.items():
            if isinstance(value, dict) and isinstance(data.get(key), dict):
                merged_section = dict(data[key])
                merged_section.update(value)
                data[key] = merged_section
            else:
                data[key] = value
        return Settings.model_validate(data)


# ---------------------------------------------------------------------------
# persistence
# ---------------------------------------------------------------------------

_config_lock = threading.RLock()


def default_settings(data_dir: Path) -> Settings:
    """Fresh settings with sensible, immediately-usable defaults."""
    return Settings(
        data_dir=str(data_dir),
        providers=[],
        routing={
            "parse": RouteConfig(temperature=0.0, max_tokens=16000),
            "chat": RouteConfig(temperature=0.3, max_tokens=4096),
            "digest": RouteConfig(temperature=0.2, max_tokens=4096),
        },
    )


def load_settings(paths: DataPaths) -> Settings:
    """Load settings from disk, creating defaults when absent or corrupt."""
    with _config_lock:
        config_file = paths.config_file
        if not config_file.is_file():
            settings = default_settings(paths.root)
            save_settings(paths, settings)
            return settings
        try:
            raw = json.loads(config_file.read_text(encoding="utf-8"))
            settings = Settings.model_validate(raw)
        except Exception:
            # Corrupt config must not brick the app: keep a backup and restart.
            try:
                backup = config_file.with_suffix(".json.broken")
                config_file.replace(backup)
            except Exception:
                pass
            settings = default_settings(paths.root)
            save_settings(paths, settings)
            return settings
        settings.data_dir = str(paths.root)
        return settings


def save_settings(paths: DataPaths, settings: Settings) -> Settings:
    """Persist settings atomically with restrictive permissions on Unix."""
    with _config_lock:
        settings.data_dir = str(paths.root)
        payload = settings.model_dump()
        payload["version"] = CONFIG_VERSION
        config_file = paths.config_file
        config_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = config_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(str(tmp), str(config_file))
        try:
            if os.name != "nt":
                os.chmod(config_file, 0o600)
        except Exception:  # pragma: no cover - platform dependent
            pass
        return settings


class SettingsStore:
    """Thread-safe holder so request handlers share one live Settings object."""

    def __init__(self, paths: DataPaths, settings: Optional[Settings] = None) -> None:
        self.paths = paths
        self._settings = settings or load_settings(paths)

    @property
    def current(self) -> Settings:
        with _config_lock:
            return self._settings

    def reload(self) -> Settings:
        with _config_lock:
            self._settings = load_settings(self.paths)
            return self._settings

    def update(self, patch: Dict[str, Any]) -> Settings:
        with _config_lock:
            self._settings = self._settings.apply_patch(patch)
            save_settings(self.paths, self._settings)
            return self._settings

    def replace(self, settings: Settings) -> Settings:
        with _config_lock:
            self._settings = settings
            save_settings(self.paths, settings)
            return self._settings


def bootstrap_settings(explicit_data_dir: Optional[str] = None) -> "tuple[DataPaths, SettingsStore]":
    """Resolve the data directory, create it, and load settings."""
    root = resolve_data_dir(explicit_data_dir)
    paths = DataPaths(root).ensure_base()
    return paths, SettingsStore(paths)
