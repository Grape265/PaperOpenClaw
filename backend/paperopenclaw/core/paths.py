"""Runtime data directory layout (content-addressed) and path safety.

Layout (docs/02-architecture.md section 2.2)::

    <data_dir>/
      config.json
      db/paperopenclaw.sqlite3
      db/vectors/               (ChromaDB)
      papers/<sha256>/paper.pdf
      papers/<sha256>/meta.json
      papers/<sha256>/fig/fig1.png ...     <- FR-4.3
      papers/<sha256>/tab/tab1.png ...     <- FR-4.3
      papers/<sha256>/okf/                 <- FR-5
      skills/<skill-id>/SKILL.md
      agents/<session-id>/<agent-id>/

Every path handed to the outside world is *relative to the data directory* and
resolved back through :meth:`DataPaths.resolve`, which refuses to escape the
root (see ``PATH_OUT_OF_SCOPE``).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Iterable, List, Optional, Union

from .errors import PathOutOfScope

ENV_DATA_DIR = "PAPEROPENCLAW_DATA_DIR"

DEFAULT_IMAGE_DPI = 200


class DataPaths:
    """Resolves and creates every location the application writes to."""

    def __init__(self, root: Union[str, Path]) -> None:
        self.root = Path(root).expanduser().resolve()

    # ------------------------------------------------------------------
    # top level
    # ------------------------------------------------------------------
    @property
    def config_file(self) -> Path:
        return self.root / "config.json"

    @property
    def db_dir(self) -> Path:
        return self.root / "db"

    @property
    def sqlite_file(self) -> Path:
        return self.db_dir / "paperopenclaw.sqlite3"

    @property
    def vectors_dir(self) -> Path:
        return self.db_dir / "vectors"

    @property
    def papers_dir(self) -> Path:
        return self.root / "papers"

    @property
    def skills_dir(self) -> Path:
        return self.root / "skills"

    @property
    def agents_dir(self) -> Path:
        return self.root / "agents"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    @property
    def models_dir(self) -> Path:
        return self.root / "models"

    @property
    def tmp_dir(self) -> Path:
        """Scratch space for the process.

        Deliberately inside the data directory: the system temp volume can be
        tiny or full (a very common situation on Windows), and the work done here
        — extracting a skill zip, writing a graph export — would otherwise fail
        or leave files outside the directory the user backs up.
        """
        return self.root / "tmp"

    # ------------------------------------------------------------------
    # per paper
    # ------------------------------------------------------------------
    def paper_dir(self, hash_value: str) -> Path:
        return self.papers_dir / hash_value

    def paper_pdf(self, hash_value: str) -> Path:
        return self.paper_dir(hash_value) / "paper.pdf"

    def paper_meta(self, hash_value: str) -> Path:
        return self.paper_dir(hash_value) / "meta.json"

    def paper_fig_dir(self, hash_value: str) -> Path:
        """``papers/<hash>/fig`` - FR-4.3."""
        return self.paper_dir(hash_value) / "fig"

    def paper_tab_dir(self, hash_value: str) -> Path:
        """``papers/<hash>/tab`` - FR-4.3."""
        return self.paper_dir(hash_value) / "tab"

    def paper_okf_dir(self, hash_value: str) -> Path:
        """OKF v0.2 bundle root - FR-5."""
        return self.paper_dir(hash_value) / "okf"

    def asset_file(self, hash_value: str, label: str) -> Path:
        """Resolve ``fig1``/``tab3`` to its PNG path under fig/ or tab/."""
        bucket = "fig" if label.startswith("fig") else "tab"
        base = self.paper_fig_dir(hash_value) if bucket == "fig" else self.paper_tab_dir(hash_value)
        return base / f"{label}.png"

    # ------------------------------------------------------------------
    # agents / skills
    # ------------------------------------------------------------------
    def agent_dir(self, session_id: str, agent_id: str) -> Path:
        """Isolated scratch workspace for one sub-agent (FR-10.2)."""
        return self.agents_dir / _safe_segment(session_id) / _safe_segment(agent_id)

    def skill_dir(self, skill_id: str) -> Path:
        return self.skills_dir / _safe_segment(skill_id)

    # ------------------------------------------------------------------
    # relative path helpers
    # ------------------------------------------------------------------
    def to_relative(self, path: Union[str, Path]) -> str:
        """Return a POSIX-style path relative to the data root.

        Stored in the database so the whole data directory stays movable
        across operating systems (NFR-1).
        """
        target = Path(path).resolve()
        try:
            rel = target.relative_to(self.root)
        except ValueError as exc:  # pragma: no cover - defensive
            raise PathOutOfScope(f"路径不在数据目录内：{path}") from exc
        return rel.as_posix()

    def resolve(self, relative: str) -> Path:
        """Resolve a stored relative path back to an absolute, in-scope path."""
        candidate = (self.root / str(relative).replace("\\", "/")).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise PathOutOfScope(f"路径越出数据目录：{relative}")
        return candidate

    def is_inside(self, path: Union[str, Path]) -> bool:
        try:
            resolved = Path(path).resolve()
        except OSError:  # pragma: no cover - defensive
            return False
        return resolved == self.root or self.root in resolved.parents

    # ------------------------------------------------------------------
    # creation
    # ------------------------------------------------------------------
    def ensure_base(self) -> "DataPaths":
        """Create every directory needed before the database is opened."""
        for directory in (
            self.root,
            self.db_dir,
            self.vectors_dir,
            self.papers_dir,
            self.skills_dir,
            self.agents_dir,
            self.logs_dir,
            self.models_dir,
            self.tmp_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        configure_temp_dir(self.tmp_dir)
        return self

    def ensure_paper_dirs(self, hash_value: str) -> "DataPaths":
        """Create the per-paper tree including empty ``fig/`` and ``tab/`` (FR-4.5)."""
        for directory in (
            self.paper_dir(hash_value),
            self.paper_fig_dir(hash_value),
            self.paper_tab_dir(hash_value),
            self.paper_okf_dir(hash_value),
        ):
            directory.mkdir(parents=True, exist_ok=True)
        return self

    def iter_okf_markdown(self, hash_value: str) -> Iterable[Path]:
        """Yield every Markdown file in a paper's OKF bundle, sorted."""
        bundle = self.paper_okf_dir(hash_value)
        if not bundle.is_dir():
            return []
        return sorted(p for p in bundle.rglob("*.md") if p.is_file())

    def list_paper_dirs(self) -> List[Path]:
        if not self.papers_dir.is_dir():
            return []
        return sorted(p for p in self.papers_dir.iterdir() if p.is_dir())

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"DataPaths(root={str(self.root)!r})"


def _safe_segment(segment: str) -> str:
    """Reject identifiers that would escape their parent directory."""
    text = str(segment).strip()
    if not text or text in {".", ".."}:
        raise PathOutOfScope(f"非法标识符：{segment!r}")
    if any(sep in text for sep in ("/", "\\", "\x00")):
        raise PathOutOfScope(f"标识符不得包含路径分隔符：{segment!r}")
    if os.path.isabs(text):  # pragma: no cover - defensive
        raise PathOutOfScope(f"标识符不得为绝对路径：{segment!r}")
    return text


def resolve_data_dir(explicit: Optional[Union[str, Path]] = None) -> Path:
    """Resolve the data directory.

    Order: explicit argument -> ``PAPEROPENCLAW_DATA_DIR`` -> a per-user home
    directory when running as a frozen desktop app (PyInstaller) -> ``<cwd>/data``.
    """
    if explicit:
        return Path(explicit).expanduser().resolve()
    from_env = os.environ.get(ENV_DATA_DIR)
    if from_env:
        return Path(from_env).expanduser().resolve()
    if getattr(sys, "frozen", False):
        # 桌面可执行文件：数据放在用户主目录，避免写入只读的程序安装目录。
        return (Path.home() / ".paperopenclaw").resolve()
    return (Path.cwd() / "data").resolve()


def configure_temp_dir(directory: Union[str, Path]) -> bool:
    """Point :mod:`tempfile` at ``directory`` for the rest of the process.

    Returns whether the redirect succeeded. Anywhere the app needs scratch space
    it uses ``tempfile``, so redirecting once here covers skill zip extraction,
    graph exports and test workspaces alike. The system temp volume is often too
    small for this workload (or entirely full), and failing there would be a
    confusing way to lose an ingest.

    The redirect is refused when ``directory`` already lies *inside* the current
    temp root. That is the case whenever a caller is itself operating on a
    throwaway directory (tests, or `data_dir` created in the system temp): the
    outer directory is about to be deleted, and re-pointing at a child of it
    would leave a dangling ``tempfile.tempdir`` that breaks every later call.
    """
    import tempfile

    target = Path(directory)
    try:
        resolved_target = target.resolve()
    except OSError:  # pragma: no cover - defensive
        return False

    current = tempfile.tempdir
    if current:
        try:
            current_root = Path(current).resolve()
        except OSError:  # pragma: no cover - defensive
            current_root = None
        if current_root is not None and (
            resolved_target == current_root or current_root in resolved_target.parents
        ):
            return False

    try:
        target.mkdir(parents=True, exist_ok=True)
        probe = target / ".tmp-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except Exception:
        return False
    tempfile.tempdir = str(target)
    return True
