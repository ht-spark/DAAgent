# 一键启动 DAAgent（PowerShell 版，与 start.bat 等价）
# 用法：
#   .\start.ps1           开发模式（后端 8000 + 前端 5173）
#   .\start.ps1 backend   仅启动后端
#   .\start.ps1 frontend  仅启动前端
#   .\start.ps1 prod      构建前端并仅启动后端（http://localhost:8000）
# 启动前会按 pyproject/uv.lock、package.json/package-lock 自动同步依赖。
param(
    [ValidateSet('dev', 'backend', 'frontend', 'prod')]
    [string]$Mode = 'dev'
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

function Assert-Command {
    param([string]$Name, [string]$Hint)
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        Write-Host "[错误] 未找到 $Name，请先安装：$Hint" -ForegroundColor Red
        exit 1
    }
}

Assert-Command uv 'https://docs.astral.sh/uv/'
if ($Mode -ne 'backend') {
    Assert-Command npm 'https://nodejs.org/'
}

# 依赖保持与清单同步：uv sync 在已最新时很快；前端仅在 package-lock 较新时装包
Write-Host '[准备] 同步 Python 依赖...' -ForegroundColor Cyan
uv sync

if ($Mode -ne 'backend') {
    $nodeModules = Join-Path $Root 'web\node_modules'
    $packageLock = Join-Path $Root 'web\package-lock.json'
    $packageJson = Join-Path $Root 'web\package.json'
    $needNpmInstall = $true
    if ((Test-Path $nodeModules) -and (Test-Path $packageLock) -and (Test-Path $packageJson)) {
        $modulesTime = (Get-Item $nodeModules).LastWriteTime
        $lockTime = (Get-Item $packageLock).LastWriteTime
        $pkgTime = (Get-Item $packageJson).LastWriteTime
        $needNpmInstall = ($lockTime -gt $modulesTime) -or ($pkgTime -gt $modulesTime)
    }
    if ($needNpmInstall) {
        Write-Host '[准备] 前端依赖有变更，正在安装...' -ForegroundColor Cyan
        Push-Location (Join-Path $Root 'web')
        npm install
        Pop-Location
    }
}

function Start-BackendDev {
    # 独立窗口继承项目根目录后启动后端，避免复杂引号嵌套
    Start-Process -FilePath 'cmd.exe' -WorkingDirectory $Root -ArgumentList '/k', 'uv run python -m server.dev'
}

function Start-FrontendDev {
    # 独立窗口在 web/ 下启动前端开发服务
    $web = Join-Path $Root 'web'
    Start-Process -FilePath 'cmd.exe' -WorkingDirectory $web -ArgumentList '/k', 'npm run dev'
}

switch ($Mode) {
    'backend' {
        Write-Host '[启动] 仅后端 http://localhost:8000' -ForegroundColor Green
        uv run python -m server.dev
    }
    'frontend' {
        Write-Host '[启动] 仅前端 http://localhost:5173' -ForegroundColor Green
        Set-Location (Join-Path $Root 'web')
        npm run dev
    }
    'prod' {
        Write-Host '[构建] 前端产物...' -ForegroundColor Cyan
        Push-Location (Join-Path $Root 'web')
        npm run build
        Pop-Location
        Write-Host '[启动] 生产模式 http://localhost:8000' -ForegroundColor Green
        Start-Process -FilePath 'cmd.exe' -WorkingDirectory $Root -ArgumentList '/k', 'uv run uvicorn server.main:app --host 0.0.0.0 --port 8000'
        Start-Sleep -Seconds 3
        Start-Process 'http://localhost:8000'
    }
    default {
        Write-Host '[启动] 开发模式：后端 8000 + 前端 5173' -ForegroundColor Green
        Start-BackendDev
        Start-FrontendDev
        Start-Sleep -Seconds 3
        Start-Process 'http://localhost:5173'
        Write-Host '已在两个独立窗口启动服务，关闭对应窗口即可停止。' -ForegroundColor Yellow
        Write-Host '前端：http://localhost:5173'
        Write-Host '后端：http://localhost:8000'
    }
}
