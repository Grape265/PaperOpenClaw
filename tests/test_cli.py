"""CLI argument-parsing tests.

The command line is a public interface (README, start scripts, automation), and
argument placement is exactly the kind of thing that silently breaks: `--data-dir`
originally lived only on the root parser, so the documented
`paperopenclaw serve --data-dir X` was rejected outright.

    python tests/test_cli.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import List

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _harness import run_all  # noqa: E402

from paperopenclaw.__main__ import _build_parser  # noqa: E402


def parse(argv: List[str]):
    return _build_parser().parse_args(argv)


def test_data_dir_accepted_after_subcommand():
    """The documented form must work: `serve --data-dir X`."""
    args = parse(["serve", "--data-dir", r"D:\poc-data", "--port", "9000"])
    assert args.command == "serve"
    assert args.data_dir == r"D:\poc-data"
    assert args.port == 9000


def test_data_dir_accepted_before_subcommand():
    args = parse(["--data-dir", "/var/lib/poc", "serve"])
    assert args.command == "serve"
    assert args.data_dir == "/var/lib/poc"


def test_data_dir_defaults_to_none():
    args = parse(["serve"])
    assert args.command == "serve"
    assert args.data_dir is None


def test_data_dir_works_for_every_subcommand():
    for argv in (
        ["doctor", "--data-dir", "/tmp/a"],
        ["info", "--data-dir", "/tmp/b"],
        ["ingest", "--data-dir", "/tmp/c", "paper.pdf"],
        ["okf-verify", "--data-dir", "/tmp/d", "bundle"],
    ):
        args = parse(argv)
        assert args.data_dir == argv[2], argv


def test_serve_flags_default_sensibly():
    args = parse(["serve"])
    assert args.host is None
    assert args.port is None
    assert args.no_browser is False
    assert args.reload is False


def test_ingest_flags():
    args = parse(["ingest", "a.pdf", "b.pdf", "--force", "--json"])
    assert args.paths == ["a.pdf", "b.pdf"]
    assert args.force is True
    assert args.json is True
    assert args.recursive is True

    args = parse(["ingest", "dir", "--no-recursive"])
    assert args.recursive is False


def test_no_command_defaults_to_serve_semantics():
    args = parse([])
    assert args.command is None
    assert args.data_dir is None


def test_unknown_command_is_rejected():
    import argparse

    try:
        parse(["nonsense"])
    except SystemExit:
        return
    raise AssertionError("未知子命令应使解析失败")


if __name__ == "__main__":
    raise SystemExit(run_all(globals(), verbose=True))
