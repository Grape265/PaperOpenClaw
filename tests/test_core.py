"""Core-layer tests: hashing, paths, text, JSON recovery, config.

Runnable two ways::

    python tests/test_core.py        # no pytest required
    pytest tests/test_core.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _harness import run_all  # noqa: E402

from paperopenclaw.config import (  # noqa: E402
    PROVIDER_PRESETS,
    Settings,
    bootstrap_settings,
)
from paperopenclaw.core import hashing, jsonutil, text  # noqa: E402
from paperopenclaw.core.errors import PathOutOfScope  # noqa: E402
from paperopenclaw.core.paths import DataPaths  # noqa: E402


# ---------------------------------------------------------------------------
# hashing
# ---------------------------------------------------------------------------

def test_hash_bytes_matches_hashlib():
    import hashlib

    data = b"paperopenclaw" * 1000
    assert hashing.hash_bytes(data) == hashlib.sha256(data).hexdigest()


def test_hash_file_is_streaming_and_stable():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "x.pdf"
        payload = os.urandom(3 * 1024 * 1024 + 17)
        path.write_bytes(payload)
        first = hashing.hash_file(path, chunk_size=4096)
        second = hashing.hash_file(path, chunk_size=1024 * 1024)
        assert first == second == hashing.hash_bytes(payload)


def test_is_sha256():
    assert hashing.is_sha256("a" * 64)
    assert not hashing.is_sha256("a" * 63)
    assert not hashing.is_sha256("z" * 64)


def test_short_hash_and_stable_id():
    value = hashing.hash_bytes(b"x")
    assert hashing.short_hash(value) == value[:8]
    assert hashing.stable_id("a", "b") == hashing.stable_id("a", "b")
    assert hashing.stable_id("a", "b") != hashing.stable_id("a", "c")


# ---------------------------------------------------------------------------
# paths
# ---------------------------------------------------------------------------

def test_content_addressed_layout():
    with tempfile.TemporaryDirectory() as tmp:
        paths = DataPaths(tmp).ensure_base()
        h = "a" * 64
        paths.ensure_paper_dirs(h)
        assert paths.paper_pdf(h).name == "paper.pdf"
        assert paths.paper_fig_dir(h).name == "fig"
        assert paths.paper_tab_dir(h).name == "tab"
        assert paths.paper_okf_dir(h).name == "okf"
        assert paths.paper_fig_dir(h).is_dir()
        assert paths.paper_tab_dir(h).is_dir()
        assert paths.asset_file(h, "fig1") == paths.paper_fig_dir(h) / "fig1.png"
        assert paths.asset_file(h, "tab2") == paths.paper_tab_dir(h) / "tab2.png"


def test_relative_roundtrip_uses_posix_separators():
    with tempfile.TemporaryDirectory() as tmp:
        paths = DataPaths(tmp).ensure_base()
        h = "b" * 64
        paths.ensure_paper_dirs(h)
        target = paths.paper_fig_dir(h) / "fig1.png"
        relative = paths.to_relative(target)
        assert "\\" not in relative
        assert relative == f"papers/{h}/fig/fig1.png"
        assert paths.resolve(relative) == target.resolve()


def test_resolve_rejects_escape():
    with tempfile.TemporaryDirectory() as tmp:
        paths = DataPaths(tmp).ensure_base()
        for bad in ("../outside.txt", "..\\outside.txt", "../../etc/passwd"):
            try:
                paths.resolve(bad)
            except PathOutOfScope:
                continue
            raise AssertionError(f"应当拒绝越界路径：{bad}")


def test_safe_segment_rejects_traversal():
    paths = DataPaths(tempfile.mkdtemp())
    for bad in ("..", ".", "a/b", "a\\b", ""):
        try:
            paths.agent_dir("session", bad)
        except PathOutOfScope:
            continue
        raise AssertionError(f"应当拒绝非法标识符：{bad!r}")


def test_configure_temp_dir_redirects_and_refuses_nesting():
    """The temp redirect must never point inside the current temp root.

    A data directory created *in* the system temp (tests, throwaway runs) would
    otherwise set `tempfile.tempdir` to a child of a directory that is about to
    be deleted, leaving every later `mkdtemp` failing on a dangling path. That
    exact failure once turned the whole suite red while reporting a disk error,
    which is why it is pinned here.
    """
    from paperopenclaw.core.paths import configure_temp_dir
    from _harness import REPO_ROOT

    saved = tempfile.tempdir
    sandbox = REPO_ROOT / ".tmp" / "tempdir-guard-test"
    sibling = REPO_ROOT / ".tmp" / "tempdir-guard-sibling"
    try:
        sandbox.mkdir(parents=True, exist_ok=True)
        tempfile.tempdir = str(sandbox)

        # A child of the current temp root is refused, and the root is untouched.
        assert configure_temp_dir(sandbox / "inner") is False
        assert tempfile.tempdir == str(sandbox)

        # A data directory created inside the temp root behaves the same way:
        # ensure_base still creates the directory, it just does not move the root.
        throwaway = sandbox / "run1"
        DataPaths(throwaway).ensure_base()
        assert tempfile.tempdir == str(sandbox)
        assert (throwaway / "tmp").is_dir()

        # A directory outside the temp root is accepted.
        assert configure_temp_dir(sibling) is True
        assert Path(tempfile.tempdir or "").resolve() == sibling.resolve()
    finally:
        tempfile.tempdir = saved
        shutil.rmtree(str(sibling), ignore_errors=True)
        shutil.rmtree(str(sandbox), ignore_errors=True)


def test_temp_dir_lives_inside_the_data_directory():
    """Scratch space belongs to the data directory, not the system volume."""
    with tempfile.TemporaryDirectory() as tmp:
        paths = DataPaths(tmp)
        assert paths.tmp_dir == Path(tmp) / "tmp"
        paths.ensure_base()
        assert paths.tmp_dir.is_dir()


# ---------------------------------------------------------------------------
# text
# ---------------------------------------------------------------------------

def test_slugify_handles_cjk_and_reserved_names():
    assert text.slugify("3.1 Model Architecture") == "3-1-model-architecture"
    assert text.slugify("模型架构") == "模型架构"
    assert text.slugify("") == "section"
    # OKF reserved filenames must never be produced (spec 3.1).
    assert text.slugify("index") != "index"
    assert text.slugify("log") != "log"


def test_slugify_is_bounded():
    long_title = "word " * 60
    slug = text.slugify(long_title)
    assert len(slug) <= 48


def test_language_detection():
    assert text.detect_language("本文提出了一种全新的注意力机制") == "zh"
    assert text.detect_language("We propose a novel attention mechanism") == "en"


def test_split_sentences_bilingual():
    zh = text.split_sentences("第一句。第二句！第三句？")
    assert len(zh) == 3
    en = text.split_sentences("First sentence. Second sentence! Third one?")
    assert len(en) == 3


def test_tokenize_drops_stopwords():
    tokens = text.tokenize("The model uses attention and the transformer")
    assert "the" not in tokens
    assert "model" in tokens
    assert "attention" in tokens


def test_truncate_respects_budget():
    assert text.truncate("short", 100) == "short"
    out = text.truncate("x" * 500, 50)
    assert len(out) <= 50


def test_jaccard_and_keywords():
    assert text.jaccard(["a", "b"], ["a", "b"]) == 1.0
    assert text.jaccard(["a"], ["b"]) == 0.0
    words = text.keywords("attention attention attention transformer transformer model")
    assert words[0] == "attention"


# ---------------------------------------------------------------------------
# json recovery
# ---------------------------------------------------------------------------

def test_extract_json_plain():
    assert jsonutil.extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_from_fence_and_prose():
    raw = 'Sure! Here is the result:\n```json\n{"title": "论文", "n": 3}\n```\nHope that helps.'
    assert jsonutil.extract_json(raw) == {"title": "论文", "n": 3}


def test_extract_json_repairs_trailing_comma():
    assert jsonutil.extract_json('{"a": [1, 2,], "b": 3,}') == {"a": [1, 2], "b": 3}


def test_extract_json_nested_braces_in_strings():
    raw = 'prefix {"text": "a } b", "ok": true} suffix'
    assert jsonutil.extract_json(raw) == {"text": "a } b", "ok": True}


def test_extract_json_array_expectation():
    assert jsonutil.extract_json("[1, 2, 3]", expect="array") == [1, 2, 3]


def test_extract_json_raises_on_garbage():
    try:
        jsonutil.extract_json("no json at all here")
    except jsonutil.JsonExtractionError:
        return
    raise AssertionError("应当抛出 JsonExtractionError")


def test_as_str_list_and_as_text():
    assert jsonutil.as_str_list(["a", {"name": "b"}, None, "  "]) == ["a", "b"]
    assert jsonutil.as_str_list("a, b；c") == ["a", "b", "c"]
    assert jsonutil.as_text({"text": "hi"}) == "hi"
    assert jsonutil.as_text(None) == ""


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------

def test_bootstrap_creates_layout_and_config():
    with tempfile.TemporaryDirectory() as tmp:
        paths, store = bootstrap_settings(tmp)
        assert paths.config_file.is_file()
        assert paths.db_dir.is_dir()
        assert paths.vectors_dir.is_dir()
        assert paths.papers_dir.is_dir()
        assert paths.skills_dir.is_dir()
        assert paths.agents_dir.is_dir()
        assert store.current.data_dir == str(paths.root)


def test_settings_patch_preserves_masked_api_key():
    with tempfile.TemporaryDirectory() as tmp:
        paths, store = bootstrap_settings(tmp)
        store.update({"providers": [{"id": "deepseek", "name": "DeepSeek",
                                     "base_url": "https://api.deepseek.com/v1",
                                     "api_key": "sk-realsecret123456", "models": ["deepseek-chat"]}]})
        masked = store.current.masked_dict()
        assert masked["providers"][0]["api_key"] != "sk-realsecret123456"
        assert masked["providers"][0]["api_key_set"] is True

        # Frontend echoes the mask back; the real secret must survive.
        store.update({"providers": masked["providers"]})
        assert store.current.providers[0].api_key == "sk-realsecret123456"

        # An explicit empty value also keeps the stored secret.
        store.update({"providers": [{"id": "deepseek", "base_url": "https://api.deepseek.com/v1"}]})
        assert store.current.providers[0].api_key == "sk-realsecret123456"


def test_settings_routing_resolution():
    settings = Settings(
        providers=[{"id": "p1", "base_url": "https://example.com/v1", "api_key": "k", "enabled": True}],
        routing={"chat": {"provider": "p1", "model": "m1"}},
    )
    provider, route = settings.resolved_route("chat")
    assert provider is not None and route.model == "m1"
    provider, route = settings.resolved_route("parse")
    assert provider is None and not route.configured


def test_provider_presets_are_wellformed():
    assert PROVIDER_PRESETS
    for preset in PROVIDER_PRESETS:
        for key in ("id", "name", "base_url", "models", "vision"):
            assert key in preset, f"{preset.get('id')} 缺少 {key}"
        assert preset["base_url"].startswith("http")


def test_provider_config_strips_trailing_slash():
    settings = Settings(providers=[{"id": "x", "base_url": "https://a.com/v1/"}])
    assert settings.providers[0].base_url == "https://a.com/v1"


# ---------------------------------------------------------------------------
# runner
# ---------------------------------------------------------------------------

def _run_all() -> int:
    return run_all(globals())


if __name__ == "__main__":
    raise SystemExit(_run_all())
