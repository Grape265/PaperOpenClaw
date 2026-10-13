"""System endpoints: health, settings, provider testing, diagnostics."""

from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, Depends, Request

from ...config import PROVIDER_PRESETS
from ...core.errors import BadRequest, ProviderNotConfigured
from ...llm.base import LLMError
from ...llm.registry import KIND_CHAT, KIND_EMBEDDING, KIND_PARSE
from ...services import Services
from ..deps import get_services
from ..responses import success
from ..schemas import ProviderTestRequest, SettingsPatch

router = APIRouter(tags=["system"])


@router.get("/health")
def health(request: Request, services: Services = Depends(get_services)) -> Dict[str, Any]:
    return success(services.health(), request)


@router.get("/doctor")
def doctor(request: Request, services: Services = Depends(get_services)) -> Dict[str, Any]:
    return success(services.doctor(), request)


@router.get("/provider-presets")
def provider_presets(request: Request) -> Dict[str, Any]:
    """Ready-made provider configurations offered by the settings page."""
    return success({"items": PROVIDER_PRESETS}, request)


@router.get("/settings")
def get_settings(request: Request, services: Services = Depends(get_services)) -> Dict[str, Any]:
    return success(services.settings.current.masked_dict(), request)


@router.put("/settings")
def update_settings(request: Request, patch: SettingsPatch,
                    services: Services = Depends(get_services)) -> Dict[str, Any]:
    payload = patch.model_dump(exclude_unset=True)
    if not payload:
        raise BadRequest("没有需要更新的字段")

    before_embedding = services.settings.current.embedding.model_dump()
    updated = services.settings.update(payload)
    services.registry.invalidate()

    # A changed embedding configuration invalidates cached vectors' meaning, so
    # the provider is rebuilt and the caller is told a rebuild is advisable.
    rebuild_advised = before_embedding != updated.embedding.model_dump()
    if rebuild_advised:
        services.refresh_embedder()

    return success(
        {"settings": updated.masked_dict(), "embedding_changed": rebuild_advised},
        request,
        meta={"rebuild_advised": rebuild_advised},
    )


@router.post("/settings/test")
def test_provider(request: Request, body: ProviderTestRequest,
                  services: Services = Depends(get_services)) -> Dict[str, Any]:
    """Probe one provider/model combination without saving anything."""
    settings = services.settings.current
    provider = settings.provider(body.provider)
    if provider is None:
        raise ProviderNotConfigured(f"提供方不存在：{body.provider}", provider=body.provider)

    kind = (body.kind or "chat").lower()
    model = body.model
    if kind == KIND_EMBEDDING:
        model = model or settings.embedding.remote_model or settings.embedding.model
        client = services.registry.client_for(body.provider, model)
        try:
            detail = client.probe(model=model, kind="embedding")
        except LLMError as exc:
            raise ProviderNotConfigured(str(exc), provider=body.provider) from exc
        return success({"provider": body.provider, "model": model, "kind": kind, **detail}, request)

    if kind not in (KIND_CHAT, KIND_PARSE):
        raise BadRequest(f"未知的测试类型：{body.kind}")
    model = model or (provider.models[0] if provider.models else "")
    if not model:
        raise BadRequest("请指定要测试的模型名称")

    client = services.registry.client_for(body.provider, model)
    try:
        if kind == KIND_PARSE:
            # 解析路由要求模型能读「PDF 转成的图片」，用一张极小 PNG 实测。
            detail = client.probe_vision(model=model)
        else:
            detail = client.probe(model=model, kind="chat")
    except LLMError as exc:
        raise ProviderNotConfigured(str(exc), provider=body.provider) from exc
    return success({"provider": body.provider, "model": model, "kind": kind, **detail}, request)


@router.get("/providers/{provider_id}/models")
def list_models(request: Request, provider_id: str,
                services: Services = Depends(get_services)) -> Dict[str, Any]:
    """Ask a provider which models it exposes (best effort)."""
    settings = services.settings.current
    provider = settings.provider(provider_id)
    if provider is None:
        raise ProviderNotConfigured(f"提供方不存在：{provider_id}", provider=provider_id)
    client = services.registry.client_for(provider_id, provider.models[0] if provider.models else "")
    return success({"provider": provider_id, "models": client.list_models()}, request)
