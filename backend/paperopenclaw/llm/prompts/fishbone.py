"""Prompt for extracting the fishbone graph directly from the OKF full text.

The fishbone is the seven-dimension skeleton (背景/数据/方法/实验/结论/讨论/不足与展望).
This prompt asks the model to mine the faithful OKF record for one point per idea,
so the fishbone is grounded in the original wording rather than in a digest.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Sequence

FISHBONE_SCHEMA: Dict[str, Any] = {
    "sections": [
        {
            "section": "background|data|method|experiment|conclusion|discussion|limitations",
            "points": [
                {
                    "text": "一条完整、可独立理解的要点，保留关键数字与专有名词",
                    "assets": ["该要点直接涉及的图表编号，如 fig1、tab2；没有则为空数组"],
                }
            ],
        }
    ],
}

SECTION_OPTIONS = (
    "background（背景）、data（数据）、method（方法）、experiment（实验）、"
    "conclusion（结论）、discussion（讨论）、limitations（不足与展望）"
)


def fishbone_system_prompt() -> str:
    return f"""你是一名学术论文结构分析专家，负责从论文全文里提取「七维鱼骨」要点。

七个维度固定为：{SECTION_OPTIONS}

要求：
1. 只输出一个 JSON 对象，不要 Markdown 围栏，不要解释。
2. 严格依据给定全文，只提取原文确实陈述过的内容，不脑补、不评价。
3. 每个维度下的 points 是给知识图谱用的节点，每一条必须：
   - 句子完整、脱离上下文也能读懂；
   - 尽量保留关键数字、专有名词、模型名、指标名；
   - 相互之间不重复，按重要性从高到低排列；
   - 数量 1-6 条；原文没有该维度内容时，该维度可以不给 points 或给空数组。
4. points[].assets 只能填写「给定图表编号」里出现的 figN / tabN；不确定就填 []。
5. 输出语言：中文。专有名词、模型名、数据集名、指标名保留英文原文。

JSON 结构：
{json.dumps(FISHBONE_SCHEMA, ensure_ascii=False, indent=2)}
"""


def fishbone_user_prompt(paper_title: str, full_text: str,
                         asset_labels: Optional[Sequence[str]] = None) -> str:
    body = (full_text or "").strip()
    # OKF 全文可能很长；给模型留足上下文但要设界：取头 + 尾，中间略过。
    if len(body) > 24000:
        head = body[:16000]
        tail = body[-8000:]
        body = f"{head}\n\n……（中间内容略）……\n\n{tail}"

    asset_line = ""
    if asset_labels:
        asset_line = (
            "\n本文已裁剪出的图表编号为：" + "、".join(sorted(asset_labels)) +
            "。points[].assets 只能从这些编号中选取。\n"
        )

    return (
        f"论文标题：{paper_title}\n"
        f"{asset_line}\n"
        f"论文全文（OKF）：\n=====\n{body}\n=====\n\n"
        f"请按七个维度提取鱼骨要点 JSON。"
    )
