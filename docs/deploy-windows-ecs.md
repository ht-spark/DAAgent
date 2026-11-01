# Windows Server 2022 部署手册（阿里云 ECS）

面向 DAAgent v1.1.0，目标是在一台 2 核 4 GB 的 Windows Server 2022 上跑通完整网页端，公网可通过 `http://<公网IP>:8000` 访问。

> 前置结论：2 GB 内存不足。项目需同时驻留 `torch`、`bge-small-zh-v1.5`（184 MB）与 pandas 峰值，按各组件常驻量核算峰值约 2.0–2.7 GB，会触发 OOM。必须先升配到 4 GB。

## 一、控制台准备（三件事，按顺序做）

### 1. 升配到 4 GB

ECS 控制台 → 实例 → 目标实例 → **更多 → 实例设置 → 变更配置** → 规格选 `ecs.e-c1m2.large`（2 核 4 GB）→ 按量付费可直接变更，系统会重启一次。

重启完成后确认「实例状态」为**运行中**，且内存显示 4 GB。

### 2. 安全组放行 8000

ECS 控制台 → 实例 → **网络与安全组 → 安全组 → 配置规则 → 入方向 → 手动添加**：

| 字段 | 值 |
|---|---|
| 授权策略 | 允许 |
| 优先级 | 1 |
| 协议类型 | 自定义 TCP |
| 端口范围 | 8000/8000 |
| 授权对象 | 0.0.0.0/0 |

> 阿里云 Windows 镜像默认只放行 3389，不手动加这一条，外网永远连不上。若只想自己访问，授权对象填你的本地公网 IP 更安全。

### 3. 设置登录密码

新创建的实例**没有默认密码**，必须自己设置一次，否则 Workbench 与 mstsc 都无法登录。

ECS 控制台 → 实例 → 顶部 **重置密码** → 实例属性选「管理员密码」→ 输入并确认新密码 → 确定 → **重启实例**（不重启不生效）。

密码需满足阿里云复杂度要求：8–30 位，且同时包含大写字母、小写字母、数字、特殊字符中的至少三类。示例结构：`DAAgent@2026ecs`。

> 忘记密码时从同一入口重新重置即可，会直接覆盖旧密码，不影响实例上已有的数据。

## 二、远程登录

推荐用控制台的**远程连接**（Workbench）：不占用本地端口，也不需要把 3389 暴露到公网。

ECS 控制台 → 实例 → 右上角 **远程连接** → 选择「Workbench 远程连接」→ 输入 Administrator 密码登录。

登录后默认可能是 cmd，输入 `powershell` 回车切换到 PowerShell。

备选方案 mstsc：本地按 `Win + R` 输入 `mstsc`，计算机填 `39.106.138.41`，用户名 `Administrator`。

> 若长期用 mstsc，建议把安全组 3389 的授权对象收窄到自己的固定 IP。

## 三、拉取代码

登录服务器后，**以管理员身份**打开 PowerShell（开始菜单搜索 PowerShell → 右键 → 以管理员身份运行）。

```powershell
# 先确认 git 可用，Server 2022 默认未安装
git --version
```

若提示找不到命令，先装 Git：

```powershell
Invoke-WebRequest -Uri https://registry.npmmirror.com/-/binary/git-for-windows/v2.47.1.windows.1/Git-2.47.1-64-bit.exe -OutFile $env:TEMP\git.exe -UseBasicParsing
Start-Process $env:TEMP\git.exe -ArgumentList '/VERYSILENT' -Wait
# 装完后重开一个 PowerShell 窗口，再执行 git --version
```

拉取代码（**用 Gitee，不要用 GitHub**）：

```powershell
cd C:\
git clone https://gitee.com/harmony-tao/MMAgent.git DAAgent
cd DAAgent
```

> 为什么不用 GitHub：该仓库的 GitHub 远端长期未同步，落后数十个提交，clone 下来是旧版本；Gitee 是当前最新代码，且国内服务器拉取速度快一个量级。

> 若 `git clone` 提示输入账号密码，说明仓库为私有，填入 Gitee 用户名与私人令牌（个人设置 → 私人令牌）即可。

## 四、环境准备

```powershell
cd C:\DAAgent
powershell -ExecutionPolicy Bypass -File .\deploy\setup-windows.ps1
```

脚本会依次完成：Python 3.12 → uv → Node.js 22 → npm 淘宝镜像 → 防火墙放行 8000。已安装的组件自动跳过，可重复执行。

预计耗时 3–8 分钟，取决于镜像速度。

> 装完后如果 `python` 或 `node` 提示找不到命令，关掉当前窗口重开一个即可（PATH 需要重新加载）。

## 五、安装依赖并构建前端

### 优先用内网镜像（决定耗时长短的关键）

阿里云 ECS 内网提供独立镜像站 `mirrors.cloud.aliyuncs.com`，走内网带宽，不计公网流量费。实测公网镜像 `mirrors.aliyun.com` 只有约 0.6 MB/s，2 GB 依赖要跑近一小时，而且按流量计费的实例还要额外付约 1.6 元；内网镜像通常可达数十 MB/s。

```powershell
cd C:\DAAgent
uv sync --default-index http://mirrors.cloud.aliyuncs.com/pypi/simple/ --allow-insecure-host mirrors.cloud.aliyuncs.com
```

前端同理：

```powershell
cd C:\DAAgent\web
npm config set registry http://mirrors.cloud.aliyuncs.com/npm/
npm install
npm run build
```

> 该域名只解析在内网（`100.100.x.x`），本地电脑访问不通，属正常现象。

### 内网镜像不可用时的备选

先用公网镜像，并放宽超时、降低并发（默认并发 50 会占满带宽反而加剧超时）：

```powershell
cd C:\DAAgent
$env:UV_HTTP_TIMEOUT = "600"
$env:UV_CONCURRENT_DOWNLOADS = "8"
uv sync
```

> 这两个环境变量只对当前 PowerShell 会话有效，重开窗口需重设。

若时间紧张且暂时不需要知识库，可跳过全部重型依赖。项目源码没有顶层 `import torch`，`langchain_huggingface` 在 `KnowledgeBase/embedding.py` 内部延迟导入，因此跳过不影响服务启动，只是知识库的向量检索不可用：

```powershell
uv sync --no-install-package torch --no-install-package sentence-transformers --no-install-package langchain-huggingface --no-install-package modelscope --no-install-package llama-index
```

`uv sync` 可重入，已下载的包命中本地缓存，中断后重跑不会从头开始。若单个包反复超时，可换清华源：

```powershell
uv sync --default-index https://pypi.tuna.tsinghua.edu.cn/simple
```

构建前端：

```powershell
cd C:\DAAgent\web
npm install
npm run build
cd C:\DAAgent
```

`npm run build` 约 30–90 秒，产物写入 `web\dist`（约 7.4 MB），后端会自动挂载该目录。

> Node 版本必须 ≥ 22.12，这是 `mermaid@12` 的引擎要求（`web/package.json` 依赖 `mermaid ^12.1.0`），不是 Vite 的 ≥18。若 `npm install` 刷出大量 `EBADENGINE` 警告，说明 Node 过低，升级后重新安装依赖。

## 六、启动与验证

```powershell
cd C:\DAAgent
uv run uvicorn server.main:app --host 0.0.0.0 --port 8000
```

`--host 0.0.0.0` 不能省略，否则只监听 `127.0.0.1`，外网无法访问。

首次启动要导入 torch、sentence-transformers 等重型依赖，约需 30–60 秒才会打印 `Application startup complete`，期间无输出属正常，不要重复启动。

看到 `Application startup complete` 后，用浏览器打开：

```
http://39.106.138.41:8000
```

先访问 `http://39.106.138.41:8000/healthz`，返回正常即代表链路打通。

### 首次使用必须配置模型

`server/api_settings.json` 已被 `.gitignore` 排除，`git clone` 后不存在，服务器上没有任何可用密钥。页面打开后需在**设置**里填入模型 API Key 与 Base URL 并保存。

注意：该配置会**同时写入浏览器 localStorage 与服务端 `server/api_settings.json`**，后者是明文密钥文件。它已在忽略规则中，切勿提交到仓库；如果是多人共用的服务器，应各自使用独立密钥并在用完后清理该文件。

## 七、常驻运行（验证通过后再做）

前台运行时关闭 RDP 窗口进程会被终止。要长期在线，先用**管理员 PowerShell** 做两件事：

```powershell
# SYSTEM 账户的家目录在 System32 下，matplotlib 默认缓存路径不可写，会直接报错
New-Item -ItemType Directory -Force -Path C:\DAAgent\.cache\matplotlib | Out-Null
[Environment]::SetEnvironmentVariable('MPLCONFIGDIR', 'C:\DAAgent\.cache\matplotlib', 'Machine')

# 注册开机自启（用 cmd 包装以便把输出重定向到日志文件）
$action = New-ScheduledTaskAction -Execute "cmd.exe" `
    -Argument '/c "C:\DAAgent\.venv\Scripts\python.exe -m uvicorn server.main:app --host 0.0.0.0 --port 8000 >> C:\DAAgent\server\uvicorn.out 2>&1"' `
    -WorkingDirectory "C:\DAAgent"
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable

Register-ScheduledTask -TaskName "DAAgent" -Action $action -Trigger $trigger `
    -Settings $settings -User "SYSTEM" -RunLevel Highest -Description "DAAgent 数据分析智能体"
```

启动与停止：

```powershell
Start-ScheduledTask -TaskName "DAAgent"
Get-ScheduledTask   -TaskName "DAAgent" | Select-Object State
Get-Content C:\DAAgent\server\uvicorn.out -Tail 50
```

停止服务需要连带结束 Python 子进程（任务计划只终止 cmd 主进程）：

```powershell
Stop-ScheduledTask -TaskName "DAAgent"
Get-Process python -ErrorAction SilentlyContinue | Stop-Process -Force
```

## 八、故障排查

| 现象 | 原因 | 处理 |
|---|---|---|
| 浏览器打不开，但 healthz 能通 | 前端未构建 | 执行 `web` 目录下 `npm run build` |
| 完全连不上 | 安全组未放行 | 检查入方向 8000 规则 |
| 能连但页面空白 | `dist` 缺失或路径错 | 确认 `C:\DAAgent\web\dist\index.html` 存在 |
| 启动即退出，日志 OOM | 内存不足 | 确认已升配 4 GB；关闭无关进程 |
| `uv sync` 卡在 torch | 镜像未生效 | 检查 `pyproject.toml` 的 `[tool.uv.index]` |
| 中文乱码 | 控制台代码页 | PowerShell 执行 `chcp 65001` |
| 上传文档报 `DLL load failed while importing onnxruntime_pybind11_state` | 缺 VC++ 运行库（`onnxruntime` 是 C++ 扩展，纯净 Server 镜像不带 `vcruntime140.dll`） | 安装 https://aka.ms/vs/17/release/vc_redist.x64.exe 后重启服务；`deploy\setup-windows.ps1` 已包含该步骤 |

## 九、费用提醒

- 实例为**按使用流量计费**，100 Mbps 峰值。流量约 0.8 元/GB，跑任务时上传数据集、下载报告会产生流量。
- 建议设置「流量计费封顶」，避免异常流量产生高额账单。
- 不用时可在控制台**停止实例**，但按量付费默认停机仍会收取计算资源（vCPU 与内存）费用。需在实例上开启「节省停机模式」，停机后才不计计算费，云盘费用照收。

## 十、后续可选项

1. **域名 + HTTPS**：备案后可绑域名，用 Nginx 或 Caddy 反代到 8000，再把 Cloudflare 作为 DNS 与 CDN。
2. **知识库模型**：`KnowledgeBase/embedding_model/bge-small-zh-v1.5` 未纳入版本库，`git clone` 后不存在，此时知识库的向量检索会报错，但不影响建模主流程。需要启用时，在 ModelScope 搜索 `bge-small-zh-v1.5` 下载完整目录（含 `config.json`、`model.safetensors`、`tokenizer.json`），放到 `C:\DAAgent\KnowledgeBase\embedding_model\bge-small-zh-v1.5\` 下，目录层级必须完全一致。
3. **数据目录持久化**：`artifacts/`、`KnowledgeBase/qdrant_db/`、`server/*.db` 均在 `.gitignore` 中，属于服务器本地状态，升级代码时不要删除。
