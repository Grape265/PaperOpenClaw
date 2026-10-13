"""LLM provider abstraction and prompt library."""

from __future__ import annotations

from .base import (
    Attachment,
    LLMError,
    LLMResult,
    Message,
    ToolSpec,
    Usage,
)
from .openai_compat import OpenAICompatProvider
from .registry import (
    KIND_CHAT,
    KIND_DIGEST,
    KIND_EMBEDDING,
    KIND_PARSE,
    ProviderRegistry,
)

__all__ = [
    "Attachment",
    "LLMError",
    "LLMResult",
    "Message",
    "ToolSpec",
    "Usage",
    "OpenAICompatProvider",
    "ProviderRegistry",
    "KIND_PARSE",
    "KIND_CHAT",
    "KIND_DIGEST",
    "KIND_EMBEDDING",
]
