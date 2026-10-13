"""Prompt for extracting the knowledge graph keywords from the OKF full text.

The knowledge graph is the floating keyword graph. Keywords are extracted from
the faithful OKF record and the model is explicitly asked to keep only research
terms — dropping common words like 影响/作者/研究/方法 — so the graph stays a
map of the paper's concepts rather than a bag of stopwords.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Sequence

KEYWORDS_SCHEMA: Dict[str, Any] = {
    "keywords": ["领域专业术语或专有名词，如 el niño、pacific decadal oscillation、主成分分析"],
}


def kg_keywords_system_prompt() -> str:
    return f"""你是一名学术术语识别专家，负责从论文全文中提取「研究专业关键词」。

要求：
1. 只输出一个 JSON 对象，不要 Markdown 围栏，不要解释。
2. 只保留「研究专业术语/专有名词」：方法名、模型名、数据集名、指标名、地名、气候/生物/物理等学科概念、
   专有缩写、专业软件/工具名。
3. 丢弃通用词与停用词：例如「影响」「作者」「研究」「方法」「本文」「结果」「分析」「数据」「图」「表」、
   「使用」「基于」「提出」「表明」「例如」「其中」「一个」「进行」等。
4. 每个关键词尽量规范（小写英文；中文用规范译名）；去重；数量 5-20 个，不够可少给。
5. 只依据给定全文，不引入文中没有的概念。

JSON 结构：
{json.dumps(KEYWORDS_SCHEMA, ensure_ascii=False, indent=2)}
"""


def kg_keywords_user_prompt(paper_title: str, full_text: str) -> str:
    body = (full_text or "").strip()
    if len(body) > 24000:
        head = body[:16000]
        tail = body[-8000:]
        body = f"{head}\n\n……（中间内容略）……\n\n{tail}"
    return (
        f"论文标题：{paper_title}\n\n"
        f"论文全文（OKF）：\n=====\n{body}\n=====\n\n"
        f"请提取研究专业关键词 JSON。"
    )
