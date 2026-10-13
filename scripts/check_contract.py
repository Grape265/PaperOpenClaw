"""Verify that every API path the frontend calls exists in the backend.

Requirement FR-12.6 says the frontend must align strictly with the backend. Unit
tests on either side cannot catch a typo'd URL that only fails at runtime, so this
script diffs the two catalogues:

    backend  -> FastAPI's generated OpenAPI schema (the machine contract)
    frontend -> every path literal in web/src/api/*.ts (the call layer)

A frontend path with no backend route is an error and fails the run. A backend
route the frontend does not use is reported as informational only, since the API
is also driven by the CLI and by external tools.

    python scripts/check_contract.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Dict, Set, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

API_DIR = ROOT / "web" / "src" / "api"

# `http.get<T>('/papers')`, `http.post<X>(`/jobs/${id}/cancel`)`, `streamSse('/agent/qa/stream'`
# The generic argument is matched non-greedily up to the first `>(` so that
# nested generics like `Paged<PaperSummary>` are handled; a naive `<[^>]*>`
# stops at the inner `>` and silently misses the call.
CALL_RE = re.compile(
    r"http\.(?:get|post|put|patch|del|upload|uploadOne)\s*(?:<[\s\S]*?>)?\s*\(\s*[`'\"]([^`'\"]+)[`'\"]"
)
STREAM_RE = re.compile(r"streamSse\s*\(\s*[`'\"]([^`'\"]+)[`'\"]")
# URL builders such as `${API_BASE}/papers/${hash}/pdf`
BUILDER_RE = re.compile(r"\$\{API_BASE\}(/[^`'\"]*)")


def normalise(path: str) -> str:
    """Collapse template/route parameters so the two sides are comparable."""
    text = path.split("?", 1)[0]
    text = re.sub(r"\$\{[^}]*\}", "{}", text)     # JS template placeholder
    text = re.sub(r"\{[^}]*\}", "{}", text)        # OpenAPI path parameter
    text = re.sub(r"\{[^}]*:path\}", "{}", text)   # FastAPI path converter
    text = text.rstrip("/")
    return text or "/"


def frontend_paths() -> Tuple[Set[str], Dict[str, str]]:
    found: Set[str] = set()
    origin: Dict[str, str] = {}
    if not API_DIR.is_dir():
        return found, origin
    for path in sorted(API_DIR.glob("*.ts")):
        if path.name in ("client.ts", "types.ts"):
            continue
        text = path.read_text(encoding="utf-8")
        for pattern in (CALL_RE, STREAM_RE, BUILDER_RE):
            for match in pattern.finditer(text):
                raw = match.group(1)
                if not raw.startswith("/") or raw.startswith("/api/"):
                    # Skip already-prefixed or non-API strings.
                    if raw.startswith("/api/v1"):
                        raw = raw[len("/api/v1"):]
                    else:
                        continue
                key = normalise(raw)
                found.add(key)
                origin.setdefault(key, f"{path.name}: {raw}")
    return found, origin


def backend_paths() -> Set[str]:
    from paperopenclaw.gateway.app import create_app

    app = create_app(enable_docs=True)
    paths: Set[str] = set()
    for raw in app.openapi().get("paths", {}):
        if raw.startswith("/api/v1"):
            paths.add(normalise(raw[len("/api/v1"):]))
    return paths


def main() -> int:
    backend = backend_paths()
    frontend, origin = frontend_paths()

    missing = sorted(path for path in frontend if path not in backend)
    unused = sorted(path for path in backend if path not in frontend)

    print(f"后端路由 {len(backend)} 条，前端调用 {len(frontend)} 条\n")

    if missing:
        print("前端调用了后端不存在的接口：")
        for path in missing:
            print(f"  - {path}    （{origin.get(path, '未知来源')}）")
    else:
        print("前端调用的全部接口均存在于后端。")

    if unused:
        print("\n后端提供但前端未使用的接口（仅提示）：")
        for path in unused:
            print(f"  - {path}")

    print()
    if missing:
        print("契约校验失败")
        return 1
    print("契约校验通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
