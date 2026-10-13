#!/usr/bin/env bash
# 构建 macOS / Linux 桌面可执行文件（PyInstaller，单文件）。
# 在对应操作系统上运行：bash scripts/build_desktop.sh
# 产物：dist/PaperOpenClaw

set -euo pipefail
repo="$(cd "$(dirname "$0")/.." && pwd)"

echo "==> 构建前端（web/dist）"
export npm_config_registry="https://registry.npmmirror.com"
export npm_config_cache="$repo/.cache/npm"
( cd "$repo/web" && npm run build )

echo "==> 打包后端为可执行文件（含 torch，体积较大）"
( cd "$repo" && python -m PyInstaller --noconfirm --clean packaging/paperopenclaw.spec )

echo "==> 完成：$repo/dist/PaperOpenClaw"
