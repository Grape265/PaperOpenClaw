"""Agent runtime: the reasoning loop and its two public entry points.

The loop is the OpenClaw-style core: assemble context, call the model, execute
any requested tools, feed results back, repeat until the model stops asking for
tools or the step budget is exhausted. Two properties matter operationally:

* a tool failure is **data, not an exception** — it is fed back so the model can
  adapt, which is what keeps a flaky tool from ending a session;
* every step is bounded (``max_steps``) and every tool obeys the agent's
  allowlist and paper scope, so isolation is enforced by the runtime rather than
  requested in the prompt.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

from ..core.errors import ProviderNotConfigured
from ..core.logging import get_logger
from ..llm.base import LLMError, Message, ToolSpec, Usage
from ..llm.registry import KIND_CHAT, ProviderRegistry
from .context import ContextEngine
from .profiles import AgentProfile, get_profile, list_profiles, resolve_roster
from .tools import ToolContext, ToolRegistry, ToolResult, build_default_registry

logger = get_logger(__name__)

StepCallback = Optional[Callable[[Dict[str, Any]], None]]


@dataclass
class AgentTurn:
    """Result of one agent loop run."""

    text: str = ""
    steps: int = 0
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    messages: List[Dict[str, Any]] = field(default_factory=list)
    error: str = ""
    elapsed_ms: int = 0


class AgentRuntime:
    """Facade over the tool registry, context engine and the two workflows."""

    def __init__(self, services: Any) -> None:
        self.services = services
        self.registry: ToolRegistry = build_default_registry()
        self.context = ContextEngine(services)
        # Pick up any skills already on disk before the first request.
        try:
            self.services.skill_loader.scan(register_tools=True, registry=self.registry)
        except Exception as exc:  # noqa: BLE001 - skills are optional
            logger.warning("启动时扫描技能失败：%s", exc)

    # ------------------------------------------------------------------
    # catalogue helpers
    # ------------------------------------------------------------------
    def reload_skills(self) -> int:
        return self.services.skill_loader.apply_tools(self.registry)

    def tool_catalogue(self) -> List[Dict[str, Any]]:
        rows = self.services.skills.list()
        source_names = {f"skill:{row['id']}": row["name"] for row in rows}
        items = []
        for tool in self.registry.list():
            items.append({
                "name": tool.name,
                "description": tool.description,
                "source": tool.source,
                "source_name": source_names.get(tool.source, "内置工具"),
                "params_schema": tool.parameters,
            })
        return items

    def profiles(self) -> List[Dict[str, Any]]:
        return list_profiles()

    # ------------------------------------------------------------------
    # the loop
    # ------------------------------------------------------------------
    def run_loop(
        self,
        profile: AgentProfile,
        *,
        task: str,
        tool_context: ToolContext,
        extra_context: str = "",
        role_note: str = "",
        provider_id: str = "",
        model: str = "",
        temperature: Optional[float] = None,
        max_steps: Optional[int] = None,
        max_tokens: Optional[int] = None,
        on_step: StepCallback = None,
    ) -> AgentTurn:
        """Run one tool-using agent to completion."""
        started = time.monotonic()
        settings = self.services.settings.current
        agent_config = settings.agent
        steps_allowed = int(max_steps or agent_config.max_steps)

        if provider_id and model:
            client = self.services.registry.client_for(provider_id, model)
        else:
            client = self.services.registry.try_client(KIND_CHAT)
            if client is None:
                # Fall back to the parse route so a chat-only install still works.
                client = self.services.registry.try_client("parse")
            if client is None:
                raise ProviderNotConfigured("尚未配置问答模型，请先在设置页填写。", kind="chat")

        allowed = tool_context.allowed_tools
        specs: List[ToolSpec] = self.registry.specs(allowed)
        skills_text = self.services.skill_loader.enabled_instructions()

        system_prompt = self.context.build_system_prompt(
            profile,
            tool_specs=specs,
            skills_instructions=skills_text,
            paper_scope=tool_context.allowed_hashes,
            role_note=role_note,
        )
        messages: List[Message] = [Message.system(system_prompt)]
        if extra_context.strip():
            messages.append(Message.system(extra_context.strip()))
        messages.append(Message.user(task))

        turn = AgentTurn()
        usage = Usage()
        transcript: List[Dict[str, Any]] = []

        for step in range(1, steps_allowed + 1):
            turn.steps = step
            try:
                result = client.chat(
                    messages,
                    temperature=agent_config.temperature if temperature is None else temperature,
                    max_tokens=max_tokens or agent_config.max_tokens,
                    tools=specs or None,
                )
            except LLMError as exc:
                turn.error = str(exc)
                turn.text = _fallback_text(exc)
                logger.warning("智能体 %s 调用模型失败：%s", profile.id, exc)
                break

            usage = usage.merge(result.usage)

            if not result.has_tool_calls:
                turn.text = (result.text or "").strip()
                transcript.append({"step": step, "type": "final", "text": turn.text})
                if on_step:
                    on_step({"step": step, "type": "final", "text": turn.text})
                break

            messages.append(Message.assistant(result.text or "", result.tool_calls))
            for call in result.tool_calls:
                name, arguments = _parse_tool_call(call)
                outcome = self.registry.invoke(name, tool_context, arguments)
                turn.tool_calls.append({
                    "step": step, "name": name, "arguments": arguments,
                    "ok": outcome.ok, "error": outcome.error,
                })
                transcript.append({
                    "step": step, "type": "tool", "name": name,
                    "arguments": arguments, "ok": outcome.ok, "error": outcome.error,
                })
                if on_step:
                    on_step({"step": step, "type": "tool", "name": name,
                             "arguments": arguments, "ok": outcome.ok,
                             "error": outcome.error})
                messages.append(Message.tool_result(
                    str(call.get("id") or ""), name, outcome.as_text()
                ))
        else:
            # Budget exhausted while still calling tools: ask once more without
            # tools so the agent produces an answer instead of nothing.
            try:
                result = client.chat(
                    messages,
                    temperature=agent_config.temperature if temperature is None else temperature,
                    max_tokens=max_tokens or agent_config.max_tokens,
                    tools=None,
                )
                usage = usage.merge(result.usage)
                turn.text = (result.text or "").strip()
                transcript.append({"step": steps_allowed + 1, "type": "final",
                                   "text": turn.text, "note": "步数耗尽后的无工具收尾"})
            except LLMError as exc:
                turn.error = str(exc)
                turn.text = turn.text or _fallback_text(exc)

        turn.usage = usage
        turn.messages = transcript
        turn.elapsed_ms = int((time.monotonic() - started) * 1000)
        return turn

    # ------------------------------------------------------------------
    # context factory
    # ------------------------------------------------------------------
    def make_tool_context(
        self,
        *,
        session_id: str = "",
        agent_id: str = "",
        allowed_hashes: Optional[Sequence[str]] = None,
        allowed_tools: Optional[Sequence[str]] = None,
        workspace: Optional[str] = None,
        round_index: int = 1,
    ) -> ToolContext:
        return ToolContext(
            services=self.services,
            session_id=session_id,
            agent_id=agent_id,
            allowed_hashes=list(allowed_hashes) if allowed_hashes is not None else None,
            allowed_tools=set(allowed_tools) if allowed_tools is not None else None,
            workspace=workspace,
            round_index=round_index,
        )

    # ------------------------------------------------------------------
    # entry points (implemented in dedicated modules)
    # ------------------------------------------------------------------
    def answer(self, **kwargs: Any) -> Dict[str, Any]:
        from .qa import answer_question

        return answer_question(self, **kwargs)

    async def stream_answer(self, **kwargs: Any):
        from .qa import stream_answer

        async for event in stream_answer(self, **kwargs):
            yield event

    def brainstorm(self, **kwargs: Any) -> Dict[str, Any]:
        from .brainstorm import run_brainstorm

        return run_brainstorm(self, **kwargs)

    def continue_brainstorm(self, session_id: str, message: Optional[str] = None) -> Dict[str, Any]:
        from .brainstorm import continue_brainstorm

        return continue_brainstorm(self, session_id, message=message)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _parse_tool_call(call: Dict[str, Any]) -> tuple:
    """Extract ``(name, arguments)`` from a wire tool call, tolerating junk."""
    function = call.get("function") or {}
    name = str(function.get("name") or call.get("name") or "").strip()
    raw = function.get("arguments")
    if isinstance(raw, dict):
        return name, dict(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return name, parsed
        except json.JSONDecodeError:
            logger.warning("工具 %s 的参数不是合法 JSON，已按空参数处理", name)
    return name, {}


def _fallback_text(exc: Exception) -> str:
    if isinstance(exc, ProviderNotConfigured):
        return "尚未配置问答模型，无法作答。请在设置页填写提供方与模型。"
    return f"模型调用失败，本轮无法作答：{exc}"
