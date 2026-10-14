# MXC 沙箱运维手册

> 对应《MXC 集成设计方案 V2.2》。适用于 Phase 1（`execute_python` 接入）；
> 终端工具接入（Phase 2）后需补充审批模型相关章节。

## 1. 平台准入

| 宿主 | 状态 | 说明 |
|---|---|---|
| Windows 11 24H2+（`26100.9278+`） | 官方支持 | `--probe` 应报告 `processcontainer` 系 tier |
| Windows 10（当前开发机） | **实验性** | 实测降级 `tier=appcontainer-dacl`，功能可用但不在官方支持矩阵；生产候选须迁移受支持宿主 |
| Linux | 待验收 | 使用 `bubblewrap` 后端，需独立验收后再启用 |

准入检查（每次部署后在目标机上执行）：

```bash
python scripts/fetch_mxc.py --verify          # 资产完整性
vendor/mxc/bin/x64/wxc-exec.exe --probe       # 实际可用 tier
```

## 2. 资产部署

- 二进制不入库；`vendor/mxc/VERSION.md` 记录版本与 SHA-256。
- 新机器获取：`python scripts/fetch_mxc.py`（从 npm registry 拉取锁定版本并解压到 `vendor/mxc/bin/<arch>/`）。
- CI 建议：部署后运行 `--verify` 并与 `VERSION.md` 中的哈希比对，禁止运行时静默获取不明版本。
- 执行器路径由 `DAAGENT_MXC_EXECUTABLE` 指定（默认 `vendor/mxc/bin/<arch>/wxc-exec.exe`），Agent 与用户不可覆盖。

## 3. 服务端配置

| 环境变量 | 取值 | 默认 | 说明 |
|---|---|---|---|
| `DAAGENT_SANDBOX_BACKEND` | `local` / `mxc` / `auto` | `local` | 运行级后端标识；Phase 1 仅影响 `execute_python` |
| `DAAGENT_SANDBOX_PROFILE` | `development` / `production` | `development` | `production` 时要求 `supported_mxc_required`，任何失败不降级 |
| `DAAGENT_SANDBOX_ALLOW_UNSAFE_FALLBACK` | `true` / `false` | `false` | 仅 development + `auto` 模式下允许探测失败降级到 local |
| `DAAGENT_MXC_REQUIRE_SUPPORTED_HOST` | `true` / `false` | production 为 `true` | 拒绝官方支持矩阵外的 tier（如 `appcontainer-dacl`） |
| `DAAGENT_MXC_EXECUTABLE` | 绝对路径 | 按架构推导 | 执行器位置 |

配置非法值会在读取时直接报错（fail-fast），不会静默回退。

## 4. 启用与回退

**启用 MXC（开发机实验）**：

```
DAAGENT_SANDBOX_BACKEND=mxc
DAAGENT_SANDBOX_PROFILE=development
```

**回退**：改回 `DAAGENT_SANDBOX_BACKEND=local`（或删除该变量）并重启后端；
回退后的一切执行均为迁移前行为（应用层 runner），不具备 MXC 的 OS 级隔离保证，日志与产品展示须如实标注。

**验证接入生效**：执行一次分析任务，检查工具返回值 `runtime.backend == "mxc"` 且 `runtime.isolation_tier` 非空。

## 5. 主机准备（可选，管理员）

`wxc-exec.exe --probe` 在降级 tier 下可能提示两个可选修复（需要管理员权限，
属于对宿主系统状态的修改，须经运维审批后执行，并记录前后安全描述符）：

```
wxc-host-prep.exe prepare-system-drive   # 授予系统盘根目录最小元数据 ACE
wxc-host-prep.exe prepare-null-device    # 修复 NUL 设备安全描述符
```

不执行亦可运行（实测通过）；不要把所有宿主都当作需要这两步。

## 6. 清理与残留检查

- 宿主控制目录：`%TEMP%\daagent-mxc\control\<run_id>\`，正常情况下每次执行后由宿主侧自动清理；
  异常残留可手工整个删除 `%TEMP%\daagent-mxc`。
- 检查残留容器/进程：`--probe` 后若怀疑异常残留，重启后端服务并核对 `%LOCALAPPDATA%\Packages\daagent-*` 目录。
- Windows 降级 tier 使用 DACL 委派机制：以单任务、并发、强杀、重启恢复四种场景比较
  `icacls <目录>` 前后输出，确认无持久化额外授权（Phase 0 门禁用例）。

## 7. 故障排查

| 现象 | 可能原因 | 处置 |
|---|---|---|
| `MXC 执行器不存在` | 资产未部署 | 运行 `scripts/fetch_mxc.py` |
| `policy_validation: missing LOCALAPPDATA` | env 缺少 presence-only 必需项 | 由 `build_mxc_config` 自动补齐；出现即说明配置被外部改写，检查版本 |
| `STATUS_DLL_INIT_FAILED (0xC0000142)` | `ui.disable` 未置 false | 检查 config 生成代码；解释器启动需要 Win32k |
| 沙箱内 `PermissionError`（越界读/写） | 正常的隔离拒绝 | 若为业务需要，走升级审批（Phase 2）；不要为其放宽默认信封 |
| sidecar 缺失 / 任务失败于 `os._exit` | 用户代码提前终止 | 属预期失败路径；检查错误信息与 stdout 尾部 |
| 探测报告 `appcontainer-dacl` 而配置了 `REQUIRE_SUPPORTED_HOST=true` | 宿主不在支持矩阵 | 换支持宿主，或（仅开发）关闭该开关 |
| 任务全部在本地执行且无隔离信息 | `DAAGENT_SANDBOX_BACKEND` 仍为 local | 检查环境变量与服务重启状态 |

## 8. 安全注意

- **禁止**对不可信代码使用 `wxc-exec.exe --audit`（审计模式会放宽沙箱保护）。
- 遥测默认关闭，保持现状；如启用须评估合规要求。
- 网络默认 `deny`；联网需求走独立可信下载阶段或升级审批，不在分析任务中放开。
- 日志（`data_analysis.sandbox` logger）只记录标识与状态字段，不记录用户代码与数据内容。
