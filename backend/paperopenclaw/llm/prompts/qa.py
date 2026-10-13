"""Prompts for retrieval-grounded question answering (FR-11).

The hard requirement here is FR-11.4: when the pyramid has nothing relevant, the
model must say so instead of improvising. That is enforced twice — the retriever
short-circuits on a low apex score, and the system prompt states the rule.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

NO_ANSWER_MESSAGE = "库中无相关内容"


def qa_system_prompt(evidence_block: str, *, strictness: str = "high") -> str:
    """System prompt with the retrieved evidence already inlined."""
    strict_rules = (
        "5. 如果证据不足以回答问题，必须明确回答「库中无相关内容」，并说明缺什么信息；"
        "严禁依据常识、训练记忆或推测来补全答案。\n"
        if strictness == "high"
        else "5. 证据不足时请明确指出依据不足的部分，不要编造。\n"
    )
    return f"""你是 PaperOpenClaw 的论文知识库问答助手。你的回答**只能**基于下面给出的证据。

规则：
1. 每一条结论后面都要标注来源编号，格式为 [1]、[2]，编号对应证据条目。
2. 引用要精确：只引用真正支撑该结论的那几条证据，不要堆砌无关编号。
3. 证据中出现的数字、指标、模型名、数据集名必须原样引用，不得改写数值。
4. 若不同证据之间存在冲突，要指出冲突并分别标注来源，不要擅自取舍。
{strict_rules}6. 用中文回答，语言简洁、结构清晰；能分点就分点。
7. 不要复述证据全文，要做归纳与整合。
8. 不要输出「根据证据」「根据提供的材料」这类元话术，直接给结论。

证据：
=====
{evidence_block}
=====
"""


def qa_user_prompt(question: str) -> str:
    return f"问题：{question}\n\n请基于上面的证据回答，并标注来源编号。"


def format_evidence(items: Sequence[Dict[str, Any]], max_chars_per_item: int = 1200) -> str:
    """Render retrieved units into a numbered evidence block.

    Layer C entries orient the model, B entries carry the substance and A
    entries supply detail — the ordering mirrors the C->B->A cascade so the
    model reads from most to least condensed.
    """
    if not items:
        return "（没有检索到任何内容）"
    lines: List[str] = []
    for index, item in enumerate(items, start=1):
        layer = item.get("layer", "?")
        title = item.get("title") or item.get("paper_title") or "未命名"
        section = item.get("section_label") or item.get("section") or ""
        text = str(item.get("text") or "")[:max_chars_per_item]
        header = f"[{index}] 层={layer}｜论文={title}"
        if section:
            header += f"｜章节={section}"
        hash_value = item.get("hash") or item.get("paper_hash") or ""
        if hash_value:
            header += f"｜哈希={hash_value[:12]}"
        assets = item.get("asset_refs") or []
        if assets:
            header += f"｜图表={'、'.join(assets)}"
        lines.append(f"{header}\n{text}")
    return "\n\n".join(lines)


def no_answer_response(question: str = "") -> str:
    return (
        f"{NO_ANSWER_MESSAGE}。\n\n"
        "当前金字塔语义库（C 层摘要 / B 层要点 / A 层详细大意）中没有检索到与问题相关的证据。"
        "可以尝试：换用论文中的术语提问、放宽检索范围，或先导入相关论文。"
    )


def citation_entry(unit: Dict[str, Any]) -> Dict[str, Any]:
    """Shape one evidence item into the citation object the UI renders."""
    return {
        "unit_id": unit.get("id") or unit.get("unit_id") or "",
        "layer": unit.get("layer", ""),
        "hash": unit.get("paper_hash") or unit.get("hash") or "",
        "title": unit.get("title") or unit.get("paper_title") or "",
        "section": unit.get("section", ""),
        "section_label": unit.get("section_label", ""),
        "snippet": (unit.get("text") or "")[:280],
        "score": round(float(unit.get("score") or 0.0), 4),
        "asset_refs": unit.get("asset_refs") or [],
    }
