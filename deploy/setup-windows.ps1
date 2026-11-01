<#
.SYNOPSIS
    DAAgent 云服务器环境准备脚本（Windows Server 2022）。

.DESCRIPTION
    按顺序完成：Python 3.12 → Node.js 20 → pip/npm 国内镜像 → 8000 端口防火墙规则。
    脚本可重复执行，已安装的组件会自动跳过，不会重复下载。
    全部依赖安装在用户级目录，不需要额外配置虚拟环境路径。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\deploy\setup-windows.ps1

.NOTES
    必须以管理员身份运行，否则无法静默安装与创建防火墙规则。
#>

$ErrorActionPreference = 'Stop'
# Server 2022 默认仍可能协商旧版 TLS，显式提升到 1.2，否则镜像站下载会失败
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

# ── 版本与镜像（超参数）────────────────────────────────────────────────
# PYTHON_VERSION：项目要求 >=3.11，选 3.12 是因为 3.13 上 torch / llama-index 的预编译轮子尚不完整
$PYTHON_VERSION = '3.12.8'
# NODE_VERSION：web/package.json 依赖 mermaid ^12.1.0，其引擎声明为 node >=22.12，
# 低于该版本 npm install 会大量报 EBADENGINE 且构建存在失败风险；选 22.20.0（22.x LTS 最新补丁版）
$NODE_VERSION = '22.20.0'
# PORT：后端监听端口，与本地 start.ps1 prod 保持一致，避免前端相对路径请求失效
$PORT = 8000
# 多镜像候选：国内服务器优先华为云与淘宝镜像，逐个回退，避免单点不可达
$PYTHON_URLS = @(
    "https://mirrors.huaweicloud.com/python/$PYTHON_VERSION/python-$PYTHON_VERSION-amd64.exe",
    "https://registry.npmmirror.com/-/binary/python/$PYTHON_VERSION/python-$PYTHON_VERSION-amd64.exe"
)
$NODE_URLS = @(
    "https://registry.npmmirror.com/-/binary/node/v$NODE_VERSION/node-v$NODE_VERSION-x64.msi",
    "https://mirrors.huaweicloud.com/nodejs/v$NODE_VERSION/node-v$NODE_VERSION-x64.msi"
)

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Write-Ok {
    param([string]$Message)
    Write-Host "    [完成] $Message" -ForegroundColor Green
}

function Write-Warn {
    param([string]$Message)
    Write-Host "    [注意] $Message" -ForegroundColor Yellow
}

# 安装程序会修改机器级 PATH，但当前进程读的是启动时的快照，必须手动刷新
function Update-SessionPath {
    $machinePath = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = "$machinePath;$userPath"
}

function Save-FileFromMirrors {
    param(
        [string[]]$Urls,
        [string]$Destination
    )
    foreach ($url in $Urls) {
        try {
            Write-Host "    下载 $url"
            Invoke-WebRequest -Uri $url -OutFile $Destination -UseBasicParsing -TimeoutSec 600
            return $true
        } catch {
            Write-Warn "该镜像不可用，尝试下一个"
        }
    }
    return $false
}

# ── 0. 管理员权限校验 ─────────────────────────────────────────────────
$currentPrincipal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $currentPrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "[错误] 请右键 PowerShell 选择「以管理员身份运行」后重试。" -ForegroundColor Red
    exit 1
}

Write-Host "DAAgent 云服务器环境准备" -ForegroundColor White
Write-Host "目标：Python $PYTHON_VERSION / Node $NODE_VERSION / 端口 $PORT"

# ── 1. Python ────────────────────────────────────────────────────────
Write-Step "检查 Python"

$pythonOk = $false
try {
    $pythonVersion = (& python --version 2>&1) -replace 'Python ', ''
    $majorMinor = [version]($pythonVersion.Split('.')[0] + '.' + $pythonVersion.Split('.')[1])
    if ($majorMinor -ge [version]'3.11') {
        Write-Ok "已安装 Python $pythonVersion"
        $pythonOk = $true
    } else {
        Write-Warn "检测到 Python $pythonVersion，低于项目要求的 3.11"
    }
} catch {
    Write-Warn "未检测到 Python"
}

if (-not $pythonOk) {
    $installer = Join-Path $env:TEMP "python-$PYTHON_VERSION-amd64.exe"
    if (-not (Test-Path $installer)) {
        if (-not (Save-FileFromMirrors -Urls $PYTHON_URLS -Destination $installer)) {
            Write-Host "[错误] Python 安装包下载失败，请手动下载后重试。" -ForegroundColor Red
            exit 1
        }
    }
    Write-Host "    静默安装 Python（约 1-2 分钟）"
    # InstallAllUsers=1：装到 Program Files，避免每个用户重复安装
    # PrependPath=1：写入机器级 PATH，后续命令可直接调用 python
    # Include_pip=1：附带 pip，用于安装 uv
    Start-Process -FilePath $installer -ArgumentList @(
        '/quiet', 'InstallAllUsers=1', 'PrependPath=1', 'Include_test=0', 'Include_pip=1'
    ) -Wait

    # Python 官方安装器会派生子进程，Start-Process -Wait 可能提前返回，
    # 因此轮询最长 5 分钟，直到 PATH 中真正出现 python 命令
    $deadline = (Get-Date).AddMinutes(5)
    while ((Get-Date) -lt $deadline) {
        Update-SessionPath
        if (Get-Command python -ErrorAction SilentlyContinue) { break }
        Start-Sleep -Seconds 5
    }

    if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
        Write-Host "[错误] Python 安装未完成或 PATH 未生效，请重开一个 PowerShell 窗口后重新运行本脚本。" -ForegroundColor Red
        exit 1
    }
    Write-Ok "Python $(& python --version)"
}

# ── 2. uv ────────────────────────────────────────────────────────────
Write-Step "检查 uv（项目依赖管理器）"

if (Get-Command uv -ErrorAction SilentlyContinue) {
    Write-Ok "已安装 uv $(& uv --version)"
} else {
    # pip 默认访问 pypi.org，国内服务器常只有几十 KB/s 甚至长时间无响应。
    # 必须显式指定镜像：pyproject.toml 的 [tool.uv.index] 只对 uv 生效，不影响 pip。
    $pipIndex = 'https://mirrors.aliyun.com/pypi/simple/'
    Write-Host "    安装 uv（镜像：$pipIndex）"
    # 不加 --quiet，保留进度条，便于区分「正在下载」与「真的卡死」
    & python -m pip install --upgrade pip -i $pipIndex
    & python -m pip install uv -i $pipIndex
    Update-SessionPath
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        Write-Warn "uv 未进入 PATH，后续请使用 python -m uv 代替 uv"
    } else {
        Write-Ok "uv $(& uv --version)"
    }
}

# ── 3. Node.js ───────────────────────────────────────────────────────
Write-Step "检查 Node.js"

$nodeOk = $false
if (Get-Command node -ErrorAction SilentlyContinue) {
    $nodeVersion = (& node --version) -replace 'v', ''
    # 引擎要求来自 mermaid 12.1.0（>=22.12），不是 Vite 的 >=18，
    # 用完整版本号比较而非只看主版本，否则 22.10 会被误判为可用
    if ([version]$nodeVersion -ge [version]'22.12.0') {
        Write-Ok "已安装 Node v$nodeVersion"
        $nodeOk = $true
    } else {
        Write-Warn "检测到 Node v$nodeVersion，低于 mermaid 12 要求的 22.12"
    }
} else {
    Write-Warn "未检测到 Node.js"
}

if (-not $nodeOk) {
    $msi = Join-Path $env:TEMP "node-v$NODE_VERSION-x64.msi"
    if (-not (Test-Path $msi)) {
        if (-not (Save-FileFromMirrors -Urls $NODE_URLS -Destination $msi)) {
            Write-Host "[错误] Node.js 安装包下载失败，请手动下载后重试。" -ForegroundColor Red
            exit 1
        }
    }
    Write-Host "    静默安装 Node.js"
    # /qn：完全静默，不弹安装向导
    Start-Process -FilePath 'msiexec.exe' -ArgumentList @('/i', "`"$msi`"", '/qn') -Wait
    Update-SessionPath
    if (Get-Command node -ErrorAction SilentlyContinue) {
        Write-Ok "Node v$(& node --version)"
    } else {
        Write-Warn "Node 安装后命令暂不可用，请重开 PowerShell 窗口"
    }
}

# ── 4. npm 镜像 ──────────────────────────────────────────────────────
Write-Step "配置 npm 镜像"

if (Get-Command npm -ErrorAction SilentlyContinue) {
    # 淘宝镜像，国内服务器 npm install 从数分钟降到数十秒
    & npm config set registry https://registry.npmmirror.com
    Write-Ok "npm registry = $(& npm config get registry)"
} else {
    Write-Warn "npm 不可用，跳过镜像配置"
}

# ── 5. 防火墙 ────────────────────────────────────────────────────────
Write-Step "配置防火墙"

$ruleName = "DAAgent HTTP $PORT"
if (Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue) {
    Write-Ok "防火墙规则已存在：$ruleName"
} else {
    # 仅放行 TCP 入站到指定端口，不放行整机，降低暴露面
    New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Protocol TCP -LocalPort $PORT -Action Allow | Out-Null
    Write-Ok "已放行 TCP $PORT 入站"
}

# ── 6. 汇总 ──────────────────────────────────────────────────────────
Write-Step "环境准备完成"

Write-Host "    Python : $(try { & python --version } catch { '未就绪' })"
Write-Host "    Node   : $(try { & node --version } catch { '未就绪' })"
Write-Host "    端口   : $PORT 已放行（防火墙）"
Write-Host ""
Write-Host "    下一步：" -ForegroundColor White
Write-Host "      1. 在阿里云控制台「安全组」中放行 TCP $PORT，否则外网仍无法访问"
Write-Host "      2. 进入项目目录执行：uv sync"
Write-Host "      3. 构建前端：cd web; npm install; npm run build; cd .."
Write-Host "      4. 启动服务：uv run uvicorn server.main:app --host 0.0.0.0 --port $PORT"
