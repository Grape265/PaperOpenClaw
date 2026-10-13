<#
PaperOpenClaw 启动脚本（Windows / PowerShell）

用法：
    .\scripts\start.ps1                 # 构建前端（如需要）并启动服务
    .\scripts\start.ps1 -SkipBuild      # 跳过前端构建
    .\scripts\start.ps1 -Port 9000      # 指定端口
    .\scripts\start.ps1 -DataDir D:\poc-data

脚本只做三件事：确认 Python 可用、必要时构建前端、启动服务。
所有路径都相对于仓库根目录解析，不依赖调用时的当前目录。
#>

[CmdletBinding()]
param(
    [string]$DataDir = "",
    [string]$Host = "",
    [int]$Port = 0,
    [switch]$SkipBuild,
    [switch]$NoBrowser
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$backend = Join-Path $root 'backend'
$web = Join-Path $root 'web'

function Write-Step($message) { Write-Host "[PaperOpenClaw] $message" -ForegroundColor Cyan }
function Write-Warn($message) { Write-Host "[PaperOpenClaw] $message" -ForegroundColor Yellow }

# ---- 1. Python -----------------------------------------------------------
$python = $null
foreach ($candidate in @('python', 'python3', 'py')) {
    $command = Get-Command $candidate -ErrorAction SilentlyContinue
    if ($command) { $python = $command.Source; break }
}
if (-not $python) {
    throw "未找到 Python。请安装 Python 3.9 或更高版本并加入 PATH。"
}

$version = & $python -c "import sys; print('%d.%d' % sys.version_info[:2])"
Write-Step "使用 Python $version ($python)"
if ([version]$version -lt [version]'3.9') {
    throw "需要 Python 3.9 或更高版本，当前为 $version"
}

# ---- 2. 依赖 -------------------------------------------------------------
$probe = & $python -c "import fastapi, uvicorn, fitz, chromadb, jieba, networkx; print('ok')" 2>&1
if ($probe -notmatch 'ok') {
    Write-Warn "运行依赖不完整，正在从清华镜像安装……"
    & $python -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r (Join-Path $backend 'requirements.txt')
    if ($LASTEXITCODE -ne 0) { throw "依赖安装失败" }
}

# ---- 3. 前端 -------------------------------------------------------------
$dist = Join-Path $web 'dist\index.html'
if (-not $SkipBuild -and -not (Test-Path $dist)) {
    $npm = Get-Command npm -ErrorAction SilentlyContinue
    if (-not $npm) {
        Write-Warn "未找到 npm，跳过前端构建。服务仍会启动，但 / 只返回接口提示。"
    } else {
        Write-Step "首次运行：安装前端依赖并构建"
        $env:npm_config_registry = 'https://registry.npmmirror.com'
        if (-not $env:npm_config_cache) {
            $env:npm_config_cache = Join-Path $root '.cache\npm'
        }
        Push-Location $web
        try {
            if (-not (Test-Path (Join-Path $web 'node_modules'))) {
                & npm install --no-audit --no-fund
                if ($LASTEXITCODE -ne 0) { throw "npm install 失败" }
            }
            & npm run build
            if ($LASTEXITCODE -ne 0) { throw "前端构建失败" }
        } finally {
            Pop-Location
        }
    }
}

# ---- 4. 启动 -------------------------------------------------------------
$arguments = @('-m', 'paperopenclaw', 'serve')
if ($DataDir) { $arguments += @('--data-dir', $DataDir) }
if ($Host) { $arguments += @('--host', $Host) }
if ($Port -gt 0) { $arguments += @('--port', "$Port") }
if ($NoBrowser) { $arguments += '--no-browser' }

Write-Step "启动：$python $($arguments -join ' ')"
Push-Location $backend
try {
    & $python @arguments
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
