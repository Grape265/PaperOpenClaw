"""LLM provider abstraction.

Everything above this package talks in :class:`Message` / :class:`ToolSpec` /
:class:`LLMResult`; only :mod:`openai_compat` knows about wire formats. That
keeps the ingest pipeline and the agent runtime provider-agnostic, which is what
lets a user point at DeepSeek, DashScope, Moonshot, OpenAI or a local Ollama
with nothing but configuration (FR-14.1).
"""

from __future__ import annotations

import base64
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence


# ---------------------------------------------------------------------------
# content building blocks
# ---------------------------------------------------------------------------

@dataclass
class Attachment:
    """A non-text part of a message: a rendered page image or a whole PDF."""

    kind: str                      # "image" | "file"
    data: bytes = b""
    mime: str = "image/png"
    filename: str = ""
    detail: str = "high"
    page: int = 0

    def to_data_url(self) -> str:
        encoded = base64.b64encode(self.data).decode("ascii")
        return f"data:{self.mime};base64,{encoded}"

    def to_content_part(self) -> Dict[str, Any]:
        """OpenAI-compatible content part."""
        if self.kind == "image":
            return {"type": "image_url", "image_url": {"url": self.to_data_url(), "detail": self.detail}}
        return {
            "type": "file",
            "file": {"filename": self.filename or "document.pdf", "file_data": self.to_data_url()},
        }


@dataclass
class Message:
    """One conversation turn."""

    role: str                                   # system | user | assistant | tool
    content: str = ""
    attachments: List[Attachment] = field(default_factory=list)
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    tool_call_id: str = ""
    name: str = ""

    def to_wire(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"role": self.role}
        if self.attachments:
            parts: List[Dict[str, Any]] = []
            if self.content:
                parts.append({"type": "text", "text": self.content})
            parts.extend(item.to_content_part() for item in self.attachments)
            payload["content"] = parts
        else:
            payload["content"] = self.content
        if self.tool_calls:
            payload["tool_calls"] = self.tool_calls
        if self.tool_call_id:
            payload["tool_call_id"] = self.tool_call_id
        if self.name:
            payload["name"] = self.name
        return payload

    @classmethod
    def system(cls, content: str) -> "Message":
        return cls(role="system", content=content)

    @classmethod
    def user(cls, content: str, attachments: Optional[Sequence[Attachment]] = None) -> "Message":
        return cls(role="user", content=content, attachments=list(attachments or []))

    @classmethod
    def assistant(cls, content: str, tool_calls: Optional[Sequence[Dict[str, Any]]] = None) -> "Message":
        return cls(role="assistant", content=content, tool_calls=list(tool_calls or []))

    @classmethod
    def tool_result(cls, tool_call_id: str, name: str, content: str) -> "Message":
        return cls(role="tool", content=content, tool_call_id=tool_call_id, name=name)


@dataclass
class ToolSpec:
    """A callable the model may invoke."""

    name: str
    description: str
    parameters: Dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {}})

    def to_wire(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    calls: int = 0

    def merge(self, other: "Usage") -> "Usage":
        return Usage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
            calls=self.calls + other.calls,
        )

    def to_dict(self) -> Dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "calls": self.calls,
        }

    @classmethod
    def from_wire(cls, payload: Optional[Dict[str, Any]]) -> "Usage":
        payload = payload or {}
        prompt = int(payload.get("prompt_tokens") or 0)
        completion = int(payload.get("completion_tokens") or 0)
        return cls(
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=int(payload.get("total_tokens") or (prompt + completion)),
            calls=1,
        )


@dataclass
class LLMResult:
    """What a provider returns for one completion."""

    text: str = ""
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    finish_reason: str = ""
    model: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)
    elapsed_ms: int = 0

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)


class LLMError(RuntimeError):
    """Provider-side failure; the caller decides whether to retry."""

    def __init__(self, message: str, *, status: int = 0, body: str = "", retryable: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.body = body
        self.retryable = retryable


def elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
