"""Fishbone spine definition and section mapping (FR-7.2).

The seven dimensions are fixed and ordered to mirror how a paper is written.
Everything downstream — layer-B condensation, graph construction, the UI
fishbone — keys off these slugs, so they must never drift.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence

# Ordered exactly as a paper reads (FR-7.2).
SPINE: List[Dict[str, Any]] = [
    {
        "slug": "background",
        "label": "背景",
        "order": 1,
        "description": "研究背景、问题动机与相关工作",
        "keywords": [
            "introduction", "background", "motivation", "related work", "prior work",
            "problem statement", "literature review", "引言", "背景", "动机",
            "相关工作", "研究现状", "问题定义",
        ],
    },
    {
        "slug": "data",
        "label": "数据",
        "order": 2,
        "description": "数据集、语料、样本与标注",
        "keywords": [
            "data", "dataset", "datasets", "corpus", "benchmark", "benchmarks",
            "data collection", "data preparation", "annotation", "sampling",
            "数据", "数据集", "语料", "语料库", "基准", "标注", "样本", "数据收集",
        ],
    },
    {
        "slug": "method",
        "label": "方法",
        "order": 3,
        "description": "模型、算法、架构与技术路线",
        "keywords": [
            "method", "methods", "methodology", "approach", "model", "models",
            "architecture", "framework", "algorithm", "algorithm design",
            "implementation", "system design", "proposed", "our approach",
            "方法", "方法论", "模型", "架构", "框架", "算法", "技术路线", "实现",
        ],
    },
    {
        "slug": "experiment",
        "label": "实验",
        "order": 4,
        "description": "实验设置、评估指标与结果分析",
        "keywords": [
            "experiment", "experiments", "experimental setup", "evaluation",
            "results", "result", "ablation", "ablation study", "metrics",
            "performance", "comparison", "baseline", "case study",
            "实验", "实验设置", "评估", "结果", "消融", "指标", "性能", "对比", "基线",
        ],
    },
    {
        "slug": "conclusion",
        "label": "结论",
        "order": 5,
        "description": "主要结论与贡献总结",
        "keywords": [
            "conclusion", "conclusions", "concluding remarks", "summary",
            "conclusion and future work", "结论", "总结", "结束语",
        ],
    },
    {
        "slug": "discussion",
        "label": "讨论",
        "order": 6,
        "description": "结果解读、机理分析与意义",
        "keywords": [
            "discussion", "discussions", "analysis", "further analysis",
            "interpretation", "implications", "threats to validity",
            "讨论", "分析", "进一步分析", "解读", "意义", "启示", "有效性威胁",
        ],
    },
    {
        "slug": "limitation",
        "label": "不足与展望",
        "order": 7,
        "description": "局限性、不足与未来工作",
        "keywords": [
            "limitation", "limitations", "future work", "future directions",
            "open problems", "outlook", "不足", "局限", "局限性", "展望",
            "未来工作", "未来方向", "开放问题",
        ],
    },
]

SPINE_SLUGS: List[str] = [item["slug"] for item in SPINE]
SPINE_BY_SLUG: Dict[str, Dict[str, Any]] = {item["slug"]: item for item in SPINE}
SPINE_LABELS: Dict[str, str] = {item["slug"]: item["label"] for item in SPINE}
SPINE_ORDER: Dict[str, int] = {item["slug"]: item["order"] for item in SPINE}

# Prepared matchers: longest keyword first so "related work" beats "work".
_MATCHERS: List[tuple] = []
for _item in SPINE:
    for _keyword in _item["keywords"]:
        _MATCHERS.append((_keyword.lower(), _item["slug"]))
_MATCHERS.sort(key=lambda pair: len(pair[0]), reverse=True)

_NUMBER_PREFIX = re.compile(r"^\s*(?:\d+(?:\.\d+)*|[ivxlc]+)[.)\s、-]*", re.IGNORECASE)


def normalize_heading(heading: str) -> str:
    """Lowercase a heading and strip its numbering ("3.1 Method" -> "method")."""
    text = str(heading or "").strip().lower()
    text = _NUMBER_PREFIX.sub("", text)
    text = re.sub(r"[\s_\-]+", " ", text)
    return text.strip(" .:：、")


def classify_heading(heading: str) -> Optional[str]:
    """Map a section heading to a spine slug, or ``None`` when unmapped.

    Matching is substring-based on the normalised heading, longest keyword
    first, which handles "3.2 The Proposed Method" and "四、实验结果与分析".
    """
    normalized = normalize_heading(heading)
    if not normalized:
        return None
    for keyword, slug in _MATCHERS:
        if keyword in normalized:
            return slug
    return None


def classify_text(title: str, body: str = "", hint: str = "") -> Optional[str]:
    """Classify a section using its heading first, then a body-text fallback.

    The fallback scores keyword occurrences so that an unnumbered section can
    still land in the right dimension; ambiguity returns ``None`` and the
    caller decides (see :func:`assign_sections`).
    """
    direct = classify_heading(title) or (classify_heading(hint) if hint else None)
    if direct:
        return direct
    if not body:
        return None
    lowered = body[:4000].lower()
    best_slug: Optional[str] = None
    best_score = 0
    for item in SPINE:
        score = 0
        for keyword in item["keywords"]:
            score += lowered.count(keyword)
        if score > best_score:
            best_score = score
            best_slug = item["slug"]
    # Require a minimal signal so we do not mislabel arbitrary text.
    return best_slug if best_score >= 2 else None


def assign_sections(sections: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Attach a ``section`` slug to each parsed section.

    Unmapped sections fall back to the nearest preceding mapped dimension (so a
    paper's flow is preserved) and are flagged with ``mapped_by='fallback'``;
    when nothing precedes them they go to ``background``.
    """
    assigned: List[Dict[str, Any]] = []
    last_known: Optional[str] = None
    for index, section in enumerate(sections):
        title = str(section.get("title") or section.get("heading") or "")
        body = str(section.get("text") or section.get("content") or "")
        slug = section.get("section") or classify_text(title, body)
        mapped_by = "model" if section.get("section") else "rule"
        if not slug:
            slug = last_known or "background"
            mapped_by = "fallback"
        last_known = slug
        item = dict(section)
        item["section"] = slug
        item["mapped_by"] = mapped_by
        item["ordinal"] = index
        assigned.append(item)
    return assigned


def group_by_section(sections: Sequence[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """Bucket parsed sections by spine slug, preserving order."""
    grouped: Dict[str, List[Dict[str, Any]]] = {slug: [] for slug in SPINE_SLUGS}
    for section in sections:
        slug = section.get("section") or "background"
        grouped.setdefault(slug, []).append(section)
    return grouped


def spine_payload() -> List[Dict[str, Any]]:
    """The shape the ``GET /kg/sections`` endpoint returns."""
    return [
        {
            "slug": item["slug"],
            "label": item["label"],
            "order": item["order"],
            "description": item["description"],
        }
        for item in SPINE
    ]


def order_index(slug: str) -> int:
    return SPINE_ORDER.get(slug, 99)


def label_of(slug: str) -> str:
    return SPINE_LABELS.get(slug, slug)
