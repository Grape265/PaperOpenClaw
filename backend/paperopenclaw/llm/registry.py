"""Provider registry: turns settings into ready-to-use clients.

Three capabilities are routed independently (FR-14.2):

    parse     - must accept PDF/image input; drives the ingest pipeline
    chat      - question answering and agent reasoning
    digest    - pyramid condensation (cheap, high-volume, JSON-ish)
    embedding - vectorisation, which may be local, remote or lexical

Separating them matters because the best PDF reader is often not the cheapest
summariser, and a local embedding model avoids sending the whole corpus to a
third party.
"""

from __future__ import annotations

import threading
from typing import Any, Dict, Optional, Tuple

from ..config import ProviderConfig, RouteConfig, Settings, SettingsStore
from ..core.errors import ProviderNotConfigured
from ..core.logging import get_logger
from .base import LLMError
from .openai_compat import OpenAICompatProvider

logger = get_logger(__name__)

KIND_PARSE = "parse"
KIND_CHAT = "chat"
KIND_DIGEST = "digest"
KIND_EMBEDDING = "embedding"
KINDS = (KIND_PARSE, KIND_CHAT, KIND_DIGEST, KIND_EMBEDDING)


class ProviderRegistry:
    """Builds and caches providers from the live settings."""

    def __init__(self, store: SettingsStore) -> None:
        self.store = store
        self._lock = threading.RLock()
        self._cache: Dict[Tuple[str, str, str], OpenAICompatProvider] = {}

    # ------------------------------------------------------------------
    # construction
    # ------------------------------------------------------------------
    def _build(self, provider: ProviderConfig, model: str) -> OpenAICompatProvider:
        key = (provider.id, provider.base_url, model)
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None and cached.api_key == provider.api_key:
                return cached
            client = OpenAICompatProvider(
                base_url=provider.base_url,
                api_key=provider.api_key,
                model=model,
                timeout=provider.timeout,
                max_retries=provider.max_retries,
                extra_headers=provider.extra_headers,
                provider_id=provider.id,
            )
            self._cache[key] = client
            return client

    def invalidate(self) -> None:
        with self._lock:
            self._cache.clear()

    # ------------------------------------------------------------------
    # routing
    # ------------------------------------------------------------------
    def _resolve(self, kind: str) -> Tuple[ProviderConfig, RouteConfig]:
        settings = self.store.current
        provider, route = settings.resolved_route(kind)
        if provider is None:
            if not route.configured:
                raise ProviderNotConfigured(
                    f"尚未配置「{kind}」所用的提供方与模型，请先在设置页填写。",
                    kind=kind,
                )
            raise ProviderNotConfigured(
                f"提供方「{route.provider}」不存在或已停用。", kind=kind, provider=route.provider
            )
        return provider, route

    def client(self, kind: str) -> OpenAICompatProvider:
        """Return a provider for ``kind``, raising when unconfigured."""
        provider, route = self._resolve(kind)
        return self._build(provider, route.model)

    def client_for(self, provider_id: str, model: str) -> OpenAICompatProvider:
        """Explicit selection, used by per-agent model overrides (FR-10.3)."""
        settings = self.store.current
        provider = settings.provider(provider_id)
        if provider is None or not provider.enabled:
            raise ProviderNotConfigured(f"提供方不存在或已停用：{provider_id}", provider=provider_id)
        return self._build(provider, model or (provider.models[0] if provider.models else ""))

    def route_config(self, kind: str) -> RouteConfig:
        return self.store.current.route(kind)

    def try_client(self, kind: str) -> Optional[OpenAICompatProvider]:
        """Non-raising variant for optional capabilities."""
        try:
            return self.client(kind)
        except ProviderNotConfigured:
            return None

    # ------------------------------------------------------------------
    # capability probe (used by /health and /doctor)
    # ------------------------------------------------------------------
    def status(self) -> Dict[str, Any]:
        settings = self.store.current
        result: Dict[str, Any] = {}
        for kind in KINDS:
            if kind == KIND_EMBEDDING:
                cfg = settings.embedding
                result[kind] = {
                    "configured": True,
                    "provider": cfg.provider,
                    "model": cfg.remote_model or cfg.model,
                }
                continue
            route = settings.route(kind)
            provider = settings.provider(route.provider) if route.provider else None
            result[kind] = {
                "configured": bool(provider and route.model),
                "provider": route.provider,
                "model": route.model,
                "vision": bool(provider.vision) if provider else False,
            }
        return result
