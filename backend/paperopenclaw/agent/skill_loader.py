"""Skill loading (FR-9).

A skill is a directory containing ``SKILL.md``::

    skills/<skill-id>/
      SKILL.md          YAML frontmatter + instruction body
      scripts/*.py      optional script tools
      references/       optional supporting files

The filesystem is authoritative and the database is a cache, so a skill can be
authored by hand, dropped into the directory, and picked up by re-scanning.

Two things a skill can contribute:

* **instructions** — prose injected into an agent's system prompt, which is how
  a skill teaches a way of working;
* **tools** — declared in frontmatter and registered into the tool registry.

Tool execution is deliberately conservative. ``script`` tools exchange data with
the child process through **files, not pipes**: arguments go in as a JSON file
and the result comes back as a JSON file. That works identically on every
platform and avoids the pipe-related failures that sandboxed environments
exhibit. ``http`` tools perform a templated request. Neither can be triggered
unless the skill is enabled *and* the calling agent has the tool in its allowlist.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import yaml

from ..core.errors import BadRequest, SkillNotFound
from ..core.logging import get_logger
from ..core.paths import DataPaths
from ..storage.repos.skills import SkillRepository
from .tools import Tool, ToolContext, ToolRegistry

logger = get_logger(__name__)

MANIFEST_NAME = "SKILL.md"
DEFAULT_TIMEOUT = 120.0
SCRIPT_SUFFIXES = {".py"}


@dataclass
class SkillManifest:
    """Parsed ``SKILL.md``."""

    skill_id: str
    directory: Path
    name: str = ""
    description: str = ""
    version: str = ""
    author: str = ""
    instructions: str = ""
    tools: List[Dict[str, Any]] = field(default_factory=list)
    frontmatter: Dict[str, Any] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)

    @property
    def tool_names(self) -> List[str]:
        return [str(item.get("name")) for item in self.tools if item.get("name")]

    def to_row(self, paths: DataPaths) -> Dict[str, Any]:
        return {
            "name": self.name or self.skill_id,
            "description": self.description,
            "version": self.version,
            "author": self.author,
            "path": paths.to_relative(self.directory),
            "tools": self.tool_names,
            "instructions": self.instructions,
            "frontmatter": self.frontmatter,
        }


# ---------------------------------------------------------------------------
# manifest parsing
# ---------------------------------------------------------------------------

def parse_manifest(path: Path, skill_id: str = "") -> SkillManifest:
    """Parse ``SKILL.md``; tolerate a missing or partial frontmatter block."""
    text = path.read_text(encoding="utf-8", errors="replace")
    manifest = SkillManifest(skill_id=skill_id or path.parent.name, directory=path.parent)

    frontmatter: Dict[str, Any] = {}
    body = text
    if text.lstrip().startswith("---"):
        stripped = text.lstrip()
        lines = stripped.split("\n")
        closing = None
        for index in range(1, len(lines)):
            if lines[index].strip() == "---":
                closing = index
                break
        if closing is not None:
            raw = "\n".join(lines[1:closing])
            try:
                loaded = yaml.safe_load(raw)
                if isinstance(loaded, dict):
                    frontmatter = loaded
            except yaml.YAMLError as exc:
                manifest.warnings.append(f"frontmatter 解析失败：{exc}")
            body = "\n".join(lines[closing + 1 :])
        else:
            manifest.warnings.append("frontmatter 缺少结束分隔符，已按纯文本处理")

    manifest.frontmatter = frontmatter
    manifest.name = str(frontmatter.get("name") or manifest.skill_id)
    manifest.description = str(frontmatter.get("description") or "")
    manifest.version = str(frontmatter.get("version") or "")
    manifest.author = str(frontmatter.get("author") or "")
    manifest.instructions = body.strip()

    if not manifest.description:
        # Fall back to the first non-heading line so the catalogue is useful
        # even for a minimal SKILL.md.
        for line in manifest.instructions.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                manifest.description = stripped[:200]
                break

    raw_tools = frontmatter.get("tools") or []
    if isinstance(raw_tools, dict):
        raw_tools = [raw_tools]
    if isinstance(raw_tools, str):
        raw_tools = [{"name": raw_tools}]
    for entry in raw_tools or []:
        if isinstance(entry, str):
            manifest.tools.append({"name": entry, "type": "script"})
        elif isinstance(entry, dict) and entry.get("name"):
            manifest.tools.append(dict(entry))

    return manifest


# ---------------------------------------------------------------------------
# declarative tool handlers
# ---------------------------------------------------------------------------

def _render_template(value: Any, arguments: Dict[str, Any]) -> Any:
    """Substitute ``{arg}`` placeholders in strings inside a structure."""
    if isinstance(value, str):
        try:
            return value.format(**{key: _stringify(item) for key, item in arguments.items()})
        except (KeyError, IndexError, ValueError):
            return value
    if isinstance(value, dict):
        return {key: _render_template(item, arguments) for key, item in value.items()}
    if isinstance(value, list):
        return [_render_template(item, arguments) for item in value]
    return value


def _stringify(value: Any) -> str:
    if isinstance(value, (str, int, float, bool)):
        return str(value)
    return json.dumps(value, ensure_ascii=False)


def _script_handler(manifest: SkillManifest, declaration: Dict[str, Any]):
    """Run a skill script, exchanging JSON through files instead of pipes."""
    script_rel = str(declaration.get("script") or declaration.get("path") or "").strip()
    timeout = float(declaration.get("timeout") or DEFAULT_TIMEOUT)

    def handler(context: ToolContext, arguments: Dict[str, Any]) -> Dict[str, Any]:
        if not script_rel:
            return {"error": "技能工具未声明 script 字段"}
        script_path = (manifest.directory / script_rel).resolve()
        # Containment: a skill must not reach outside its own directory.
        if manifest.directory.resolve() not in script_path.parents:
            return {"error": "脚本路径越出技能目录"}
        if not script_path.is_file():
            return {"error": f"脚本不存在：{script_rel}"}
        if script_path.suffix.lower() not in SCRIPT_SUFFIXES:
            return {"error": f"不支持的脚本类型：{script_path.suffix}"}

        workspace = Path(context.workspace) if context.workspace else Path(tempfile.mkdtemp(prefix="skill_"))
        workspace.mkdir(parents=True, exist_ok=True)
        args_file = workspace / f"{manifest.skill_id}_{declaration['name']}_args.json"
        out_file = workspace / f"{manifest.skill_id}_{declaration['name']}_out.json"
        args_file.write_text(json.dumps({
            "arguments": arguments,
            "session_id": context.session_id,
            "agent_id": context.agent_id,
            "allowed_hashes": context.allowed_hashes or [],
        }, ensure_ascii=False), encoding="utf-8")
        if out_file.exists():
            out_file.unlink()

        command = [sys.executable, str(script_path), str(args_file), str(out_file)]
        try:
            completed = subprocess.run(
                command,
                cwd=str(manifest.directory),
                timeout=timeout,
                capture_output=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return {"error": f"脚本执行超时（>{timeout:.0f}s）"}
        except Exception as exc:  # noqa: BLE001
            return {"error": f"脚本执行失败：{exc}"}

        if not out_file.is_file():
            return {"error": f"脚本未产生输出文件（退出码 {completed.returncode}）"}
        try:
            return json.loads(out_file.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            return {"error": f"脚本输出不是合法 JSON：{exc}"}

    return handler


def _http_handler(manifest: SkillManifest, declaration: Dict[str, Any]):
    """Perform a templated HTTP request declared by a skill."""
    import httpx

    method = str(declaration.get("method") or "POST").upper()
    url_template = str(declaration.get("url") or "")
    headers = declaration.get("headers") or {}
    timeout = float(declaration.get("timeout") or DEFAULT_TIMEOUT)

    def handler(context: ToolContext, arguments: Dict[str, Any]) -> Dict[str, Any]:
        if not url_template:
            return {"error": "技能工具未声明 url 字段"}
        url = _render_template(url_template, arguments)
        if not str(url).startswith(("http://", "https://")):
            return {"error": "只允许 http/https 地址"}
        try:
            with httpx.Client(timeout=timeout) as client:
                if method in ("GET", "DELETE"):
                    response = client.request(method, str(url), headers=headers)
                else:
                    payload = _render_template(declaration.get("body") or arguments, arguments)
                    response = client.request(method, str(url), headers=headers, json=payload)
        except Exception as exc:  # noqa: BLE001
            return {"error": f"请求失败：{exc}"}
        text = response.text[:8000]
        try:
            return {"status": response.status_code, "json": json.loads(text)}
        except Exception:
            return {"status": response.status_code, "text": text}

    return handler


def build_tool_from_declaration(manifest: SkillManifest, declaration: Dict[str, Any]) -> Optional[Tool]:
    """Turn one frontmatter tool entry into a registrable :class:`Tool`."""
    name = str(declaration.get("name") or "").strip()
    if not name:
        return None
    kind = str(declaration.get("type") or "script").lower()
    parameters = declaration.get("parameters") or {"type": "object", "properties": {}}
    if not isinstance(parameters, dict):
        parameters = {"type": "object", "properties": {}}
    parameters.setdefault("type", "object")
    parameters.setdefault("properties", {})

    if kind == "http":
        handler = _http_handler(manifest, declaration)
    else:
        handler = _script_handler(manifest, declaration)

    return Tool(
        name=name,
        description=str(declaration.get("description") or f"由技能 {manifest.skill_id} 提供的工具"),
        parameters=parameters,
        handler=handler,
        source=f"skill:{manifest.skill_id}",
    )


# ---------------------------------------------------------------------------
# loader
# ---------------------------------------------------------------------------

class SkillLoader:
    """Discovers, persists and applies skills."""

    def __init__(self, paths: DataPaths, repository: SkillRepository) -> None:
        self.paths = paths
        self.repository = repository
        self._manifests: Dict[str, SkillManifest] = {}

    # ------------------------------------------------------------------
    def scan(self, register_tools: bool = True, registry: Optional[ToolRegistry] = None) -> List[SkillManifest]:
        """Synchronise the skills directory with the registry."""
        directory = self.paths.skills_dir
        directory.mkdir(parents=True, exist_ok=True)
        found: List[SkillManifest] = []
        seen: set = set()

        for child in sorted(directory.iterdir()):
            manifest_path = child / MANIFEST_NAME
            if not child.is_dir() or not manifest_path.is_file():
                continue
            try:
                manifest = parse_manifest(manifest_path, child.name)
            except Exception as exc:  # noqa: BLE001 - one bad skill must not break the scan
                logger.warning("技能 %s 解析失败：%s", child.name, exc)
                continue
            found.append(manifest)
            seen.add(manifest.skill_id)
            self._manifests[manifest.skill_id] = manifest
            self.repository.upsert(manifest.skill_id, **manifest.to_row(self.paths))

        # Skills whose directory disappeared must not linger in the catalogue.
        for row in self.repository.list():
            if row["id"] not in seen:
                logger.info("技能目录已移除，从登记表删除：%s", row["id"])
                self.repository.delete(row["id"])
                self._manifests.pop(row["id"], None)

        if register_tools and registry is not None:
            self.apply_tools(registry)

        logger.info("技能扫描完成：%s 个", len(found))
        return found

    def apply_tools(self, registry: ToolRegistry) -> int:
        """(Re)register the tools of every enabled skill."""
        count = 0
        for row in self.repository.list():
            registry.unregister_source(f"skill:{row['id']}")
        for row in self.repository.list(enabled_only=True):
            manifest = self.manifest(row["id"])
            if manifest is None:
                continue
            for declaration in manifest.tools:
                tool = build_tool_from_declaration(manifest, declaration)
                if tool is None:
                    continue
                registry.register(tool)
                count += 1
        return count

    # ------------------------------------------------------------------
    def manifest(self, skill_id: str) -> Optional[SkillManifest]:
        cached = self._manifests.get(skill_id)
        if cached is not None and cached.directory.is_dir():
            return cached
        manifest_path = self.paths.skill_dir(skill_id) / MANIFEST_NAME
        if not manifest_path.is_file():
            return None
        try:
            manifest = parse_manifest(manifest_path, skill_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("技能 %s 解析失败：%s", skill_id, exc)
            return None
        self._manifests[skill_id] = manifest
        return manifest

    def detail(self, skill_id: str) -> Dict[str, Any]:
        row = self.repository.get(skill_id)
        if row is None:
            raise SkillNotFound(f"技能不存在：{skill_id}")
        manifest = self.manifest(skill_id)
        scripts: List[str] = []
        directory = self.paths.skill_dir(skill_id)
        if directory.is_dir():
            scripts = sorted(
                str(path.relative_to(directory).as_posix())
                for path in directory.rglob("*")
                if path.is_file() and path.name != MANIFEST_NAME
            )
        return {
            "skill": row,
            "instructions": (manifest.instructions if manifest else row.get("instructions", "")),
            "frontmatter": (manifest.frontmatter if manifest else row.get("frontmatter", {})),
            "tool_declarations": (manifest.tools if manifest else []),
            "files": scripts,
            "warnings": (manifest.warnings if manifest else []),
        }

    def enabled_instructions(self, limit: int = 8) -> str:
        """Instructions of every enabled skill, for the agent system prompt."""
        blocks: List[str] = []
        for row in self.repository.list(enabled_only=True)[:limit]:
            manifest = self.manifest(row["id"])
            text = (manifest.instructions if manifest else row.get("instructions", "")) or ""
            if text.strip():
                blocks.append(f"### 技能：{row['name']}（{row['id']}）\n{text.strip()}")
        return "\n\n".join(blocks)

    # ------------------------------------------------------------------
    # install / uninstall
    # ------------------------------------------------------------------
    def install_from_directory(self, source: Path, *, enable: bool = True,
                               overwrite: bool = False) -> Dict[str, Any]:
        source = Path(source).expanduser().resolve()
        if not source.is_dir():
            raise BadRequest(f"技能目录不存在：{source}")
        if not (source / MANIFEST_NAME).is_file():
            raise BadRequest(f"目录中缺少 {MANIFEST_NAME}：{source}")

        skill_id = source.name
        target = self.paths.skill_dir(skill_id)
        if target.exists():
            if not overwrite:
                raise BadRequest(f"技能已存在：{skill_id}（如需覆盖请设置 overwrite）")
            shutil.rmtree(target, ignore_errors=True)
        shutil.copytree(source, target)

        manifest = parse_manifest(target / MANIFEST_NAME, skill_id)
        self._manifests[skill_id] = manifest
        self.repository.upsert(skill_id, enabled=enable, source=str(source),
                               **manifest.to_row(self.paths))
        return self.repository.get(skill_id) or {}

    def install_from_zip(self, archive: Path, *, enable: bool = True,
                         overwrite: bool = False) -> Dict[str, Any]:
        archive = Path(archive).expanduser().resolve()
        if not archive.is_file():
            raise BadRequest(f"压缩包不存在：{archive}")

        with tempfile.TemporaryDirectory(prefix="skill_zip_") as tmp:
            extract_root = Path(tmp)
            with zipfile.ZipFile(archive) as handle:
                for member in handle.namelist():
                    # Zip-slip guard: reject absolute paths and parent traversal.
                    normalized = Path(member.replace("\\", "/"))
                    if normalized.is_absolute() or ".." in normalized.parts:
                        raise BadRequest(f"压缩包含有非法路径：{member}")
                handle.extractall(extract_root)

            manifest_dir = _find_manifest_dir(extract_root)
            if manifest_dir is None:
                raise BadRequest(f"压缩包中未找到 {MANIFEST_NAME}")
            return self.install_from_directory(manifest_dir, enable=enable, overwrite=overwrite)

    def uninstall(self, skill_id: str) -> bool:
        directory = self.paths.skill_dir(skill_id)
        if directory.is_dir():
            shutil.rmtree(directory, ignore_errors=True)
        self._manifests.pop(skill_id, None)
        return self.repository.delete(skill_id)

    def set_enabled(self, skill_id: str, enabled: bool) -> Dict[str, Any]:
        if self.repository.get(skill_id) is None:
            raise SkillNotFound(f"技能不存在：{skill_id}")
        return self.repository.set_enabled(skill_id, enabled) or {}

    def reload(self, skill_id: str) -> Dict[str, Any]:
        manifest = self.manifest(skill_id)
        if manifest is None:
            raise SkillNotFound(f"技能不存在或清单不可读：{skill_id}")
        self.repository.upsert(skill_id, **manifest.to_row(self.paths))
        return self.repository.get(skill_id) or {}


def _find_manifest_dir(root: Path) -> Optional[Path]:
    """Locate the skill root inside an extracted archive."""
    if (root / MANIFEST_NAME).is_file():
        return root
    candidates = sorted(
        path.parent for path in root.rglob(MANIFEST_NAME) if path.is_file()
    )
    if not candidates:
        return None
    # Prefer the shallowest match: archives often wrap everything in one folder.
    return min(candidates, key=lambda path: len(path.relative_to(root).parts))
