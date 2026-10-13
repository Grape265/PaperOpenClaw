"""Tool registry.

A tool is a named, schema-described capability the model may call. The registry
is the single place that knows what exists, and the single place that enforces
the per-agent allowlist — which is what makes tool isolation real rather than
advisory (FR-10.2).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Set

from ...core.logging import get_logger
from ...llm.base import ToolSpec

logger = get_logger(__name__)

ToolHandler = Callable[["ToolContext", Dict[str, Any]], Dict[str, Any]]


@dataclass
class ToolContext:
    """Everything a tool may touch, scoped to one agent invocation.

    The scope fields (``allowed_hashes``, ``workspace``, ``allowed_tools``) are
    populated from the agent row, so a tool physically cannot read a paper the
    agent was not granted.
    """

    services: Any
    session_id: str = ""
    agent_id: str = ""
    allowed_hashes: Optional[List[str]] = None
    allowed_tools: Optional[Set[str]] = None
    workspace: Optional[Any] = None
    round_index: int = 1
    meta: Dict[str, Any] = field(default_factory=dict)

    def scope(self, hashes: Optional[Sequence[str]] = None) -> Optional[List[str]]:
        """Intersect a requested paper scope with what this agent may see."""
        if self.allowed_hashes is None:
            return list(hashes) if hashes else None
        granted = set(self.allowed_hashes)
        if not hashes:
            return sorted(granted)
        return [item for item in hashes if item in granted]


@dataclass
class Tool:
    """A callable capability."""

    name: str
    description: str
    parameters: Dict[str, Any]
    handler: ToolHandler
    source: str = "builtin"

    def spec(self) -> ToolSpec:
        return ToolSpec(name=self.name, description=self.description, parameters=self.parameters)


@dataclass
class ToolResult:
    ok: bool
    payload: Any
    error: str = ""
    elapsed_ms: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {"ok": self.ok, "payload": self.payload, "error": self.error,
                "elapsed_ms": self.elapsed_ms}

    def as_text(self, max_chars: int = 12000) -> str:
        """Serialise for the model. Errors are returned as text, not raised."""
        import json

        if not self.ok:
            return json.dumps({"error": self.error or "工具执行失败"}, ensure_ascii=False)
        try:
            text = json.dumps(self.payload, ensure_ascii=False, default=str)
        except Exception:  # pragma: no cover - defensive
            text = str(self.payload)
        return text if len(text) <= max_chars else text[:max_chars] + "…（截断）"


class ToolRegistry:
    """Holds every registered tool and dispatches calls."""

    def __init__(self) -> None:
        self._tools: Dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            logger.debug("覆盖同名工具：%s", tool.name)
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def unregister_source(self, source: str) -> None:
        for name in [key for key, tool in self._tools.items() if tool.source == source]:
            self._tools.pop(name, None)

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def names(self) -> List[str]:
        return sorted(self._tools)

    def list(self, allowed: Optional[Set[str]] = None) -> List[Tool]:
        tools = [self._tools[name] for name in sorted(self._tools)]
        if allowed is None:
            return tools
        return [tool for tool in tools if tool.name in allowed]

    def specs(self, allowed: Optional[Set[str]] = None) -> List[ToolSpec]:
        return [tool.spec() for tool in self.list(allowed)]

    def is_allowed(self, name: str, allowed: Optional[Set[str]]) -> bool:
        if allowed is None:
            return name in self._tools
        return name in allowed and name in self._tools

    def invoke(self, name: str, context: ToolContext, arguments: Dict[str, Any]) -> ToolResult:
        """Run a tool, enforcing the allowlist and never raising."""
        started = time.monotonic()
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(False, None, f"未知工具：{name}")
        if not self.is_allowed(name, context.allowed_tools):
            logger.warning("智能体 %s 尝试调用未授权工具 %s", context.agent_id, name)
            return ToolResult(False, None, f"该智能体无权调用工具：{name}")
        try:
            payload = tool.handler(context, dict(arguments or {}))
            elapsed = int((time.monotonic() - started) * 1000)
            return ToolResult(True, payload, "", elapsed)
        except Exception as exc:  # noqa: BLE001 - tool errors are data, not crashes
            elapsed = int((time.monotonic() - started) * 1000)
            logger.warning("工具 %s 执行失败：%s", name, exc)
            return ToolResult(False, None, f"{type(exc).__name__}: {exc}", elapsed)


def build_default_registry() -> ToolRegistry:
    """Registry preloaded with the built-in tools."""
    from .builtin import register_builtin_tools

    registry = ToolRegistry()
    register_builtin_tools(registry)
    return registry


__all__ = [
    "Tool",
    "ToolContext",
    "ToolRegistry",
    "ToolResult",
    "build_default_registry",
]
