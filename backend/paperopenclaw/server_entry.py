"""Server-only entry point for the desktop sidecar backend.

Runs ``paperopenclaw serve`` without opening a browser or window. This is the
executable that the Tauri GUI spawns as a sidecar.

使用绝对导入：PyInstaller 会把本文件当作顶层脚本执行，相对导入会失败。
"""

from __future__ import annotations


def main() -> int:
    from paperopenclaw.__main__ import main as cli_main

    return cli_main(["serve", "--no-browser"])


if __name__ == "__main__":
    raise SystemExit(main())
