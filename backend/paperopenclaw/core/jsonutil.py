"""Lenient JSON extraction for model output.

Vision and chat models wrap JSON in prose or fenced code blocks, emit trailing
commas, or use full-width punctuation. Rather than failing the whole ingest
(FR-3.3 still wants retries, but for *real* schema failures), recover what we
can here and let schema validation decide.
"""

from __future__ import annotations

import json
import re
from typing import Any, List, Optional

_FENCE_RE = re.compile(r"```(?:json|JSON|json5)?\s*(.*?)```", re.DOTALL)
_TRAILING_COMMA_RE = re.compile(r",\s*([}\]])")
_SMART_QUOTES = {
    "\u201c": '"', "\u201d": '"', "\u2018": "'", "\u2019": "'",
    "\uff02": '"', "\uff07": "'",
}


class JsonExtractionError(ValueError):
    """Raised when no JSON value can be recovered from the text."""


def _strip_smart_quotes(text: str) -> str:
    for src, dst in _SMART_QUOTES.items():
        text = text.replace(src, dst)
    return text


def _balanced_candidates(text: str) -> List[str]:
    """Yield balanced ``{...}`` / ``[...]`` substrings, outermost first."""
    candidates: List[str] = []
    for opener, closer in (("{", "}"), ("[", "]")):
        depth = 0
        start = -1
        in_string = False
        escaped = False
        for index, char in enumerate(text):
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == opener:
                if depth == 0:
                    start = index
                depth += 1
            elif char == closer:
                if depth > 0:
                    depth -= 1
                    if depth == 0 and start >= 0:
                        candidates.append(text[start : index + 1])
                        start = -1
    # Longest first: the outer object is almost always the payload.
    candidates.sort(key=len, reverse=True)
    return candidates


def _try_loads(candidate: str) -> Optional[Any]:
    for transform in (
        lambda s: s,
        lambda s: _TRAILING_COMMA_RE.sub(r"\1", s),
        lambda s: _TRAILING_COMMA_RE.sub(r"\1", _strip_smart_quotes(s)),
        lambda s: _strip_smart_quotes(s).replace("'", '"'),
    ):
        try:
            return json.loads(transform(candidate))
        except Exception:
            continue
    return None


def extract_json(text: str, expect: str = "object") -> Any:
    """Extract the first JSON value of ``expect`` kind from arbitrary text.

    ``expect`` is ``"object"``, ``"array"`` or ``"any"``.
    """
    if text is None:
        raise JsonExtractionError("空响应，无法提取 JSON")
    raw = str(text).strip()
    if not raw:
        raise JsonExtractionError("空响应，无法提取 JSON")

    wanted = {"object": dict, "array": list}.get(expect)

    # 1. Whole payload is already JSON.
    parsed = _try_loads(raw)
    if parsed is not None and (wanted is None or isinstance(parsed, wanted)):
        return parsed

    # 2. Fenced code block.
    for match in _FENCE_RE.finditer(raw):
        inner = match.group(1).strip()
        parsed = _try_loads(inner)
        if parsed is not None and (wanted is None or isinstance(parsed, wanted)):
            return parsed

    # 3. Balanced scan.
    for candidate in _balanced_candidates(raw):
        parsed = _try_loads(candidate)
        if parsed is not None and (wanted is None or isinstance(parsed, wanted)):
            return parsed

    # 4. Last resort: accept the wrong container kind rather than fail.
    if wanted is not None:
        for candidate in _balanced_candidates(raw):
            parsed = _try_loads(candidate)
            if parsed is not None:
                return parsed

    raise JsonExtractionError("响应中未找到可解析的 JSON")


def safe_json_loads(text: str, default: Any = None) -> Any:
    """Best-effort JSON parse used for database columns holding JSON text."""
    if text is None or text == "":
        return default
    if isinstance(text, (dict, list)):
        return text
    try:
        return json.loads(text)
    except Exception:
        return default


def json_dumps(value: Any, indent: Optional[int] = None) -> str:
    """UTF-8 friendly JSON dump (never escape non-ASCII)."""
    return json.dumps(value, ensure_ascii=False, indent=indent, default=str)


def as_str_list(value: Any) -> List[str]:
    """Coerce a model-provided field into a list of non-empty strings."""
    if value is None:
        return []
    if isinstance(value, str):
        # Split on ASCII and full-width list separators: models mix both.
        parts = re.split(r"[,;、，；\n]|\s+and\s+|\s+与\s+", value)
        return [p.strip() for p in parts if p and p.strip()]
    if isinstance(value, (list, tuple, set)):
        out: List[str] = []
        for item in value:
            if item is None:
                continue
            if isinstance(item, dict):
                text = item.get("name") or item.get("title") or item.get("value") or ""
                if str(text).strip():
                    out.append(str(text).strip())
            elif str(item).strip():
                out.append(str(item).strip())
        return out
    return [str(value).strip()] if str(value).strip() else []


def as_text(value: Any, default: str = "") -> str:
    """Coerce a model-provided field into a string."""
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, (list, tuple)):
        joined = "；".join(as_text(v) for v in value if v is not None)
        return joined.strip()
    if isinstance(value, dict):
        for key in ("text", "value", "content", "name", "title"):
            if key in value:
                return as_text(value[key], default)
        return json_dumps(value)
    return str(value).strip()
