# 构建 Tauri 桌面 GUI（Windows）。
# 需要：Node.js、Rust 工具链、以及已安装的 Python 后端（pip install .）。
# 产物：desktop/src-tauri/target/release/paperopenclaw.exe

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot

Write-Host "==> 构建前端（桌面 API 基地址指向本地后端）" -ForegroundColor Cyan
$env:VITE_API_BASE = "http://127.0.0.1:8787/api/v1"
$env:npm_config_registry = "https://registry.npmmirror.com"
$env:npm_config_cache = Join-Path $repo ".cache\npm"
Push-Location (Join-Path $repo "web")
try { npm run build } finally { Pop-Location }

Write-Host "==> 安装 Tauri CLI" -ForegroundColor Cyan
Push-Location (Join-Path $repo "desktop")
try {
    if (-not (Test-Path "node_modules\@tauri-apps\cli")) { npm install }
} finally { Pop-Location }

Write-Host "==> Tauri build" -ForegroundColor Cyan
Push-Location (Join-Path $repo "desktop")
try { npx tauri build } finally { Pop-Location }

Write-Host "==> 完成：desktop\src-tauri\target\release\paperopenclaw.exe" -ForegroundColor Green
