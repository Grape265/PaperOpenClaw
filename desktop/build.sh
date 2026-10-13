#!/usr/bin/env bash
# 构建 Tauri 桌面 GUI（macOS / Linux）。
# 需要：Node.js、Rust 工具链、以及已安装的 Python 后端（pip install .）。
# 产物：desktop/src-tauri/target/release/paperopenclaw

set -euo pipefail
repo="$(cd "$(dirname "$0")/.." && pwd)"

echo "==> 构建前端（桌面 API 基地址指向本地后端）"
export VITE_API_BASE="http://127.0.0.1:8787/api/v1"
export npm_config_registry="https://registry.npmmirror.com"
export npm_config_cache="$repo/.cache/npm"
( cd "$repo/web" && npm run build )

echo "==> 安装 Tauri CLI"
( cd "$repo/desktop" && [ -d node_modules/@tauri-apps/cli ] || npm install )

echo "==> Tauri build"
( cd "$repo/desktop" && npx tauri build )

echo "==> 完成：desktop/src-tauri/target/release/paperopenclaw"
