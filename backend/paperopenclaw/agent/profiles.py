"""Agent role presets (FR-10.3).

Personas are chosen so their blind spots differ: the methodologist notices
mechanism, the experimentalist notices evidence quality, the skeptic attacks the
claim, and the connector asks what transfers. A brainstorm of four similar roles
produces four restatements of one opinion, which is why the defaults are
deliberately in tension.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence


@dataclass
class AgentProfile:
    """A reusable role definition."""

    id: str
    name: str
    persona: str
    tools: List[str] = field(default_factory=list)
    default: bool = False
    emoji_free: bool = True  # the UI forbids icons; kept explicit for clarity

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "persona": self.persona,
            "tools": list(self.tools),
            "default": self.default,
        }


READONLY_TOOLS = ["rag_search", "paper_meta", "kg_query", "okf_read", "asset_list"]
ANALYST_TOOLS = READONLY_TOOLS + ["py_eval"]


PROFILES: List[AgentProfile] = [
    AgentProfile(
        id="qa",
        name="问答助手",
        persona=(
            "你是论文知识库的问答助手。回答必须完全基于检索到的证据，"
            "每个结论都要标注来源编号；证据不足时明确说明，不要用常识补全。"
        ),
        tools=READONLY_TOOLS,
        default=True,
    ),
    AgentProfile(
        id="moderator",
        name="主持人",
        persona=(
            "你是一场学术讨论的主持人。你的职责是拆解议题、维持讨论纪律、"
            "识别虚假共识、在分歧处给出基于证据的判断，并在最后汇总为结构化结论。"
            "你不代替他人发言，也不引入讨论中未出现的论点。"
        ),
        tools=ANALYST_TOOLS,
    ),
    AgentProfile(
        id="methodologist",
        name="方法学家",
        persona=(
            "你只关心方法与机制：模型结构、算法步骤、假设前提、复杂度与实现细节。"
            "你会追问「这个方法为什么有效」「在什么条件下会失效」"
            "「作者省略了哪些关键步骤」。你倾向给出机制层面的解释，而不是复述实验结果。"
        ),
        tools=ANALYST_TOOLS,
    ),
    AgentProfile(
        id="experimentalist",
        name="实验学家",
        persona=(
            "你只关心证据质量：数据集选择、基线是否公平、指标是否恰当、"
            "消融是否完整、统计显著性、结果是否可复现。"
            "你会指出「这个提升可能来自什么混淆因素」「缺少哪个对照实验」。"
        ),
        tools=ANALYST_TOOLS,
    ),
    AgentProfile(
        id="skeptic",
        name="怀疑论者",
        persona=(
            "你的职责是攻击结论而不是附和。你会寻找反例、替代解释、"
            "外部效度问题与过度声称，并明确指出作者在哪些地方超出了证据能支持的范围。"
            "攻击必须具体、可检验，不能只是泛泛的怀疑。"
        ),
        tools=ANALYST_TOOLS,
    ),
    AgentProfile(
        id="connector",
        name="跨域联结者",
        persona=(
            "你负责把这篇论文与其它领域、其它方法、真实场景连接起来。"
            "你会指出「这个思路能否迁移到 X」「它与 Y 方法的关系是什么」"
            "「在工程落地时最大的障碍是什么」。你的价值在于提出别人没想到的联系。"
        ),
        tools=ANALYST_TOOLS,
    ),
    AgentProfile(
        id="engineer",
        name="工程师",
        persona=(
            "你关注可落地性：算力与显存需求、推理延迟、数据准备成本、"
            "工程复杂度、失败模式与监控。你会把纸面方法翻译成「实际要做哪些事、"
            "会遇到什么坑、代价是多少」。"
        ),
        tools=ANALYST_TOOLS,
    ),
]

PROFILES_BY_ID: Dict[str, AgentProfile] = {profile.id: profile for profile in PROFILES}

# The default brainstorm roster: four roles chosen for maximum disagreement.
DEFAULT_BRAINSTORM_ROSTER = ["methodologist", "experimentalist", "skeptic", "connector"]


def list_profiles() -> List[Dict[str, Any]]:
    return [profile.to_dict() for profile in PROFILES]


def get_profile(profile_id: str) -> AgentProfile:
    """Fetch a profile, synthesising a generic one for unknown ids."""
    profile = PROFILES_BY_ID.get(profile_id)
    if profile is not None:
        return profile
    return AgentProfile(
        id=profile_id,
        name=profile_id,
        persona="你是一名严谨的学术分析者，基于证据给出结论，并明确标注不确定性。",
        tools=READONLY_TOOLS,
    )


def resolve_roster(ids: Optional[Sequence[str]], limit: int = 6) -> List[AgentProfile]:
    """Map requested ids to profiles, defaulting to the standard roster."""
    if not ids:
        return [PROFILES_BY_ID[item] for item in DEFAULT_BRAINSTORM_ROSTER][:limit]
    resolved: List[AgentProfile] = []
    for item in ids:
        profile = PROFILES_BY_ID.get(str(item))
        if profile is None:
            continue
        if profile.id not in {entry.id for entry in resolved}:
            resolved.append(profile)
    if not resolved:
        return [PROFILES_BY_ID[item] for item in DEFAULT_BRAINSTORM_ROSTER][:limit]
    return resolved[:limit]
