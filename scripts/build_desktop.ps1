# 构建 Windows 桌面可执行文件（PyInstaller，单文件）。
# 在 Windows 上运行：powershell -ExecutionPolicy Bypass -File scripts/build_desktop.ps1
# 产物：dist/PaperOpenClaw.exe

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot

Write-Host "==> 构建前端（web/dist）" -ForegroundColor Cyan
$env:npm_config_registry = "https://registry.npmmirror.com"
$env:npm_config_cache = Join-Path $repo ".cache\npm"
Push-Location (Join-Path $repo "web")
try { npm run build } finally { Pop-Location }

Write-Host "==> 打包后端为可执行文件（含 torch，体积较大）" -ForegroundColor Cyan
Push-Location $repo
try {
    python -m PyInstaller --noconfirm --clean packaging/paperopenclaw.spec
} finally { Pop-Location }

Write-Host "==> 完成：$(Join-Path $repo 'dist\PaperOpenClaw.exe')" -ForegroundColor Green
