"""Text utilities: slugs, sentence splitting, tokenisation, truncation.

Chinese and English both matter here: papers may be either, and mixed-language
abstracts are common. Tokenisation therefore has to serve retrieval (BM25 in
:mod:`paperopenclaw.memory.lexical`) as well as naming (file slugs).
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Iterable, List, Optional, Sequence

# ---------------------------------------------------------------------------
# slugs
# ---------------------------------------------------------------------------

_SLUG_STRIP = re.compile(r"[^\w\u4e00-\u9fff]+", re.UNICODE)
_CJK = re.compile(r"[\u4e00-\u9fff]")
# 一个或多个连续汉字：用于判断「整段是否纯中文」。`_CJK.fullmatch` 只匹配单个汉字，
# 误用会让双字词落到单字回退，故这里用独立的 `+` 版本。
_CJK_FULL = re.compile(r"[\u4e00-\u9fff]+")

# Reserved OKF filenames: must never be used for concept documents (OKF spec 3.1).
OKF_RESERVED = {"index", "log"}


def slugify(value: str, fallback: str = "section", max_length: int = 48) -> str:
    """Turn arbitrary heading text into a filesystem-safe, URL-safe slug.

    CJK characters are preserved (they are perfectly valid in filenames and keep
    the OKF bundle readable), everything else collapses to ``-``.
    """
    text = unicodedata.normalize("NFKC", str(value or "")).strip().lower()
    text = text.replace("/", "-").replace("\\", "-")
    text = _SLUG_STRIP.sub("-", text)
    text = re.sub(r"-{2,}", "-", text).strip("-._")
    if not text:
        text = fallback
    if text in OKF_RESERVED:
        text = f"{text}-section"
    if len(text) > max_length:
        digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:6]
        text = f"{text[: max_length - 7].rstrip('-')}-{digest}"
    return text


def concept_id(*parts: str) -> str:
    """Build an OKF concept id from path segments."""
    cleaned = [slugify(part) for part in parts if part not in (None, "")]
    return "/".join(cleaned)


# ---------------------------------------------------------------------------
# language detection
# ---------------------------------------------------------------------------

def looks_chinese(text: str, threshold: float = 0.15) -> bool:
    """Heuristic: does the text contain a meaningful share of CJK characters?"""
    if not text:
        return False
    sample = text[:4000]
    cjk = len(_CJK.findall(sample))
    return cjk / max(len(sample), 1) >= threshold


def detect_language(text: str) -> str:
    """Return ``zh``, ``en`` or ``mixed``."""
    if not text:
        return "en"
    sample = text[:4000]
    cjk = len(_CJK.findall(sample))
    ratio = cjk / max(len(sample), 1)
    if ratio >= 0.15:
        return "zh"
    if ratio >= 0.02:
        return "mixed"
    return "en"


# ---------------------------------------------------------------------------
# sentences and tokens
# ---------------------------------------------------------------------------

_SENTENCE_SPLIT = re.compile(r"(?<=[。！？!?；;])\s*|(?<=[.!?])\s+(?=[A-Z0-9\u4e00-\u9fff])")

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_\-]{1,}|[\u4e00-\u9fff]|\d+(?:\.\d+)?")

_EN_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "of", "to", "in", "on", "for", "with",
    "is", "are", "was", "were", "be", "been", "being", "as", "at", "by", "from",
    "that", "this", "these", "those", "it", "its", "we", "our", "they", "their",
    "which", "who", "whom", "what", "when", "where", "how", "why", "can", "could",
    "should", "would", "may", "might", "must", "will", "shall", "do", "does",
    "did", "not", "no", "than", "then", "there", "here", "also", "such", "into",
    "over", "under", "between", "more", "most", "some", "any", "all", "each",
    "other", "only", "both", "very", "using", "used", "use", "based", "however",
}

_ZH_STOPWORDS = {
    "的", "了", "和", "是", "在", "我", "有", "就", "不", "人", "都", "一", "一个",
    "上", "也", "很", "到", "说", "要", "去", "你", "会", "着", "没有", "看", "好",
    "自己", "这", "那", "这些", "那些", "我们", "他们", "以及", "并且", "但是",
    "然而", "因此", "所以", "可以", "能够", "通过", "对于", "关于", "由于", "如果",
    "这个", "那个", "进行", "具有", "成为", "需要", "使用", "采用", "本文", "方法",
}

_jieba_module = None
_jieba_failed = False


def _get_jieba():
    """Import jieba lazily and only once; tolerate a missing install."""
    global _jieba_module, _jieba_failed
    if _jieba_module is not None or _jieba_failed:
        return _jieba_module
    try:  # pragma: no cover - depends on optional runtime state
        import jieba  # type: ignore

        jieba.setLogLevel(60)
        _jieba_module = jieba
    except Exception:
        _jieba_failed = True
        _jieba_module = None
    return _jieba_module


def tokenize(text: str, keep_stopwords: bool = False) -> List[str]:
    """Tokenise mixed Chinese/English text into lowercase terms.

    Falls back to a pure-regex tokeniser when jieba is unavailable, so the
    offline lexical retriever never hard-depends on it (NFR-6).
    """
    if not text:
        return []
    lowered = text.lower()
    tokens: List[str] = []

    jieba = _get_jieba()
    if jieba is not None and _CJK.search(lowered):
        for piece in jieba.lcut(lowered, cut_all=False):
            piece = piece.strip()
            if not piece:
                continue
            if _CJK_FULL.fullmatch(piece):
                if len(piece) == 1 and piece in _ZH_STOPWORDS:
                    continue
                tokens.append(piece)
            else:
                tokens.extend(_TOKEN_RE.findall(piece))
    else:
        tokens.extend(_TOKEN_RE.findall(lowered))

    if not keep_stopwords:
        tokens = [t for t in tokens if t not in _EN_STOPWORDS and t not in _ZH_STOPWORDS and len(t.strip()) > 0]
    return tokens


def split_sentences(text: str, max_sentences: int = 0) -> List[str]:
    """Split text into sentences, keeping CJK punctuation intact."""
    if not text:
        return []
    cleaned = re.sub(r"\s+", " ", text.replace("\n", " ")).strip()
    if not cleaned:
        return []
    parts = [p.strip() for p in _SENTENCE_SPLIT.split(cleaned) if p and p.strip()]
    if max_sentences and len(parts) > max_sentences:
        parts = parts[:max_sentences]
    return parts


def truncate(text: str, max_chars: int, ellipsis: str = "…") -> str:
    """Truncate on a character budget without cutting mid-word where possible."""
    if text is None:
        return ""
    text = str(text)
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    head = text[: max_chars - len(ellipsis)]
    cut = max(head.rfind(" "), head.rfind("，"), head.rfind("。"))
    if cut > max_chars * 0.6:
        head = head[:cut]
    return head.rstrip() + ellipsis


def normalize_whitespace(text: str) -> str:
    """Collapse runs of whitespace; keep paragraph breaks to a single blank line."""
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t\u00a0]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def word_count(text: str) -> int:
    """Approximate length in tokens, used for budgeting model context."""
    if not text:
        return 0
    cjk = len(_CJK.findall(text))
    words = len(re.findall(r"[A-Za-z0-9]+", text))
    return cjk + words


def jaccard(a: Iterable[str], b: Iterable[str]) -> float:
    """Jaccard similarity of two token bags; used for dedup and C-C linking."""
    set_a, set_b = set(a), set(b)
    if not set_a or not set_b:
        return 0.0
    return len(set_a & set_b) / float(len(set_a | set_b))


def keywords(text: str, top_k: int = 12) -> List[str]:
    """Cheap frequency-based keyword extraction (no model call).

    Single CJK characters are dropped: they are near-meaningless as keywords and
    would crowd out the multi-character terms that actually identify a concept.
    """
    counts = {}
    for token in tokenize(text):
        if len(token) < 2:
            continue
        counts[token] = counts.get(token, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [word for word, _ in ranked[:top_k]]


def dedupe_preserve_order(items: Sequence[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for item in items:
        key = item.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def first_non_empty(*values: Optional[str]) -> str:
    """Return the first value that has non-whitespace content."""
    for value in values:
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""
