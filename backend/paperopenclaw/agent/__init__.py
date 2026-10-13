"""Agent layer: runtime, tools, skills, isolated sub-agents and workflows."""

from __future__ import annotations

from .context import ContextEngine
from .profiles import (
    AgentProfile,
    DEFAULT_BRAINSTORM_ROSTER,
    PROFILES,
    get_profile,
    list_profiles,
    resolve_roster,
)
from .runtime import AgentRuntime, AgentTurn
from .skill_loader import SkillLoader, SkillManifest, parse_manifest
from .tools import Tool, ToolContext, ToolRegistry, ToolResult, build_default_registry

__all__ = [
    "ContextEngine",
    "AgentProfile",
    "DEFAULT_BRAINSTORM_ROSTER",
    "PROFILES",
    "get_profile",
    "list_profiles",
    "resolve_roster",
    "AgentRuntime",
    "AgentTurn",
    "SkillLoader",
    "SkillManifest",
    "parse_manifest",
    "Tool",
    "ToolContext",
    "ToolRegistry",
    "ToolResult",
    "build_default_registry",
]
