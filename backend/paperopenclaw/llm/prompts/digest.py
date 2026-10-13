"""Prompts for the RAG pyramid (FR-6).

The layering follows the requirement exactly:

    A  = 从原文精简得到的大意          -> one unit per source section (detailed)
    B  = 把大意 A 再精简                -> one unit per fishbone dimension (要点)
    C  = abstract 原文                  -> no model call at all

Layer B additionally has to feed the knowledge graph (FR-7.1), so it returns
JSON with discrete points rather than a prose blob: those points become the
content nodes mounted on the fishbone.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Sequence

B_SCHEMA: Dict[str, Any] = {
    "summary": "本维度要点的一段式概括，150-300 字",
    "points": [
        {
            "text": "一条具体的要点，句子完整、可独立理解，保留关键数字与专有名词",
            "assets": ["该要点涉及的图表编号，如 fig1、tab2；没有则为空数组"],
        }
    ],
    "importance": "该维度在本文中的重要性，0-1 之间的小数",
}


def digest_a_system_prompt(target_language: str = "zh") -> str:
    """System prompt for the detailed digest (layer A)."""
    language_rule = (
        "输出语言：中文。专有名词、模型名、数据集名、指标名保留英文原文。"
        if target_language == "zh"
        else "Keep the original language of the paper. Do not translate."
    )
    return f"""你是一名学术编辑，负责把论文的某一节压缩成「详细大意」。

要求：
1. 只输出大意正文，不要标题、不要 Markdown 围栏、不要任何解释或前缀。
2. 忠实于原文，不添加原文没有的信息，不做评价。
3. 保留对理解该方法/结论必需的信息：关键定义、关键公式的语义、关键超参数、
   关键数据集的名称与规模、关键指标名称与数值、以及作者给出的理由。
4. 删除与理解无关的内容：致谢、参考文献列表、页眉页脚、排版噪声、重复表述。
5. 结构上按原文逻辑分段，可使用「-」开头的要点列表，也可用连贯段落；不要写成摘要体的空洞概述。
6. 长度控制在 200-500 字；若该节信息量很大，可放宽到 800 字，但不要更长。
7. {language_rule}"""


def digest_a_user_prompt(
    paper_title: str,
    section_label: str,
    section_title: str,
    text: str,
    max_chars: int = 1400,
) -> str:
    """User prompt for one section's digest."""
    body = (text or "").strip()
    if len(body) > max_chars * 6:
        # Give the model room, but bound the request: the head and tail of a
        # section carry the framing and the conclusion respectively.
        head = body[: max_chars * 4]
        tail = body[-max_chars * 2 :]
        body = f"{head}\n\n……（中间内容略）……\n\n{tail}"
    return (
        f"论文标题：{paper_title}\n"
        f"所属维度：{section_label}\n"
        f"章节标题：{section_title}\n\n"
        f"章节正文：\n-----\n{body}\n-----\n\n"
        f"请输出这一节的详细大意。"
    )


def digest_b_system_prompt(target_language: str = "zh") -> str:
    """System prompt for the per-dimension condensation (layer B).

    This is also the input the knowledge graph is built from, so the output is
    schema-bound.
    """
    language_rule = (
        "summary 与 points[].text 用中文书写，专有名词、模型名、数据集名、指标名保留英文原文。"
        if target_language == "zh"
        else "Write in the paper's original language."
    )
    return f"""你是一名学术综述作者，负责把「同一维度的多段详细大意」进一步精简为要点。

要求：
1. 只输出一个 JSON 对象，不要 Markdown 围栏，不要解释。
2. 忠实于给定材料，只做压缩与归纳，不引入材料之外的信息。
3. points 是给知识图谱使用的节点，因此每一条都必须：
   - 句子完整、脱离上下文也能读懂；
   - 尽量保留关键数字、专有名词、模型名、指标名；
   - 相互之间不重复，按重要性从高到低排列；
   - 数量控制在 3-8 条，信息不足时可少于 3 条，但不要为凑数而拆句。
4. points[].assets 填写该要点直接涉及的图表编号（只能填材料中出现过的 figN / tabN）；
   若材料未提及任何图表，或无法确定，填 []。
5. importance 反映该维度对整篇论文的重要程度：背景/讨论类通常偏低，方法/实验类通常偏高。
6. {language_rule}

JSON 结构：
{json.dumps(B_SCHEMA, ensure_ascii=False, indent=2)}
"""


def digest_b_user_prompt(
    paper_title: str,
    section_label: str,
    digests: Sequence[Dict[str, str]],
    known_assets: Optional[Sequence[str]] = None,
) -> str:
    """User prompt for one dimension's condensation.

    ``digests`` items carry ``title`` and ``text`` (the layer-A digests that map
    to this dimension).
    """
    blocks: List[str] = []
    for index, item in enumerate(digests, start=1):
        blocks.append(
            f"[材料 {index}] 章节：{item.get('title') or '未命名'}\n{item.get('text') or ''}"
        )
    material = "\n\n".join(blocks) if blocks else "（无材料）"

    asset_line = ""
    if known_assets:
        asset_line = (
            "\n本文已裁剪出的图表编号为：" + "、".join(sorted(known_assets)) +
            "。points[].assets 只能从这些编号中选取。\n"
        )

    return (
        f"论文标题：{paper_title}\n"
        f"目标维度：{section_label}\n"
        f"{asset_line}\n"
        f"该维度下的详细大意材料：\n=====\n{material}\n=====\n\n"
        f"请把它精简为该维度的要点 JSON。"
    )


def empty_dimension_note(section_label: str) -> str:
    """Fallback text when a dimension has no source material at all."""
    return f"本文未明确给出「{section_label}」相关的内容。"
