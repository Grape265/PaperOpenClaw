#!/usr/bin/env bash
# PaperOpenClaw 启动脚本（macOS / Linux）
#
# 用法：
#   ./scripts/start.sh                 # 构建前端（如需要）并启动服务
#   ./scripts/start.sh --skip-build
#   ./scripts/start.sh --port 9000 --data-dir /var/lib/paperopenclaw
#
# 与 Windows 版本行为一致：确认 Python、必要时构建前端、启动服务。

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND="$ROOT/backend"
WEB="$ROOT/web"

DATA_DIR=""
HOST_ARG=""
PORT_ARG=""
SKIP_BUILD=0
NO_BROWSER=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --data-dir) DATA_DIR="$2"; shift 2 ;;
    --host) HOST_ARG="$2"; shift 2 ;;
    --port) PORT_ARG="$2"; shift 2 ;;
    --skip-build) SKIP_BUILD=1; shift ;;
    --no-browser) NO_BROWSER=1; shift ;;
    -h|--help)
      grep '^#' "$0" | sed 's/^# \{0,1\}//' | head -12
      exit 0 ;;
    *) echo "未知参数：$1" >&2; exit 2 ;;
  esac
done

step() { printf '\033[36m[PaperOpenClaw] %s\033[0m\n' "$1"; }
warn() { printf '\033[33m[PaperOpenClaw] %s\033[0m\n' "$1"; }

# ---- 1. Python -----------------------------------------------------------
PYTHON=""
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1; then PYTHON="$candidate"; break; fi
done
if [[ -z "$PYTHON" ]]; then
  echo "未找到 Python。请安装 Python 3.9 或更高版本。" >&2
  exit 1
fi

VERSION="$("$PYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
step "使用 Python $VERSION ($PYTHON)"
"$PYTHON" - <<'PY' || { echo "需要 Python 3.9 或更高版本" >&2; exit 1; }
import sys
raise SystemExit(0 if sys.version_info[:2] >= (3, 9) else 1)
PY

# ---- 2. 依赖 -------------------------------------------------------------
if ! "$PYTHON" -c 'import fastapi, uvicorn, fitz, chromadb, jieba, networkx' >/dev/null 2>&1; then
  warn "运行依赖不完整，正在从清华镜像安装……"
  "$PYTHON" -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r "$BACKEND/requirements.txt"
fi

# ---- 3. 前端 -------------------------------------------------------------
if [[ "$SKIP_BUILD" -eq 0 && ! -f "$WEB/dist/index.html" ]]; then
  if command -v npm >/dev/null 2>&1; then
    step "首次运行：安装前端依赖并构建"
    export npm_config_registry="${npm_config_registry:-https://registry.npmmirror.com}"
    export npm_config_cache="${npm_config_cache:-$ROOT/.cache/npm}"
    mkdir -p "$npm_config_cache"
    ( cd "$WEB"
      [[ -d node_modules ]] || npm install --no-audit --no-fund
      npm run build )
  else
    warn "未找到 npm，跳过前端构建。服务仍会启动，但 / 只返回接口提示。"
  fi
fi

# ---- 4. 启动 -------------------------------------------------------------
ARGS=(-m paperopenclaw serve)
[[ -n "$DATA_DIR" ]] && ARGS+=(--data-dir "$DATA_DIR")
[[ -n "$HOST_ARG" ]] && ARGS+=(--host "$HOST_ARG")
[[ -n "$PORT_ARG" ]] && ARGS+=(--port "$PORT_ARG")
[[ "$NO_BROWSER" -eq 1 ]] && ARGS+=(--no-browser)

step "启动：$PYTHON ${ARGS[*]}"
cd "$BACKEND"
exec "$PYTHON" "${ARGS[@]}"
