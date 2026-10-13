"""Prompts for isolated multi-agent brainstorming (FR-10).

Isolation is a prompt-level contract as much as a runtime one: round 1 agents
receive only their own role brief and their own retrieved evidence, never each
other's output. Later rounds exchange *summaries* through the blackboard, not
raw contexts (FR-10.5), which is what keeps the discussion from collapsing into
one voice while still letting positions interact.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Sequence

OPINION_SCHEMA: Dict[str, Any] = {
    "claim": "你的核心主张，一句话",
    "reasoning": "推理过程，150-400 字",
    "evidence": [
        {
            "point": "你引用的具体证据要点",
            "source": "证据编号，如 [2]；若来自常识或推理请写 none",
        }
    ],
    "confidence": "你对这一主张的置信度，0-1 之间的小数",
    "caveats": ["你自己看到的局限、反例或不确定性"],
    "questions": ["你希望其他角色回答的问题"],
}

SYNTHESIS_SCHEMA: Dict[str, Any] = {
    "synthesis": "整场讨论的总结，300-600 字",
    "consensus": [{"point": "达成共识的结论", "supporters": ["支持该结论的角色名"]}],
    "disagreements": [
        {
            "topic": "分歧主题",
            "positions": [{"role": "角色名", "stance": "该角色的立场"}],
            "assessment": "你对这一分歧的评估，指出哪一方证据更强及原因",
        }
    ],
    "evidence_summary": [{"point": "讨论中被反复引用的关键证据", "sources": ["来源编号或论文哈希"]}],
    "open_questions": ["尚未解决、值得进一步研究的问题"],
    "recommendations": ["基于本次讨论的行动建议"],
}


def decompose_system_prompt() -> str:
    return """你是一场学术头脑风暴的主持人。你的任务是把议题拆解为若干**互不重叠**的子问题，
分派给不同角色的专家独立研究。

要求：
1. 只输出一个 JSON 对象，不要 Markdown 围栏，不要解释。
2. 子问题数量与给定角色数量一致，一一对应，顺序保持一致。
3. 每个子问题都要能从给定论文材料中找到依据，避免空泛的「谈谈你的看法」。
4. 子问题之间要有张力：有的偏方法、有的偏证据、有的偏质疑、有的偏跨领域迁移，
   这样独立结论汇总后才有多样性。

JSON 结构：
{
  "sub_questions": [
    {"role": "角色名（与输入一致）", "question": "该角色要回答的具体问题", "focus": "该问题的考察角度"}
  ],
  "shared_context": "所有角色都需要的背景说明，100-200 字"
}"""


def decompose_user_prompt(topic: str, roles: Sequence[Dict[str, str]],
                          paper_briefs: Sequence[Dict[str, str]]) -> str:
    role_lines = "\n".join(
        f"- {item.get('name', '')}：{item.get('persona', '')}" for item in roles
    )
    brief_lines = []
    for item in paper_briefs:
        brief_lines.append(
            f"- 《{item.get('title', '')}》（哈希 {str(item.get('hash', ''))[:12]}）\n"
            f"  摘要：{item.get('abstract', '') or '（无摘要）'}"
        )
    briefs = "\n".join(brief_lines) if brief_lines else "（未指定具体论文，请围绕议题本身拆解）"
    return (
        f"议题：{topic}\n\n"
        f"参与角色：\n{role_lines}\n\n"
        f"可参考的论文材料：\n{briefs}\n\n"
        f"请输出子问题拆解 JSON。"
    )


def analysis_system_prompt(role_name: str, persona: str, shared_context: str = "") -> str:
    """Prompt for one isolated expert — natural dialogue, not a JSON blob.

    The output is conversational prose that *summarises* the retrieved evidence
    rather than copying it, cites papers inline with [n], and closes with a
    reference list in the fixed format `[n] 作者，论文名，哈希`.
    """
    context_block = f"\n共同背景：{shared_context}\n" if shared_context else ""
    return f"""你是「{role_name}」。你的立场与专长是：{persona}
{context_block}
现在你要**独立**完成分析。你看不到其他角色的观点，也不要猜测他们会说什么。

输出要求（只输出正文，不要 JSON，不要 Markdown 围栏）：
1. 用自然的对话语气表达你的观点，像在讨论组里发言，而不是写综述或列条目。
2. 内容必须是对检索证据的**提炼与概括**，用自己的话转述，禁止整段复制原文。
3. 引用证据时在句子中标注编号，如「……达到 28.4 BLEU [1]，显著优于基线 [2]」。
   编号对应证据条目。证据不支持的推断要显式说明是「我的推测」。
4. 保持你的角色视角，给出明确判断，同时诚实指出自己的不确定性。
5. 结尾另起一段，列出你实际用到的「参考文献」，每行一条，格式严格为：
   [n] 作者，论文名，哈希
   其中 n 是正文里出现过的编号；只列你真正引用过的，不要罗列全部证据。
6. 全文 150-350 字，简洁有力，不要空话。"""


def analysis_user_prompt(question: str, evidence_block: str, round_index: int = 1,
                         previous: Optional[str] = None,
                         blackboard: str = "", steering: str = "") -> str:
    parts: List[str] = []
    if steering:
        parts.append(f"用户补充指示（请据此调整讨论方向）：\n-----\n{steering}\n-----\n")
    if round_index > 1 and blackboard:
        parts.append(f"其他角色的观点摘要（黑板，仅供你参考与回应）：\n-----\n{blackboard}\n-----\n")
    if previous:
        parts.append(
            "你在上一轮的发言（可以坚持、修正或撤回）：\n"
            f"-----\n{previous}\n-----\n"
        )
    parts.append(f"你要回答的问题：{question}\n")
    parts.append(f"你可以使用的证据（编号即引用编号）：\n=====\n{evidence_block}\n=====\n")
    if round_index > 1:
        parts.append("请基于黑板上的他人观点，明确说明你同意什么、反对什么及理由，再给出你的更新发言。")
    else:
        parts.append("请给出你的独立发言。")
    return "\n".join(parts)


def blackboard_system_prompt() -> str:
    return """你是头脑风暴的主持人。请把本轮各角色的发言压缩成一份「黑板摘要」。

要求：
1. 只输出一段纯文本摘要，不要 JSON，不要围栏。
2. 摘要只保留观点本身，**不要**保留各角色的完整推理过程、语气与修辞。
3. 按主题组织，而不是按角色罗列；同一主题下若有多人持相同观点，合并并注明角色名。
4. 明确标出相互冲突的观点。
5. 长度控制在 400 字以内。这份摘要会作为下一轮唯一的跨角色信息通道，
   因此既要有信息量，也不能替代各角色自己的判断。"""


def blackboard_user_prompt(round_index: int, speeches: Sequence[Dict[str, Any]]) -> str:
    blocks = []
    for item in speeches:
        blocks.append(f"角色：{item.get('name', '')}\n{item.get('text', '')}")
    return (
        f"第 {round_index} 轮各角色发言如下：\n\n" + "\n\n".join(blocks) +
        "\n\n请输出黑板摘要。"
    )


def synthesis_system_prompt() -> str:
    return f"""你是头脑风暴的主持人，负责做最终汇总。

要求：
1. 只输出一个 JSON 对象，不要 Markdown 围栏，不要解释。
2. consensus 只收录**确实**被多方独立支持的结论，不要为了好看而制造共识。
3. disagreements 要如实呈现；assessment 必须给出你的判断与依据，不要和稀泥。
4. evidence_summary 只收录讨论中真正被引用的证据，并标注来源。
5. open_questions 要具体、可研究，避免「需要进一步研究」这类空话。
6. 不要引入讨论中未出现的新论点。

JSON 结构：
{json.dumps(SYNTHESIS_SCHEMA, ensure_ascii=False, indent=2)}
"""


def synthesis_user_prompt(topic: str, transcript: Sequence[Dict[str, Any]],
                          blackboard: str = "") -> str:
    blocks = []
    for item in transcript:
        blocks.append(
            f"【第 {item.get('round', 1)} 轮】{item.get('name', '')}：\n"
            f"{item.get('text', '')}"
        )
    bb = f"最后一轮黑板摘要：\n{blackboard}\n\n" if blackboard else ""
    return (
        f"议题：{topic}\n\n{bb}"
        f"全部发言记录：\n=====\n" + "\n\n".join(blocks) + "\n=====\n\n"
        f"请输出汇总 JSON。"
    )
