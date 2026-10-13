"""Prompts for turning a PDF into structured metadata and sections (FR-3).

The parse contract is a single JSON object. It is spelled out field by field in
the system prompt *and* echoed as a schema literal, because models follow a
concrete example far more reliably than a prose description.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Sequence

# The exact shape we require back. Kept as a literal so prompt and validator
# can never drift: `paperopenclaw.ingest.vision_parse` validates against this.
PARSE_SCHEMA: Dict[str, Any] = {
    "title": "论文标题（英文原文）",
    "title_original": "论文标题（原始语言，如中文标题）",
    "authors": [
        {
            "name": "作者姓名",
            "name_en": "英文名或拼音（若有）",
            "affiliations": ["该作者所属单位名称（与 affiliations 中的 name 对应）"],
            "corresponding": "是否为通讯作者 true/false",
            "email": "邮箱（若有）",
        }
    ],
    "affiliations": [
        {"name": "单位名称", "department": "院系", "country": "国家"}
    ],
    "journal": "期刊或会议名称",
    "venue_type": "journal|conference|preprint|thesis|other",
    "published_at": "刊发时间，YYYY-MM-DD 或 YYYY-MM 或 YYYY",
    "year": "年份，整数",
    "doi": "DOI（若有）",
    "url": "原文链接（若有）",
    "abstract": "摘要全文",
    "keywords": ["关键词"],
    "language": "zh|en|mixed",
    "sections": [
        {
            "title": "章节标题",
            "text": "该章节的完整正文（保持原意，不要删减关键信息）",
            "section": "该章节所属维度，只能取 background|data|method|experiment|conclusion|discussion|limitation 之一",
        }
    ],
    "figures": [
        {
            "label": "fig1",
            "caption": "图注全文",
            "page": "所在页码，整数",
            "content": "该图展示的内容描述",
        }
    ],
    "tables": [
        {
            "label": "tab1",
            "caption": "表注全文",
            "page": "所在页码，整数",
            "content": "该表的关键数据或结论（可用 Markdown 表格）",
        }
    ],
    "contributions": ["论文的主要贡献"],
    "limitations": ["论文自述的不足与局限"],
    "future_work": ["论文提出的未来工作"],
}

_SPINE_LEGEND = """维度取值含义（section 字段只能取下列之一）：
- background：研究背景、问题动机、相关工作
- data：数据集、语料、样本、标注
- method：模型、算法、架构、技术路线、实现细节
- experiment：实验设置、评估指标、结果、消融、对比
- conclusion：主要结论与贡献总结
- discussion：结果解读、机理分析、意义与启示
- limitation：局限性、不足、未来工作

若某章节无法归入以上任何维度，请选择语义上最接近的一个，不要留空、不要自造新值。"""


def parse_system_prompt(language_hint: str = "") -> str:
    """System prompt for PDF → structured JSON extraction."""
    language_line = ""
    if language_hint:
        language_line = f"\n论文主要语言初步判定为 {language_hint}，正文请按其原语言抽取，不要翻译。\n"
    return f"""你是一名严谨的学术论文结构化抽取引擎。用户会给你一篇论文（PDF 文件或逐页图像）。
你的唯一任务是通读全文并输出**一个 JSON 对象**，不得输出任何解释、前言、后记或 Markdown 围栏。
{language_line}
要求：
1. 只输出 JSON。第一个字符必须是 {{，最后一个字符必须是 }}。
2. 所有字段都必须出现；确实没有信息的字段用空字符串 ""、空数组 [] 或 null，不得省略字段。
3. 忠于原文：不要杜撰作者、单位、期刊、DOI、数据或结论。看不清的内容宁可留空。
4. 作者与单位必须对应：authors[].affiliations 里的名称要与 affiliations[].name 完全一致。
5. 单位要精确到机构名；有院系、国家信息时一并给出。
6. sections 要覆盖论文的正文结构（含 Introduction、Method、Experiments、Conclusion 等），
   每节 text 尽量完整，不要只写摘要式的一句话。
7. figures 与 tables 按论文中出现顺序编号：图用 fig1、fig2…，表用 tab1、tab2…。
   label 必须与这个编号规则一致。
8. 若输入只是论文的一个片段（用户会告知页码范围），只抽取该片段中的内容；
   不要重复输出片段之外的内容，也不要用空值填充，直接返回你能看到的字段。

{_SPINE_LEGEND}

JSON 结构（严格遵守字段名与嵌套关系）：
{json.dumps(PARSE_SCHEMA, ensure_ascii=False, indent=2)}
"""


def parse_user_prompt(
    page_from: int = 1,
    page_to: int = 0,
    part_index: int = 1,
    part_total: int = 1,
    filename: str = "",
    detected_assets: Optional[Sequence[Dict[str, Any]]] = None,
    text_excerpt: str = "",
) -> str:
    """User prompt for one PDF slice.

    ``detected_assets`` and ``text_excerpt`` come from the local PyMuPDF pass:
    they help the model label figures/tables consistently with the filenames we
    already cropped (FR-4.2), without asking it to guess geometry.
    """
    lines: List[str] = []
    if filename:
        lines.append(f"文件名：{filename}")
    if part_total > 1:
        if page_to:
            lines.append(f"这是第 {part_index}/{part_total} 个片段，仅包含第 {page_from}-{page_to} 页。")
        else:
            lines.append(f"这是第 {part_index}/{part_total} 个片段。")
    else:
        lines.append("以下是论文全文。")

    if part_index > 1:
        lines.append("注意：元数据（标题、作者、单位、期刊、刊发时间、摘要、关键词）已在第 1 个片段中抽取，"
                     "本片段请把 title/authors/affiliations/journal/published_at/abstract/keywords 留空或返回空数组，"
                     "只填 sections、figures、tables。")

    if detected_assets:
        listing = "；".join(
            f"{item.get('label')}（{item.get('kind')}，第 {item.get('page')} 页）"
            for item in detected_assets[:60]
        )
        lines.append(f"本地版面分析已按出现顺序裁剪出以下图表，请沿用相同的 label：{listing}。"
                     f"请为它们补全 caption 与 content。")

    if text_excerpt:
        lines.append("本地抽取的文本片段（供你定位章节标题与图注，可能有排版噪声，以图像/PDF 原文为准）：")
        lines.append("-----")
        lines.append(text_excerpt)
        lines.append("-----")

    lines.append("请输出符合 system 要求的 JSON 对象。")
    return "\n".join(lines)


def repair_prompt(error: str, previous: str = "") -> str:
    """Appended after a failed validation so the retry can self-correct (FR-3.3)."""
    snippet = (previous or "")[:1200]
    return (
        "上一次返回的内容无法通过校验。\n"
        f"校验错误：{error}\n"
        + (f"上一次返回的开头部分：\n{snippet}\n" if snippet else "")
        + "请重新输出**完整且合法**的 JSON 对象：不要 Markdown 围栏，不要注释，不要尾随逗号，"
          "所有引号必须闭合。"
    )
