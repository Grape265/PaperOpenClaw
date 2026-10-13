"""OKF v0.2 bundle reading, writing and conformance checking.

Implements the parts of the Open Knowledge Format that this application relies
on (FR-5):

* concept documents = UTF-8 Markdown with a YAML frontmatter block whose only
  always-required key is a non-empty ``type`` (spec §4.1);
* reserved filenames ``index.md`` and ``log.md`` which must never be concepts
  (spec §3.1, §8, §9);
* bundle-relative (``/``-prefixed) links between concepts (spec §6.1);
* conformance = every non-reserved ``.md`` file parses with a non-empty ``type``
  (spec §11).

Serialisation is done by hand rather than by ``yaml.dump`` so the output ordering
is stable and diff-friendly: ``type``/``title``/``description`` first, then the
project-specific keys, then the trust and provenance families.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import yaml

RESERVED_FILENAMES = {"index.md", "log.md"}
FRONTMATTER_DELIMITER = "---"

_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
_DELIMITER_LINE = re.compile(r"^---\s*$")

# Frontmatter keys emitted in this order; anything else follows alphabetically.
PREFERRED_KEY_ORDER = (
    "type", "title", "description", "resource", "tags",
    "paper_hash", "section", "okf_version",
    "status", "generated", "verified", "stale_after", "sources",
)


class OKFError(ValueError):
    """Malformed OKF concept document."""


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------

def split_frontmatter(text: str) -> Tuple[str, str]:
    """Split a document into ``(frontmatter_yaml, body)``.

    A document without a leading delimiter yields ``("", text)`` rather than
    raising, so the conformance checker can report it as an error instead of
    crashing on the first bad file (spec §11).
    """
    if text is None:
        return "", ""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.startswith(FRONTMATTER_DELIMITER):
        return "", normalized
    lines = normalized.split("\n")
    if not _DELIMITER_LINE.match(lines[0]):
        return "", normalized
    for index in range(1, len(lines)):
        if _DELIMITER_LINE.match(lines[index]):
            frontmatter = "\n".join(lines[1:index])
            body = "\n".join(lines[index + 1 :])
            return frontmatter, body.lstrip("\n")
    return "", normalized


def parse_concept(text: str) -> Dict[str, Any]:
    """Parse a concept document into frontmatter, body, raw and links."""
    frontmatter_text, body = split_frontmatter(text)
    if not frontmatter_text.strip():
        raise OKFError("缺少 YAML frontmatter")
    try:
        data = yaml.safe_load(frontmatter_text)
    except yaml.YAMLError as exc:
        raise OKFError(f"frontmatter 不是合法 YAML：{exc}") from exc
    if not isinstance(data, dict):
        raise OKFError("frontmatter 必须是键值映射")
    if not str(data.get("type") or "").strip():
        raise OKFError("frontmatter 缺少非空的 type 字段")
    return {
        "frontmatter": data,
        "body": body,
        "raw": text,
        "links": extract_links(body),
    }


def extract_links(body: str) -> List[str]:
    """Markdown link targets, normalised away from anchors."""
    targets: List[str] = []
    for match in _LINK_RE.finditer(body or ""):
        target = match.group(1).strip()
        if target.startswith(("http://", "https://", "mailto:")):
            targets.append(target)
            continue
        target = target.split("#", 1)[0].strip()
        if target and target not in targets:
            targets.append(target)
    return targets


# ---------------------------------------------------------------------------
# serialisation
# ---------------------------------------------------------------------------

def _order_keys(frontmatter: Dict[str, Any]) -> Dict[str, Any]:
    ordered: Dict[str, Any] = {}
    for key in PREFERRED_KEY_ORDER:
        if key in frontmatter and frontmatter[key] is not None:
            ordered[key] = frontmatter[key]
    for key in sorted(frontmatter):
        if key not in ordered and frontmatter[key] is not None:
            ordered[key] = frontmatter[key]
    return ordered


def dump_concept(frontmatter: Dict[str, Any], body: str = "") -> str:
    """Serialise a concept document with stable key ordering."""
    data = _order_keys(dict(frontmatter))
    if not str(data.get("type") or "").strip():
        raise OKFError("frontmatter 必须包含非空的 type 字段")
    yaml_text = yaml.safe_dump(
        data,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
        width=100,
    ).rstrip()
    body_text = (body or "").strip()
    return f"{FRONTMATTER_DELIMITER}\n{yaml_text}\n{FRONTMATTER_DELIMITER}\n\n{body_text}\n"


def build_index_markdown(sections: Sequence[Tuple[str, Sequence[Tuple[str, str, str]]]],
                         okf_version: str = "") -> str:
    """Render an ``index.md`` (spec §8).

    ``sections`` is a sequence of ``(heading, [(title, url, description), ...])``.
    """
    parts: List[str] = []
    if okf_version:
        # The bundle-root index is the only index allowed frontmatter (spec §12).
        parts.append(f"{FRONTMATTER_DELIMITER}\nokf_version: \"{okf_version}\"\n{FRONTMATTER_DELIMITER}\n")
    for heading, entries in sections:
        if not entries:
            continue
        parts.append(f"# {heading}\n")
        for title, url, description in entries:
            line = f"* [{title}]({url})"
            if description:
                line += f" - {description}"
            parts.append(line)
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def build_log_markdown(entries: Sequence[Tuple[str, Sequence[Tuple[str, str]]]]) -> str:
    """Render a ``log.md`` (spec §9); entries are ``(date, [(kind, text), ...])``."""
    parts = ["# 目录更新日志", ""]
    for date, items in entries:
        parts.append(f"## {date}")
        for kind, text in items:
            parts.append(f"* **{kind}**: {text}")
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


# ---------------------------------------------------------------------------
# bundle access
# ---------------------------------------------------------------------------

def concept_id_for(path: Path, bundle_root: Path) -> str:
    """Bundle-relative concept id: path minus ``.md``, POSIX separators."""
    relative = path.resolve().relative_to(bundle_root.resolve())
    return relative.with_suffix("").as_posix()


def load_concept(path: Path) -> Dict[str, Any]:
    parsed = parse_concept(path.read_text(encoding="utf-8"))
    parsed["path"] = str(path)
    return parsed


def iter_concepts(bundle_root: Path) -> Iterable[Path]:
    if not bundle_root.is_dir():
        return []
    return sorted(
        path for path in bundle_root.rglob("*.md")
        if path.is_file() and path.name not in RESERVED_FILENAMES
    )


def build_tree(bundle_root: Path) -> List[Dict[str, Any]]:
    """Concept tree for the OKF viewer endpoint.

    Nested by directory; ``index.md`` files are skipped because they are
    navigation, not knowledge (spec §3.1).
    """
    if not bundle_root.is_dir():
        return []

    root_nodes: List[Dict[str, Any]] = []

    def node_for(directory: Path, title_override: str = "") -> List[Dict[str, Any]]:
        nodes: List[Dict[str, Any]] = []
        try:
            entries = sorted(directory.iterdir(), key=lambda p: (p.is_file(), p.name))
        except OSError:  # pragma: no cover - unreadable directory
            return nodes
        for entry in entries:
            if entry.is_dir():
                children = node_for(entry)
                if children:
                    nodes.append({
                        "concept_id": f"{concept_id_for(entry, bundle_root)}/",
                        "type": "Directory",
                        "title": title_override or entry.name,
                        "description": "",
                        "children": children,
                    })
            elif entry.suffix == ".md" and entry.name not in RESERVED_FILENAMES:
                try:
                    parsed = load_concept(entry)
                except OKFError as exc:
                    nodes.append({
                        "concept_id": concept_id_for(entry, bundle_root),
                        "type": "Malformed",
                        "title": entry.stem,
                        "description": f"解析失败：{exc}",
                        "children": [],
                    })
                    continue
                frontmatter = parsed["frontmatter"]
                nodes.append({
                    "concept_id": concept_id_for(entry, bundle_root),
                    "type": str(frontmatter.get("type") or "Unknown"),
                    "title": str(frontmatter.get("title") or entry.stem),
                    "description": str(frontmatter.get("description") or ""),
                    "section": str(frontmatter.get("section") or ""),
                    "children": [],
                })
        return nodes

    root_nodes = node_for(bundle_root)
    return root_nodes


def validate_bundle(bundle_root: Path, okf_version: str = "0.2") -> Dict[str, Any]:
    """Conformance check per spec §11, plus advisory warnings."""
    errors: List[Dict[str, str]] = []
    warnings: List[Dict[str, str]] = []
    checked = 0

    if not bundle_root.is_dir():
        return {
            "conformant": False,
            "files": 0,
            "errors": [{"file": str(bundle_root), "message": "OKF 目录不存在"}],
            "warnings": [],
            "okf_version": okf_version,
        }

    for path in sorted(bundle_root.rglob("*.md")):
        if not path.is_file():
            continue
        relative = path.relative_to(bundle_root).as_posix()
        text = path.read_text(encoding="utf-8", errors="replace")

        if path.name in RESERVED_FILENAMES:
            if path.name == "index.md":
                body = text
                if text.startswith(FRONTMATTER_DELIMITER):
                    frontmatter_text, body = split_frontmatter(text)
                    if relative != "index.md":
                        warnings.append({"file": relative,
                                         "message": "只有 bundle 根部的 index.md 允许 frontmatter"})
                    elif "okf_version" not in (yaml.safe_load(frontmatter_text) or {}):
                        warnings.append({"file": relative,
                                         "message": "index.md frontmatter 建议包含 okf_version"})
                if not body.strip():
                    warnings.append({"file": relative, "message": "index.md 内容为空"})
            else:
                if not re.search(r"^##\s+\d{4}-\d{2}-\d{2}\s*$", text, re.MULTILINE):
                    warnings.append({"file": relative,
                                     "message": "log.md 缺少 YYYY-MM-DD 形式的日期标题"})
            continue

        checked += 1
        try:
            parsed = parse_concept(text)
        except OKFError as exc:
            errors.append({"file": relative, "message": str(exc)})
            continue

        frontmatter = parsed["frontmatter"]
        if not frontmatter.get("title"):
            warnings.append({"file": relative, "message": "建议填写 title"})
        if not frontmatter.get("description"):
            warnings.append({"file": relative, "message": "建议填写 description"})
        if "generated" in frontmatter:
            generated = frontmatter["generated"]
            if not isinstance(generated, dict) or "by" not in generated:
                errors.append({"file": relative, "message": "generated 必须包含 by 字段（规范 §5.2）"})
        for target in parsed["links"]:
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            base = bundle_root if target.startswith("/") else path.parent
            resolved = (base / target.lstrip("/")).resolve()
            if not resolved.exists():
                # Broken links are explicitly tolerated by the spec (§11), but
                # they are worth surfacing while authoring a bundle.
                warnings.append({"file": relative, "message": f"链接目标不存在：{target}"})

    return {
        "conformant": not errors,
        "files": checked,
        "errors": errors,
        "warnings": warnings,
        "okf_version": okf_version,
    }
