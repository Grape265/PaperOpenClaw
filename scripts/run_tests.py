"""Run every test module in its own process and report a single exit code.

Each module is already a self-contained runner (``python tests/test_x.py``), so
spawning it in a fresh process gives us two things for free: an isolated file
descriptor budget per module (uvicorn/httpx/sqlite thread-local handles from one
module can no longer exhaust the descriptor limit for the next), and a hard
boundary against shared-global leaks. stdio is inherited, not piped, because the
sandbox forbids capturing another process's output through a pipe.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    paths = sorted((ROOT / "tests").glob("test_*.py"))
    if not paths:
        print("未找到任何测试文件")
        return 1

    failed_modules = []
    for path in paths:
        print(f"\n=== {path.name} ===", flush=True)
        result = subprocess.run([sys.executable, str(path)], cwd=str(ROOT))
        if result.returncode != 0:
            failed_modules.append(path.name)

    print("\n" + "=" * 60)
    if failed_modules:
        print("失败模块：")
        for name in failed_modules:
            print(f"  - {name}")
        return 1
    print("全部测试模块通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
