"""Context engine: assembling the system prompt (FR-10.2, ADR-8).

The order of the blocks is deliberate and stable, so a change in behaviour can be
attributed to a specific block rather than to prompt drift:

    1. identity and stance        (who the agent is)
    2. operating rules            (grounding, citations, honesty about gaps)
    3. isolation contract         (what this agent may see and do)
    4. skills                     (enabled SKILL.md instructions)
    5. tools                      (name + description + parameter schema)
    6. paper scope                (which papers are in view)
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from ..llm.base import ToolSpec
from .profiles import AgentProfile

BASE_RULES = """\
工作规则：
1. 所有事实性结论都必须来自工具返回的证据，禁止依据训练记忆或常识编造。
2. 引用证据时标注来源编号（如 [1]、[2]），编号对应工具返回结果中的顺序。
3. 工具返回 empty 或 error 时，如实说明「库中无相关内容」或工具不可用，不要绕过。
4. 数字、指标名、模型名、数据集名必须原样引用，不得四舍五入或改写。
5. 区分「论文的结论」与「你的推断」；推断必须显式标注为推断。
6. 不确定时给出不确定性来源，而不是给出一个圆滑的答案。
7. 用中文回答，结构清晰；不使用表情符号或图形符号。"""

ISOLATION_TEMPLATE = """\
隔离约束（本轮的运行边界，不得绕过）：
- 你的可见论文范围：{scope}
- 你可调用的工具：{tools}
- 你的工作目录：{workspace}
- 你与其他智能体的对话上下文相互隔离；你能看到的他人观点只来自主持人提供的黑板摘要。
- 不要尝试访问范围之外的论文，也不要假定自己知道其他智能体的完整推理过程。"""


class ContextEngine:
    """Builds the system prompt and the memory context block."""

    def __init__(self, services: Any) -> None:
        self.services = services

    # ------------------------------------------------------------------
    def build_system_prompt(
        self,
        profile: AgentProfile,
        *,
        tool_specs: Sequence[ToolSpec],
        skills_instructions: str = "",
        paper_scope: Optional[Sequence[str]] = None,
        role_note: str = "",
        extra_rules: str = "",
    ) -> str:
        blocks: List[str] = []

        blocks.append(f"# 身份\n你是「{profile.name}」。{profile.persona.strip()}")
        if role_note:
            blocks.append(f"# 本轮角色\n{role_note.strip()}")

        blocks.append(f"# 工作规则\n{BASE_RULES}")
        if extra_rules:
            blocks.append(extra_rules.strip())

        blocks.append("# 隔离约束\n" + ISOLATION_TEMPLATE.format(
            scope=self._describe_scope(paper_scope),
            tools="、".join(spec.name for spec in tool_specs) or "（无）",
            workspace=str(getattr(self.services.paths, "agents_dir", "")),
        ))

        if skills_instructions.strip():
            blocks.append("# 已装载技能\n" + skills_instructions.strip())

        if tool_specs:
            blocks.append("# 可用工具\n" + self._describe_tools(tool_specs))

        return "\n\n".join(blocks)

    # ------------------------------------------------------------------
    def _describe_scope(self, paper_scope: Optional[Sequence[str]]) -> str:
        if not paper_scope:
            return "知识库内全部论文"
        papers = self.services.papers.list(hashes=list(paper_scope), page_size=100)[0]
        by_hash = {row["hash"]: row for row in papers}
        lines = []
        for hash_value in paper_scope:
            row = by_hash.get(hash_value)
            title = (row or {}).get("title") or "（已删除或不可见）"
            lines.append(f"  - 《{title}》（哈希 {hash_value[:12]}）")
        return "\n" + "\n".join(lines)

    @staticmethod
    def _describe_tools(tool_specs: Sequence[ToolSpec]) -> str:
        import json

        lines = []
        for spec in tool_specs:
            lines.append(f"## {spec.name}\n{spec.description}")
            if spec.parameters.get("properties"):
                lines.append("参数：" + json.dumps(spec.parameters, ensure_ascii=False))
        return "\n".join(lines)

    # ------------------------------------------------------------------
    def build_evidence_block(self, evidences: Sequence[Dict[str, Any]],
                             max_chars_per_item: int = 1200) -> str:
        from ..llm.prompts.qa import format_evidence

        return format_evidence(evidences, max_chars_per_item=max_chars_per_item)

    def tool_result_block(self, name: str, text: str, max_chars: int = 12000) -> str:
        body = text if len(text) <= max_chars else text[:max_chars] + "…（已截断）"
        return f"工具 {name} 返回：\n{body}"
