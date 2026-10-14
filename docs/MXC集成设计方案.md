# DAAgent × Microsoft MXC 沙箱集成设计（V2.2）

> **文档状态**：定稿（评审复核通过），可进入实施
> **修订日期**：2026-10-14（V2.0 修订稿 → V2.1 复核实测修订 → V2.2 审批模型演进）
> **实施目标**：在保留现有 Harness、审批和业务工具协议的基础上，将 MXC 作为可插拔的不可信代码执行后端。
> **适用范围**：第一阶段 `execute_python`；第二阶段 `run_terminal_command`；第三阶段评估 Skill。
> **评审结论**：架构可沿用；需先完成隔离能力准入、控制面保护、结果可信边界及异常清理验证，再开始全面接入。
> **依据说明**：本文基于用户提供的《DAAgent × MXC 沙箱集成设计》修订。**V2.1 定稿已完成关键主张的实机复核**（附录 A：8 项基线 + 6 项补充）；DAAgent 实际源码路径与接口签名已核对（2026-10-14，结论与 V1 描述一致）。
> **V2.1 修订摘要**：① `process.env` / `inheritDefaultEnv` / `cwd` / `timeout` 字段名经 SDK schema 生成代码（`wire.d.ts`）与实机运行双重确认；② 新增实测约束——`LOCALAPPDATA` 为 presence-only 必需项、`TEMP/TMP` 由平台覆盖重定向、只读语义（可读不可写）与未授权读取拦截均已验证、env 非全封闭（密钥隔离须全集枚举断言）；③ §3.3 `containment` 表述修正为“落点由宿主能力决定”；④ Phase 0 门禁分级（必须集/增强集）并补充宿主获取路径（§9）。
> **V2.2 修订摘要**：新增 §6.4「审批模型演进」——确立“沙箱优先 + 越界审批”三档模型（L0 沙箱内自动执行 / L1 策略升级审批 / L2 宿主机直通审批）；同步更新 §1.4 分期表、§6.2 交叉引用与 §9 Phase 2/Phase 3 验收项。

---

## 0. 相对 V1 的关键调整

| 主题       | V1 设计                                        | V2 决策                                                                                      |
| ---------- | ---------------------------------------------- | -------------------------------------------------------------------------------------------- |
| 架构       | `SandboxService + LocalBackend + MxcBackend` | **保留**，不重构 Harness 和 Agent                                                      |
| MXC 调用   | Python `subprocess` → `wxc-exec.exe`      | **保留**，采用官方非 SDK JSON 执行器路径                                               |
| 隔离判定   | probe 后按字段选择 containment                 | 改为**能力检测 + 配置预检 + 实际运行验证**；不根据单一 probe 字段做强隔离保证          |
| Windows 10 | 已测可用                                       | **仅标记实验性、非官方支持目标**；开发可显式启用，生产不可因八项测试直接宣称安全       |
| Sidecar    | `result.json` 作为结果回传                   | 控制文件独立只读，Sidecar 视为**不可信业务输出**；宿主决定最终执行状态                 |
| 文件权限   | `output_dir` 连同 `.runtime` 一起读写      | **宿主控制目录与沙箱写入目录物理分离**，避免不可信代码覆盖 wrapper / config            |
| 环境变量   | 字段待定、可能继承宿主环境                     | 使用 `process.env` / `process.inheritDefaultEnv`，默认最小环境；Windows 必需变量显式处理 |
| 执行策略   | MXC 与 local 的行为差异留待评估                | 显式定义可执行程序、子进程、网络、权限和任务隔离策略；不能无条件称“语义一致”               |
| 降级       | `auto` 在 probe 不可用时回退                 | 仅**兼容性任务 + 开发环境明确许可**可降级；不可信生产任务 fail-closed                  |
| 审计       | `size + mtime_ns`                            | 保留增量检测，加上**关键产物 SHA-256、类型检查和链接/重解析点检查**                    |
| 测试       | 八项功能测试 + 常规回归                        | 增加 Sidecar 伪造、环境泄露、并发、取消、崩溃、权限清理等**安全门禁测试**              |

## 1. 背景、目标与范围

### 1.1 现有体系（沿用原稿描述，待源码核对）

| 层            | 当前位置                                                    | 当前机制                                                                       | 评价                                 |
| ------------- | ----------------------------------------------------------- | ------------------------------------------------------------------------------ | ------------------------------------ |
| 审批闸门      | `runtime/harness.py`、`server/main.py`                  | Terminal / Skill 一次性人工审批；原稿描述 `id(action)` 消费制、300s 等待上限 | 工作流审批，**不是隔离边界**   |
| 执行路由      | `tools/registry.py`、`runtime/sandbox.py`               | 默认 `SubprocessSandbox`，有 4 个 in-process 例外                            | 进程组织能力，不等同不可信代码沙箱   |
| Python runner | `tools/code_tools.py`、`runtime/_python_code_runner.py` | `python -I -B`、monkey-patch、产物快照及承诺校验                             | 应用层防误操作能力，不能视作安全隔离 |

### 1.2 核心目标

1. 使用 MXC 提供**经实测验证的宿主 OS 级文件、网络和执行限制**，不依赖 Python monkey-patch 作为唯一边界。
2. 保持上层 Agent 的工具名称、审批流程、返回结构以及前端交互**兼容**；如有不兼容差异，必须列出并通过测试，而非声称完全透明。
3. `SandboxService` 可配置、可探测、可观察、可回退，但不得在要求隔离的任务上静默降级。
4. 优先支持单次 Python 分析；Terminal 和 Skill 分阶段接入，不一次性重构所有工具。
5. 建立端到端安全测试、运行 Trace 和异常清理能力。

### 1.3 非目标

- 本期不开发新的虚拟机、容器运行时或自定义 MXC Rust 后端。
- 不引入 Node.js 常驻中间服务，不依赖 Node SDK；使用 `wxc-exec.exe` 官方非 SDK 接入方式。
- 不立即重构可信文件工具（`write_artifact`、`write_workspace_file`、`edit_workspace_file`）；但必须复核路径校验及重解析点防护。
- 不声称 MXC 可以杜绝 Prompt Injection、数据污染、隐私泄露、DOS 或恶意产物；这些仍需独立防线。
- 本期不承诺持久化容器、多租户强隔离、跨平台零配置迁移或 CPU/内存配额均可用。

### 1.4 分期范围

| 行为                                | 分期     | 决策                                                                                        |
| ----------------------------------- | -------- | ------------------------------------------------------------------------------------------- |
| `execute_python`                  | Phase 1  | 首批接入；每次任务一个临时隔离执行，严格约束输入输出路径                                    |
| `run_terminal_command`            | Phase 2  | 接入 MXC 沙箱后，审批模型演进为“沙箱内自动执行 + 越界审批”（§6.4）；过渡期保留原人工审批 |
| Skill 脚本（`skills/runtime.py`） | Phase 3  | 先审计 SkillRuntime 资产注入方式，再决定接入                                                |
| 可信文件操作工具                    | 暂不接入 | 不为单次受控文件写入单独启动沙箱，但路径输入仍视为不可信                                    |
| 确定性数据工具                      | 暂不接入 | 不直接执行 LLM 生成程序；仍需要输入文件安全和资源限制                                       |

---

## 2. 威胁模型与安全边界

### 2.1 信任域

```text
用户 / 上传数据 / LLM 输出（不可信）
             │
             ▼
Agent Tools（参数校验 / 审批 / 权限决策，可信应用代码）
             │
             ▼
SandboxService（后端选择 / 安全策略 / Trace / 错误归一化）
             │
             ▼
MxcBackend（宿主侧配置生成 / 资产校验 / 结果校验 / 清理）
             │
             ▼
MXC（请求支持的 OS 隔离策略，具体有效性按后端与环境确认）
             │
             ▼
Python 或 Shell + 用户生成代码（不可信）
             │
             ▼
stdout / stderr / result.json / 图表 / CSV / 文件（均视为不可信输出）
```

**不可跨越的原则**：LLM 不决定是否启用沙箱、最低隔离要求、是否降级、MXC 执行器路径、授权目录或环境密钥。这些只能由后端可信配置与权限系统决定。

### 2.2 需要防范的风险

| 风险                                      | 防护责任                                                                                                                                                                                         |
| ----------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| 非授权目录读取/写入                       | MXC 文件策略 + 宿主路径解析/边界校验 + 真机越权测试                                                                                                                                              |
| 网络外传/回环访问                         | MXC 具体后端可执行的网络策略 + 请求前后验证                                                                                                                                                      |
| 子进程逃逸/异常残留                       | 后端隔离、任务树终止、超时/取消/崩溃测试                                                                                                                                                         |
| 篡改 `task.py` / `config.json`        | 控制面独立目录；沙箱仅允许必要的读取权限                                                                                                                                                         |
| 伪造 `result.json` / 产物               | 结果归类为不可信数据；宿主校验格式、路径、退出情况与期望产物                                                                                                                                     |
| 密钥、宿主环境泄露                        | 环境白名单、禁止默认注入业务凭证、不授予敏感路径；实测 `inheritDefaultEnv: false` 下仍有少量平台变量渗透（`LSBOX_*`、`PYTHONHOME`），验收须做 env **全集枚举断言**（见 §9 Phase 0） |
| 符号链接、junction、reparse point、TOCTOU | 宿主规范化及最终目标检查；覆盖竞态测试                                                                                                                                                           |
| 大输出、文件洪泛、CPU/内存耗尽            | 超时、流式输出上限、产物上限、并发闸门；资源配额按后端能力实现                                                                                                                                   |
| 任务间互相污染                            | 每次执行独立目录、独立 ID、并发隔离与清理测试                                                                                                                                                    |

### 2.3 必须承认的边界

- **不可信代码与 Python wrapper 处于同一解释器时，wrapper 写出的 `result.json` 无法构成强可信执行证明**。恶意代码可能修改可写结果文件、调用 `os._exit()` 或干扰 wrapper。V2 只保证宿主**不会把 Sidecar 中的 `ok: true` 当作安全证明**；如果以后需要可验证执行证明，应将受信任监督器放在不可信解释器进程之外，属于独立改造。
- `stdout`、`stderr`、用户代码返回值、CSV、图表、HTML、JSON 都是不可信内容；不能直接作为新的高权限指令执行。
- `processcontainer`、`appcontainer-dacl`、`bubblewrap` 不自动等于 VM 隔离；隔离能力必须按宿主系统、具体后端及实测策略声明。
- 本设计不把“没有测试出逃逸”当作“证明不存在逃逸”。

---

## 3. 可行性与平台准入

### 3.1 已有实测证据（2026-10-14 复核）

开发机为 **Windows 10 19045.6466**，`wxc-exec.exe --probe` 报告 `tier=appcontainer-dacl`；**8 项基线全部复核通过**（probe、基础命令、只读、读写、越界拒绝、`.venv` Python、pandas 流水线、网络 deny），另完成 **6 项补充复核**（环境字段与最小环境、`LOCALAPPDATA` 约束、只读严格语义、未授权读取拦截、`TEMP` 重定向、env 渗透观察），详见附录 A。

**解释**：这说明该二进制及其配置在该机可执行所列功能，不代表该 Windows 10 构建进入 MXC 官方支持矩阵，也不覆盖恶意代码对抗、并发/崩溃清理等情况。

### 3.2 环境策略

| 环境                       | 准入方案                                                              | 安全说明                             |
| -------------------------- | --------------------------------------------------------------------- | ------------------------------------ |
| Windows 10 当前开发机      | 可显式启用 `mxc` 实验模式；记录 `appcontainer-dacl`；执行安全测试 | **不视为已具备生产级隔离保证** |
| 官方受支持 Windows 11 版本 | 按版本补丁要求完成 probe、策略预检、功能与安全验收                    | 仍以实际生效的隔离能力为准           |
| Linux                      | 后续使用 `bubblewrap` 或选定后端                                    | 路径、网络、挂载和权限行为需独立验收 |
| 生产高敏感任务             | 要求受支持宿主 + 满足任务必需的文件/网络/进程约束；否则拒绝执行       | 不自动退回 `local`                 |

官方 Windows 11 版本和构建号应以部署时的 [OS-version support](https://github.com/microsoft/mxc/blob/main/docs/backends/process-container/os-version-support.md) 为准，不将 Windows 10 试验结果视为官方支持声明。

### 3.3 隔离准入判定

`MxcBackend.probe()` 使用原稿已测试的 `wxc-exec.exe --probe`，获取支持信息及警告；但**probe 是建议性检查，不是运行成功或完整策略强制执行的证明**。

准入流程：

1. 校验执行器来源、版本、hash、架构及签名/分发链信息（可用时）；失败拒绝。
2. 获取宿主平台、版本、MXC probe 结果和告警，并记录。
3. 生成**完整实际请求**，依据选定执行器版本的 schema 校验字段和值。
4. 检查当前模式规定的最低隔离要求与所需能力（文件只读、文件读写、出站网络拒绝、进程超时、清理）。
5. 用受限 smoke test 验证完整策略可以启动，并在集成测试中验证越权阻断。**不可把 smoke test 替代成安全认证**。
6. 执行后记录实际可获得的后端/tier 信息；若运行时无法可靠获得，则记为 `unknown`，不能编造 `strong` 等级。
7. 请求与有效后端不兼容时 **fail-closed**。

> 官方 schema 的 `containment: "process"` 是跨平台抽象意图：具体落点由宿主与后端能力决定——受支持的 Windows 宿主通常落到 `processcontainer` 系，Linux 为 `bubblewrap`、macOS 为 `seatbelt`；**本机（Windows 10）实测落到 `appcontainer-dacl` tier**。不要把 `process` 理解为一种固定强度的“低级模式”，实际 tier 以 probe 与运行记录为准。

---

## 4. 总体架构与契约

### 4.1 分层架构

```text
Harness / 审批系统（沿用）
  └── Tool Registry（微改：传递后端服务端策略）
       ├── execute_python（Phase 1）
       └── run_terminal_command（Phase 2）
            │
            ▼
       SandboxService
         ├── PolicyResolver（可信环境中解析策略）
         ├── BackendSelector（按策略探测、拒绝或降级）
         ├── LocalBackend（原实现，仅兼容或显式允许场景）
         └── MxcBackend（wxc-exec + JSON + 输出审计）
                ├── Host Control Plane（wrapper、payload、config，只读暴露）
                ├── MXC OS Containment（实际后端依宿主决定）
                └── Work Output Plane（产物/缓存/结果，不可信可写）
```

### 4.2 核心数据契约（接口示意，非可直接运行代码）

```python
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Mapping, Protocol

IsolationRequirement = Literal[
    "legacy_allowed",          # 仅明确允许的兼容任务
    "mxc_required",            # 必须由 MXC 执行；Windows 10 仅开发实验
    "supported_mxc_required",  # 受支持宿主 + 能力检查；面向生产不可信任务
]

@dataclass(frozen=True)
class ExecutionLimits:
    timeout_seconds: float
    max_stdout_bytes: int
    max_stderr_bytes: int
    max_result_bytes: int
    max_artifact_files: int
    max_artifact_bytes: int

@dataclass(frozen=True)
class SandboxPolicy:
    required_isolation: IsolationRequirement
    allow_local_fallback: bool            # 仅可由服务端可信配置赋值
    allow_network: bool                   # Phase 1 默认 False
    allow_child_processes: bool           # 默认 False；若后端不能强制则需拒绝或降级策略声明
    limits: ExecutionLimits

@dataclass(frozen=True)
class PythonRunRequest:
    run_id: str
    code: str
    data_paths: tuple[Path, ...]
    output_dir: Path
    workspace_root: Path | None
    python_executable: Path
    expected_artifacts: tuple[str, ...]
    skill_assets: Mapping[str, str]
    timeout_seconds: float

@dataclass(frozen=True)
class CommandRunRequest:
    run_id: str
    command: str
    workspace_root: Path
    shell: str
    timeout_seconds: float | None

class SandboxBackend(Protocol):
    name: str
    def probe(self) -> "BackendProbe": ...
    def run_python(self, request: PythonRunRequest, *, policy: SandboxPolicy, cancel_check=None) -> dict: ...
    def run_command(self, request: CommandRunRequest, *, policy: SandboxPolicy, cancel_check=None) -> dict: ...
```

`SandboxPolicy` 的构造者是 `PolicyResolver`（服务端可信代码），**不作为 LLM 工具参数公开**。对于不可强制落实的策略，必须拒绝对应任务或明确限制允许任务类型，不能仅在文档中宣称“已禁止”。

### 4.3 兼容性契约

保持原稿既有成功响应的字段：

- Python：`{stdout, stderr, result, artifacts, changed_artifacts, artifact_details, runtime}`；失败使用现有 `CodeExecutionError` 分类。
- Terminal：`{command, shell, exit_code, output, output_truncated, timed_out, duration_ms, changed_files, changes_truncated, ...}`。
- `runtime` 扩充后端和隔离状态字段，但不删除已有字段；LLM 面向的已有语义不因新增元数据而改变。
- 逐项核对以下差异：禁止子进程、环境变量、网络、异常类别、编码、stdout 截断、缓存位置、文件时间戳、Python `-I -B`、临时文件、matplotlib 与 pandas。

### 4.4 服务端注入与权限控制

原稿建议通过 `ExecutionContext.tool_context()` 传递 JSON 字符串 `sandbox_backend`，V2 继续采用，但增加防护：

1. `server/main.py` 从**受信任的服务端配置**加载模式，生成任务级 `SandboxPolicy`。
2. `ExecutionContext` 只传递必要的后端标识和已解析的可信策略索引；客户端/LLM 输入不能覆盖这些字段。
3. `registry` 只允许调用预定义的 `execute_python` / `run_terminal_command` 服务，不将任意用户参数解释为执行器路径或授权目录。
4. `SandboxService` 根据任务类型、配置和宿主能力最终裁决后端；工具级绕过后端选择必须禁止。
5. Phase 1 **只切换 Python 工具**；Terminal 尚未接入 MXC 时仍保持原行为，日志必须反映其真实后端，不能因为全局配置为 `mxc` 就声明所有工具都已隔离。

---

## 5. MxcBackend 详细设计

### 5.1 目录布局：控制面与产物面分离（P0）

**禁止继续把 `payload.py`、`task.py`、`config.json` 放在授权读写的 `output_dir/.runtime/` 中。** 建议目录：

```text
<app_state_root>/mxc/control/<run_id>/      # 宿主控制目录；位于任何沙箱 RW 授权之外
    payload.py                             # 用户生成代码；任务运行期沙箱只读
    task.py                                # 固定 wrapper；沙箱只读
    config.json                            # 宿主读取的执行配置；沙箱无需写权限

<workspace>/outputs/<run_id>/              # 本次任务唯一授权读写输出目录
    artifacts/                             # CSV、图片等最终产物
    runtime_cache/                         # TEMP、MPLCONFIGDIR 等
    sidecar/result.json                    # 非可信结果数据；必须限长、验证结构
```

实现要求：

- `run_id` 使用难以预测的唯一 ID（如 UUIDv4），不直接依赖固定 hash 作为唯一性来源。
- `control` 目录不得位于授权为可写的祖先目录下；避免父目录写权限穿透。
- 即使控制目录以只读方式映射给沙箱，仍应测试 Windows DACL 实际生效与拒写效果。
- 独立任务不能共享可写的 sidecar、cache、临时目录；清理须在宿主侧完成。
- 明确 `.runtime`、`sidecar` 与 `runtime_cache` 不属于用户承诺产物，不进入公开产物列表。

### 5.2 配置字段与策略映射

| DAAgent 输入/决策            | MXC 稳定 schema 字段                         | 处理规则                                                                                                                                                                                                    |
| ---------------------------- | -------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 一次性执行                   | `containerId`、`lifecycle.destroyOnExit` | 每次生成唯一 ID；异常仍需额外清理检查                                                                                                                                                                       |
| 跨平台进程意图               | `containment: "process"`                   | 默认；若显式选 concrete backend，需验证其可用性和能力                                                                                                                                                       |
| 解释器执行                   | `process.commandLine`                      | 宿主构造命令，正确引用路径；保持 Python `-I -B` 等已有约定                                                                                                                                                |
| 工作目录                     | `process.cwd`                              | 使用任务目录；不默认继承宿主当前目录                                                                                                                                                                        |
| 环境变量                     | `process.env`                              | `NAME=value` 数组；不能传入完整宿主环境。**`LOCALAPPDATA` 为 Windows presence-only 必需项（值任意；缺失由执行器直接拒绝启动——实测）**                                                           |
| 环境继承                     | `process.inheritDefaultEnv`                | 默认 `false`；实测该模式下仍有少量平台变量渗透（`LSBOX_*`/`PYTHONHOME`），密钥隔离须以全集枚举断言验收                                                                                                |
| 临时目录                     | （平台管理）                                 | **`TEMP`/`TMP` 由平台覆盖**：实测重定向到容器私有目录 `%LOCALAPPDATA%\Packages\<containerId>\AC\Temp`（随容器销毁，天然隔离）；任务缓存路径请改用 `MPLCONFIGDIR` 等显式变量（实测手动值有效） |
| 超时                         | `process.timeout`                          | 毫秒；叠加宿主进程超时与取消控制                                                                                                                                                                            |
| 数据文件/Skill 资源/控制脚本 | `filesystem.readonlyPaths`                 | 最小权限、只读；路径规范化后生成                                                                                                                                                                            |
| 输出与缓存目录               | `filesystem.readwritePaths`                | 每任务独立路径；不包含宿主控制目录                                                                                                                                                                          |
| 显式禁止路径                 | `filesystem.deniedPaths`                   | 防御性补充；不能替代最小授权和后端策略验证                                                                                                                                                                  |
| 网络                         | `network.egress.default: "deny"`           | 默认拒绝；额外 ingress/loopback 字段按具体后端能力验证                                                                                                                                                      |
| UI                           | `ui.disable`                               | 原 Win10 测试需 `false`；仅作为经过评估的兼容选项，不默认泛化到所有宿主                                                                                                                                   |
| 版本                         | `version: "1.0.0"`                         | 与所使用执行器支持的 schema 精确匹配                                                                                                                                                                        |

**环境字段更正（已复核）**：官方 schema 使用 `process.env` 和 `process.inheritDefaultEnv`，不是原稿待确认的 `process.environment`；`cwd`、`timeout` 字段名同经 SDK schema 生成代码（`wire.d.ts`）与实机运行双重确认。Windows 容器对 `SYSTEMROOT`、`LOCALAPPDATA` 有特殊要求：实测 `LOCALAPPDATA` 为 presence-only 必需项，缺失时执行器以 `policy_validation` 拒绝启动（报错信息给出补全变量 / `inheritDefaultEnv: true` / 省略 `env` 三种解法）。仅应传必要系统变量和任务缓存路径，不能用 `inheritDefaultEnv=true` 代替环境白名单审查。

### 5.3 JSON 结构模板（**占位示例，需按当前宿主预检**）

```json
{
  "version": "1.0.0",
  "containerId": "daagent-<uuid>",
  "containment": "process",
  "lifecycle": {
    "destroyOnExit": true,
    "preservePolicy": false
  },
  "process": {
    "commandLine": "\"<python.exe>\" -I -B \"<control/task.py>\"",
    "cwd": "<workspace/outputs/run_id>",
    "timeout": 60000,
    "env": [
      "SYSTEMROOT=<trusted-system-root>",
      "LOCALAPPDATA=<approved-local-app-data>",
      "MPLCONFIGDIR=<runtime-cache>",
      "PYTHONUTF8=1"
    ],
    "inheritDefaultEnv": false
  },
  "filesystem": {
    "readonlyPaths": ["<control>", "<data_file>", "<venv>", "<base_python>"],
    "readwritePaths": ["<workspace/outputs/run_id>"],
    "deniedPaths": []
  },
  "network": {
    "egress": { "default": "deny" }
  },
  "ui": { "disable": false }
}
```

模板说明：

- 仅适用于已经证实该组字段与权限在目标宿主可用的配置；`ui.disable=false` 来源于原 Win10 实测，不代表推荐的通用安全默认值。
- `TEMP`/`TMP` 由平台管理并覆盖手动值（实测重定向到容器私有目录，随容器销毁）；任务缓存改用 `MPLCONFIGDIR` 等显式变量。`LOCALAPPDATA` 为 presence-only 必需项（值任意；缺失直接导致启动失败）。
- 配置中的路径占位符**不直接替换来自 LLM 的字符串**，应通过受信任 `Path` 对象构造、schema 序列化，并针对 Windows 引号、驱动器、UNC 路径做测试。
- `.venv` 与 base Python 的读取路径遵照原稿从 `pyvenv.cfg` 解析，但需处理相对路径、目录链接和版本变化，禁止简单无限授权整个用户主目录。
- 外部用户数据允许只读挂载，**其上层父目录不能因 convenience 而整体开放为可写**。

### 5.4 Python 执行流程

```text
 1. 接收 PythonRunRequest（run_id、代码、数据授权、输出路径）
 2. 服务端 PolicyResolver 确定隔离要求、网络、超时、子进程策略
 3. 校验路径、解释器、资产来源与 `process.env` 平台必需项（Windows：`LOCALAPPDATA`）；规范化并拒绝逃逸/链接风险
 4. MxcBackend probe + 能力/策略准入；必要时拒绝运行
 5. 创建宿主私有 control 目录与任务专属 output/cache 目录
 6. 写入 payload.py / 固定 task.py / config.json；设定读写隔离
 7. 宿主侧记录 before 快照、执行元信息
 8. subprocess 启动 wxc-exec.exe；并发读取 stdout/stderr、监控超时/取消
 9. 收集退出码、终止原因、后端警告、清理结果
10. 校验 sidecar 的大小、JSON schema、run_id、字段类型、允许的结果类型
11. 验证产物路径、文件类型、SHA-256、总量限制和 expected_artifacts
12. 根据宿主侧执行状态及业务校验组合现有响应或 CodeExecutionError
13. 在 finally 执行清理；若清理异常保留受控证据并记录告警
```

### 5.5 Sidecar 结果协议：不可信数据（P0）

继续保留 `task.py` 注入：`DATA_FILES`、`DATA_FILE_MAP`、`OUTPUT_DIR`、`WORKSPACE_DIR`、`SKILL_ASSETS`、`ARTIFACTS`、`result`，以兼容原有模型代码。

建议 Sidecar 格式：

```json
{
  "schema_version": 1,
  "run_id": "<uuid>",
  "ok": true,
  "result": { "summary": "..." },
  "error_type": null,
  "error": null
}
```

宿主处理规则：

1. Sidecar 字段全部视为不可信；`run_id` **仅用于关联/过滤，不是签名或防伪凭证**。
2. 文件必须存在于当前任务允许目录；限定最大大小（建议默认 1 MiB，可配置），拒绝异常类型和超深嵌套 JSON。
3. 对 `result` 做 JSON 可序列化和尺寸限制；不执行 Sidecar 中的代码或路径指令。
4. 优先依据**宿主观察到的进程退出、超时、取消、资源/权限错误**确定底层执行状态；Sidecar 的 `ok` 仅代表用户层返回数据，不得覆盖宿主错误。
5. 期望产物由宿主独立检查；`ok=true` 但缺少文件、出现越界产物或违反策略，一律失败。
6. 由于用户代码可能主动提前退出或伪造产物，**不能把 exit code=0 + Sidecar 合法表述为恶意代码已被证明正确执行**。若业务需要强执行证明，另行设计沙箱外监督器。
7. 若 Sidecar 缺失、畸形、过大，产生明确的 `ProtocolError`（映射到项目既有错误体系），保留受限 stdout/stderr 供诊断。

### 5.6 文件产物审计

- 快照以 `size + mtime_ns` 识别疑似变化；对于变化文件或承诺产物，计算 SHA-256 用于可靠内容对比。
- 枚举时拒绝越界路径、符号链接、Windows junction/reparse point 以及非常规文件类型；必须核对**最终落点**仍在任务授权目录内。
- 允许的产物扩展名/文件类型由业务策略确定；不能简单信任文件后缀。预览 HTML、SVG、Notebook 等主动内容时需额外消毒与隔离。
- 限制产物文件数量、单文件和总字节数；限制目录深度及枚举耗时。超限时返回结构化错误，而非无限扫描。
- `runtime_cache/`、`sidecar/` 不计为用户产物；公共前端仅展示通过宿主校验的 `artifacts`。
- 对于非确定性输出，比较**产物结构、数据内容和容差**；只有确定性基准 fixture 才要求两后端逐字节一致。

### 5.7 子进程与其他行为差异

原 runner 对 `subprocess.*` 实施禁用；MXC OS 隔离**不天然等于禁止创建子进程**。因此：

- Phase 1 默认业务策略 `allow_child_processes=False`，必须核实选定后端能否强制该限制。
- **若当前后端无法强制禁止子进程**，不得宣称与旧 runner 安全语义等价。可选择维持应用层拦截作为附加兼容措施，但要明确它不是 OS 强制边界；对必须强禁止的任务拒绝执行或选用其他能实现该策略的后端。
- 若允许子进程，应确认其继承约束，并测试多级进程树的超时与强制取消。
- 网络默认 deny；允许下载依赖或模型的需求，应采用独立可信下载阶段或经审批的最小网络规则，不能在分析任务中默认开放互联网。

### 5.8 异常与清理

- 采用宿主侧超时监控 + MXC `process.timeout`；禁止因输出管道阻塞而无法取消。
- `subprocess` 执行器本身、其子进程和 MXC 内部状态的清理行为需要真机验证；不能仅靠 `destroyOnExit` 推断崩溃后无残留。
- 记录清理是否成功、残留进程/目录/策略告警；提供幂等启动前残留检查与独立清理命令。
- Windows AppContainer/DACL 模式可能修改宿主 ACL。必须在单任务、并发、强制终止、重启恢复等场景比较**前后 ACL**，确认不存在持久化的额外授权。
- `--audit` 模式可能放宽沙箱保护；只用于可信测试负载，不运行 LLM 生成的不可信代码。

---

## 6. 后端选择、降级与发布控制

### 6.1 服务端配置

| 配置项                                    | 可选值                           | 建议默认                                         | 说明                                |
| ----------------------------------------- | -------------------------------- | ------------------------------------------------ | ----------------------------------- |
| `DAAGENT_SANDBOX_BACKEND`               | `local` / `mxc` / `auto`   | `local`（兼容旧环境）                          | 一期仅影响已经接入的 Python 工具    |
| `DAAGENT_MXC_EXECUTABLE`                | 绝对路径                         | `<project>/vendor/mxc/bin/<arch>/wxc-exec.exe` | 必须验证来源和 hash                 |
| `DAAGENT_SANDBOX_PROFILE`               | `development` / `production` | `development`（显式部署配置应覆盖）            | 降级与最低隔离策略的准入条件        |
| `DAAGENT_SANDBOX_ALLOW_UNSAFE_FALLBACK` | `true` / `false`             | `false`                                        | 仅开发 + `legacy_allowed` 可开启  |
| `DAAGENT_MXC_REQUIRE_SUPPORTED_HOST`    | `true` / `false`             | 生产 `true`                                    | Windows 10 实验环境仅可设 `false` |

执行隔离需求优先级：**服务端安全策略/任务分类 > 受信任系统配置 > 默认值**；调用方不能通过 JSON context 或工具参数降低任务最低隔离要求。

### 6.2 后端路由矩阵

| 模式/条件                                                               | 执行行为                                     |
| ----------------------------------------------------------------------- | -------------------------------------------- |
| `local` + `legacy_allowed`                                          | 使用原实现；记录 `effective_backend=local` |
| `local` + `mxc_required` / `supported_mxc_required`               | **拒绝执行**，不能伪装已隔离           |
| `mxc` + 能力满足                                                      | 使用 MXC                                     |
| `mxc` + probe 不可用、权限不满足或执行期失败                          | **直接报错**，不降级                   |
| `auto` + MXC 能力满足                                                 | 使用 MXC                                     |
| `auto` + probe 失败 + `legacy_allowed` + development + 显式允许降级 | 可回退 `local`，返回显著 runtime 警告      |
| `auto` + 其他失败或不可信生产任务                                     | **拒绝执行**；执行中不得降级           |

**重要**：生产场景执行 LLM 生成的 Python 代码，默认要求 `supported_mxc_required` 或更严格的平台专项规则；若当前宿主不满足，应阻止运行，而不是为追求可用性退回 monkey-patch。

> **维度区分**：本节矩阵决定“请求由哪个后端隔离执行”；终端工具审批策略的演进（L0/L1/L2）见 §6.4，二者正交。

### 6.3 灰度与回滚

1. 初始维持 `local` 不变，先验证新封装不会引入调用环路或响应变化。
2. 在开发环境使用 `mxc` 显式开启小流量、非敏感任务；分别记录成功率、超时率、隔离失败、资源占用。
3. 完成 P0 安全门禁和目标支持宿主测试后，才对不可信任务扩大 MXC 比例。
4. 回滚操作是停止新 MXC 任务、切换配置、重启服务并检查**遗留任务状态**；运行中的任务不能仅改环境变量就假设已安全终止。
5. 回滚后的 `local` 任务不具备与 MXC 等价的隔离保证；产品展示及日志应准确注明。

### 6.4 审批模型演进：沙箱优先 + 越界审批（Phase 2 目标）

**动机**：现状 `run_terminal_command` 每次调用均需人工审批，其根源是终端命令在宿主机直接执行、没有沙箱兜底——审批在此承担的是“补偿性控制”。Phase 2 终端接入 MXC 后，终端与 `execute_python` 能力同构（受同一文件系统/网络/超时约束），审批可从“全量逐条”演进为“**按越界触发**”：常规操作在沙箱内自动执行，只有需要打破默认信封的请求才进入人工审批。

**三档模型**：

| 档位                  | 触发条件                                                  | 审批                                     | 执行位置                              |
| --------------------- | --------------------------------------------------------- | ---------------------------------------- | ------------------------------------- |
| **L0 自动**     | 请求在默认信封内：只读授权数据 + 写产物目录 + 断网 + 限时 | 无                                       | MXC 容器                              |
| **L1 策略升级** | 越界请求：需要联网、需要额外目录授权、需要更长超时等      | 人工批准**策略增量**（一次性令牌） | **仍在 MXC 容器**（信封放宽版） |
| **L2 直通**     | 沙箱内无法完成的操作（系统级安装、宿主服务操作等）        | 人工逐条批准（门槛最高）                 | 宿主机 + 变更审计                     |

关键语义：**L1 批准后不退出沙箱**——放宽的是信封（如某域名网络、某目录只读），隔离仍然生效；真正“出沙箱”只有 L2 一条路。

**升级触发路径**（两条并存，以预检为主）：

1. **预检式**：LLM 在工具参数中显式声明需求（如 `network_required`、`extra_readonly_paths`），由 `PolicyResolver` 判定；越界则挂起执行、发起审批请求；批准后以放宽策略一次执行完成（避免“先失败一次再重试”的往返）。
2. **失败驱动**：沙箱拒绝（如网络 `PermissionError`）作为 Observation 回喂 LLM；LLM 可显式发起升级请求，审批界面展示越界原因后挂起。

**界面要求**：升级审批必须清楚展示“批的是什么”——越界原因、具体策略增量（哪个域名 / 哪个目录 / 超时调整量）与一次性语义，避免习惯性批准导致审批失效。

**底线原则**（与 §4.4、§6.2 一致）：

1. LLM 只能“请求”，不能自决升级/降级；工具参数不可降低或绕过隔离要求；
2. 批准 = 一次性令牌（一次批准 → 一次放宽执行），不产生长期放宽；
3. 批准 ≠ 退出沙箱——L1 仍在容器内；只有 L2 到宿主机；
4. 审计全覆盖：每次升级记录原因、策略增量与执行结果（Trace 字段见 §8.3）。

**前置条件与残余风险**（Phase 2 验收前关闭）：

- **资源配额未验证**：AppContainer 为进程级隔离（共享内核），L0 免审批模式需以超时 + 产物体积/数量上限（`ExecutionLimits`）兜底；硬配额需求另行评估 hyperlight（微虚机，本机 probe 显示可用、未实测）或迁移受支持后端；
- **判定器正确性**：升级判定完全依赖服务端 `PolicyResolver` 规则集，需单元测试 + 对抗用例（判定只认服务端配置与请求结构，LLM 无法通过声明影响判定强度）；
- **过渡期策略**：终端接入 MXC 前保留原人工审批不变；接入后按“L0 默认、L1 新增升级链路、L2 保留原语义”切换。

---

## 7. 文件改动清单

> 路径来自 V1 设计，实施时须以实际源码结构核对，特别避免 `LocalBackend` 调回 `execute_python_code()` 再次进入 `SandboxService` 造成递归。

| #  | 目标文件                                          | 修改内容                                              | 阶段 |
| -- | ------------------------------------------------- | ----------------------------------------------------- | ---- |
| 1  | `runtime/sandbox_backends/__init__.py`          | 包导出                                                | 1    |
| 2  | `runtime/sandbox_backends/base.py`              | 请求、响应、探测、策略与后端协议                      | 1    |
| 3  | `runtime/sandbox_backends/service.py`           | 服务端路由、拒绝策略、probe 缓存                      | 1    |
| 4  | `runtime/sandbox_backends/policy.py`            | **新增** `PolicyResolver`、最低隔离与降级规则 | 0/1  |
| 5  | `runtime/sandbox_backends/local_backend.py`     | 封装现有 runner 底层实现，避免循环调用                | 1    |
| 6  | `runtime/sandbox_backends/mxc_backend.py`       | MXC 执行器、环境配置、输出与清理                      | 0/1  |
| 7  | `runtime/sandbox_backends/_mxc_task_wrapper.py` | 兼容命名空间、写不可信 Sidecar                        | 1    |
| 8  | `runtime/sandbox_backends/artifacts.py`         | 宿主侧产物检查、hash、路径防护                        | 1    |
| 9  | `runtime/sandbox_backends/trace.py`             | **新增**隔离状态与清理审计事件                  | 1    |
| 10 | `tools/code_tools.py`                           | `execute_python_code` 经 Service 路由               | 1    |
| 11 | `tools/terminal_tools.py`                       | Terminal 同类路由，维持审批                           | 2    |
| 12 | `tools/registry.py`                             | 透传服务器可信后端上下文                              | 1/2  |
| 13 | `runtime/execution_context.py`                  | 增加后端选择的可信上下文字段                          | 1    |
| 14 | `server/main.py`                                | 启动时载入、验证配置及策略                            | 1    |
| 15 | `vendor/mxc/VERSION.md`                         | 锁定版本、来源、校验和与许可                          | 0    |
| 16 | `vendor/mxc/bin/<arch>/`                        | 部署原生二进制；不直接纳入 Git                        | 0    |
| 17 | `scripts/fetch_mxc.py`                          | **新增/完善**锁版本下载和校验                   | 0    |
| 18 | `tests/unit/runtime/test_sandbox_backends.py`   | 配置、协议、策略单测                                  | 0/1  |
| 19 | `tests/integration/test_mxc_sandbox.py`         | 功能、对抗、异常和并发真机测试                        | 0/1  |
| 20 | `docs/operations/mxc.md`                        | **新增**平台准入、部署、清理和故障排查          | 0/1  |

**保持不改或仅最小抽取**：`_python_code_runner.py` 的原 monkey-patch 能力、Harness 的审批协议、前端工具名称、`_sandbox_worker.py`、SkillRuntime。原 runner 可抽出共享产物校验函数，但要用回归测试保证 `local` 保持兼容。

---

## 8. 部署、资产与运维

### 8.1 MXC 原生资产

沿用原稿的 npm 包提取方法，而不是运行 Node SDK：

1. 固定经过验收的包版本（例如原稿实验使用的 `@microsoft/mxc-sdk@1.0.0`；部署时先确认该版本资产完整）。
2. 通过 `npm pack` 或可信分发渠道取得包，提取目标架构的 `wxc-exec.exe` 及**必要配套文件**到 `vendor/mxc/bin/<arch>/`。
3. `VERSION.md` 记录包版本、来源、下载时间、SHA-256、架构、测试结果和许可信息。
4. `.gitignore` **只忽略二进制目录**，让 `VERSION.md` 与获取脚本可提交；CI 校验资产 hash，禁止启动时默默获取不明版本。
5. 执行器路径由服务端配置指定；不允许 Agent/用户通过命令参数改写。

### 8.2 主机准备

原稿记录 `wxc-host-prep.exe` 可在提升权限下运行 `prepare-system-drive`、`prepare-null-device`；这属于**对宿主系统状态的修改**，不是无风险的通用安装步骤。

- 只在 probe 明确提示且运维审批后执行，先备份/记录前后安全描述符。
- 在生产部署中区分“需要主机准备的 tier”和“不需要的 tier”；不把 Windows 10 特殊处理写成所有宿主必需步骤。
- 严格限制管理员操作，Agent 运行时不以管理员权限启动准备程序。

### 8.3 Trace、日志与指标

每次执行建议记录（敏感内容按需脱敏）：

| 类别 | 最少字段                                                                                              |
| ---- | ----------------------------------------------------------------------------------------------------- |
| 标识 | `run_id`、任务/工具标识、开始/结束时间                                                              |
| 后端 | requested backend、effective backend、host OS/build、MXC 版本、probe 摘要、tier（未知则 `unknown`） |
| 策略 | 策略版本/hash、隔离要求、网络模式、授权路径**摘要**、输出限制                                   |
| 过程 | 启动/运行/取消/超时/退出/清理状态、exit code、耗时                                                    |
| 结果 | stdout/stderr 字节数与截断情况、Sidecar 校验、产物数量/大小/hash                                      |
| 安全 | probe/策略不满足、访问被拒、可疑路径、降级原因、清理残留                                              |

**禁止默认写入 Trace**：完整用户代码、完整私有数据、环境密钥、原始授权文件内容；如排障需要，另设经审批、限时的安全日志机制。

关键指标：执行成功率、拒绝率、运行时错误率、超时/取消率、启动开销、清理失败率、残留 ACL/进程数量、产物校验失败率、非法降级次数（应为 0）。

---

## 9. 实施计划与验收标准

### Phase 0：安全可行性门禁（必须先完成）

> 分级说明：**（M = 必须集）** 为安全放行硬条件，未通过不得进入 Phase 1 验收；**（E = 增强集）** 可随首批迭代补齐，不阻塞开发推进，但阻塞“生产候选”结论。

- [ ] （M）冻结执行器版本、hash 和 schema 版本，验证当前开发机与目标生产机差异。
- [ ] （M）根据 `process.env` / `inheritDefaultEnv` 配置环境（必含 `LOCALAPPDATA` presence-only 项），验证 Python、pandas、matplotlib 正常运行；**密钥隔离以 env 全集枚举断言验收**——实测存在少量平台变量渗透（`LSBOX_*`/`PYTHONHOME`），不能仅按变量数量或单一变量判断。
- [ ] （M）测试 control 目录只读、output/sidecar 可写，证明 payload 不能覆盖 wrapper/config（只读语义已在独立环境实测通过：可读不可写，仍须在接入链路上复测）。
- [ ] （M）测试 Sidecar 伪造、缺失、超大、格式错误、提前退出、`os._exit()`；宿主不将其作为可信成功证明。
- [ ] （M）测试符号链接、junction/reparse point、路径穿越、写入未授权位置（未授权**读取**拦截也已实测通过，一并纳入用例）。
- [ ] （M）验证多级子进程的默认策略；无法强制时明确拒绝或保留附加应用层限制。
- [ ] （M）测试 2 路并发；**（E）** 扩展至 5/10 路，检查独立目录、ACL 冲突、数据串扰。
- [ ] （M）测试超时、取消、强杀执行器、异常崩溃；**（E）** 扩展恢复场景与多轮压力，核对进程、目录和 ACL 的前后变化。
- [ ] （M）输出《平台准入与风险记录》，对于不受支持 Windows 10 明确开发实验定位。

**Phase 0 放行条件**：**必须集（M）** 项目全部通过；已知无法强制的限制有明确拒绝策略或可审计的任务范围约束；不存在未解释的权限残留。若不通过，停止扩大 MXC 业务接入。

### Phase 1：Python 最小闭环

- [ ] 新增 `sandbox_backends`、策略解析、MxcBackend、LocalBackend、Trace。
- [ ] 保持 `execute_python` 旧响应字段，完成 `CodeExecutionError` 对齐。
- [ ] 现有 local runner 全量回归通过；不得出现封装调用递归。
- [ ] 原稿的 8 项功能测试在接入后的真实调用链上重新通过。
- [ ] 宿主控制目录、Sidecar 限制和产物 SHA-256 校验正常工作。
- [ ] 前端 5173/8000 真实分析任务 E2E 通过；对确定性基准比对字节，对非确定性任务按预设容差/结构比对。
- [ ] `mxc` 模式不能静默回退；`runtime` 能反映真实后端、tier/unknown 和失败原因。

**Phase 1 放行条件**：功能回归、异常处理及 Phase 0 安全门禁持续通过，且至少具备一个官方受支持目标宿主的验收记录，才能作为生产候选。

> **宿主获取路径（现实约束提示）**：当前开发机为 Windows 10（不在官方支持矩阵），按上述标准无法直接产出“生产候选”结论。推进选项：
> ① 准备一台官方受支持宿主作为验收环境——推荐 Linux 云主机（`bubblewrap` 后端，成本低、不依赖 Windows 11 硬件条件）；
> ② 升级到 Windows 11 24H2+（需硬件满足 TPM 2.0 等要求）；
> ③ 若定位为开发原型/演示用途，明确接受“实验性”标记，生产候选结论留待 ①/② 完成后再给出。

### Phase 2：Terminal 工具

- [ ] **L0 沙箱内路径（§6.4）**：默认信封内的终端命令免审批自动执行；验证 `echo`、目录列举、授权目录写入、越界写/读拒绝、网络拒绝。
- [ ] **L1 升级路径（§6.4）**：越界请求正确触发策略升级审批；批准后仍在沙箱内以放宽策略执行；一次性令牌语义验证（不可复用、不长期放宽）。
- [ ] **L2 直通路径（§6.4）**：沙箱无法完成的操作保留原一次性人工审批与权限消费语义。
- [ ] 支持批准 shell、工作目录和命令边界；Shell 和脚本路径不得被未授权参数替换。
- [ ] 验证子进程树、超时/取消、命令输出截断和清理。
- [ ] 审批界面展示越界原因与策略增量；审计 Trace 覆盖全部升级事件。
- [ ] 端到端验证工具名与响应结构兼容。

### Phase 3：Skill 与长期演进

- [ ] 审计 SkillRuntime 的执行器、资源注入、签名/来源、网络及文件权限。
- [ ] 为 Skill 定义独立权限 Profile，不能继承宽松的 Python/Terminal 配置。
- [ ] 评估 Linux Bubblewrap、持久化容器、多任务调度、CPU/内存配额及更高隔离等级需求（具体候选：hyperlight 微虚机后端，本机 probe 显示可用、未实测，可用于更强隔离与资源约束评估，并对接 §6.4 资源配额缺口）。
- [ ] 如需**可验证执行结果**，设计沙箱外受信任监督器，不在 Sidecar 中增加伪认证字段。

### 9.1 测试矩阵（最小要求）

| 用例                                                        | local  | Windows 10 实验 MXC | 官方受支持宿主 MXC | 预期                                                   |
| ----------------------------------------------------------- | ------ | ------------------- | ------------------ | ------------------------------------------------------ |
| 基础 Python、pandas、绘图                                   | ✓     | ✓                  | ✓                 | 成功并产出合法文件                                     |
| 环境注入 / `LOCALAPPDATA` presence-only / `TEMP` 重定向 | 不适用 | ✓                  | ✓                 | 自定义变量注入成功；密钥全集断言；缓存路径符合平台行为 |
| 数据只读 / 输出可写                                         | 回归   | ✓                  | ✓                 | 非授权写入失败                                         |
| 控制文件覆盖尝试                                            | 不适用 | ✓                  | ✓                 | wrapper/config 不可修改                                |
| Sidecar 伪造/缺失/过大                                      | 不适用 | ✓                  | ✓                 | 不被视为可信结果；格式异常明确报错                     |
| 密钥读取/出站联网                                           | 回归   | ✓                  | ✓                 | 按策略被拒绝                                           |
| 子进程树、超时、取消                                        | 回归   | ✓                  | ✓                 | 策略能力准确、无遗留进程                               |
| 并发、崩溃、ACL 清理                                        | 不适用 | ✓                  | ✓                 | 无跨任务污染和额外永久授权                             |
| 执行器缺失/损坏/策略不匹配                                  | ✓     | ✓                  | ✓                 | 按路由矩阵 fail-closed                                 |
| 文件链接/路径穿越/超量产物                                  | ✓     | ✓                  | ✓                 | 拒绝越界、完整审计                                     |

---

## 10. 风险登记与待决问题

| 编号 | 事项                                                  | 级别 | 责任方          | 关闭标准                                                                                                                         |
| ---- | ----------------------------------------------------- | ---- | --------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| R1   | Windows 10 `appcontainer-dacl` 不在目标官方支持矩阵 | P0   | 平台/运维       | 只用于开发或迁移到受支持宿主                                                                                                     |
| R2   | 控制文件可写导致篡改                                  | P0   | Sandbox         | 分离权限 + 越权测试通过                                                                                                          |
| R3   | Sidecar 缺乏强真实性                                  | P0   | Sandbox/Agent   | 明确不可信语义；宿主不依赖 `ok` 作安全判断                                                                                     |
| R4   | 实际隔离 tier、支持的能力无法确认                     | P0   | Sandbox/平台    | 记录已验证能力；不满足要求时拒绝执行                                                                                             |
| R5   | 子进程限制与现有 runner 不一致                        | P1   | Sandbox         | 明确策略能否强制，测试通过或显式限制任务                                                                                         |
| R6   | Windows DACL 并发更新/恢复                            | P0   | 平台/测试       | 并发/强杀/恢复验证，无权限残留                                                                                                   |
| R7   | 环境变量/解释器兼容                                   | P1   | Sandbox         | pandas、matplotlib、venv 回归通过；`LOCALAPPDATA` presence-only 与 `TEMP` 平台重定向行为纳入用例；密钥隔离以全集枚举断言验收 |
| R8   | 输出洪泛和资源耗尽                                    | P1   | Sandbox         | 流式限流、任务取消和资源约束验收                                                                                                 |
| R9   | `ui.disable=false` 扩大 UI 面                       | P1   | 平台/安全       | 明确仅需该配置的宿主、实际影响与替代方案                                                                                         |
| R10  | 供应链/执行器二进制完整性                             | P1   | 运维            | 固定版本和 hash，可重复部署                                                                                                      |
| R11  | `auto` 模式安全降级                                 | P0   | BackendSelector | 不可信任务零静默降级                                                                                                             |
| R12  | Windows/Linux 策略能力差异                            | P2   | 平台            | 各平台独立安全验收、配置模板                                                                                                     |

---

## 附录 A：实机验证记录（2026-10-14，基线 8 项 + 补充 6 项）

环境：**Windows 10 19045.6466**；测试文件在 `%TEMP%\mxc-probe\`（`hello.json` / `fs-test.json` / `py-test.json` / `full-test.json` / `env-test.json` / `data\task.py` / `data\env_task.py`），可直接复用为 Phase 0 门禁用例。

| #  | 测试                                                            | 结果                                                                                                                                               |
| -- | --------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1  | `wxc-exec.exe --probe`                                        | `tier=appcontainer-dacl`；`hyperlightAvailable: true`                                                                                          |
| 2  | `cmd /c echo`                                                 | 正常输出                                                                                                                                           |
| 3  | `readonlyPaths`                                               | 授权文件可读；**补充复核只读严格语义：可读不可写**（`RO_READ` 成功 / `RO_WRITE` 抛 `PermissionError`）                                 |
| 4  | `readwritePaths`                                              | 产物落盘，宿主侧内容逐字节核对正确                                                                                                                 |
| 5  | 未授权目录写入                                                  | 拒绝访问（“拒绝访问。”），宿主未生成文件                                                                                                         |
| 6  | `.venv` Python                                                | 配合 base 解释器只读授权与 `ui.disable=false`，Python 3.11.15 正常运行                                                                           |
| 7  | pandas 数据流                                                   | 读取 CSV、计算并写入产物                                                                                                                           |
| 8  | `network.egress.default=deny`                                 | 联网失败，观察到 `PermissionError`                                                                                                               |
| 9  | `process.env` / `inheritDefaultEnv` / `cwd` / `timeout` | 字段名经 SDK schema（`wire.d.ts`）确认且实机生效：自定义变量注入成功、`cwd` 生效、宿主变量未泄露（`USERPROFILE` 不可见）、`env_count = 11` |
| 10 | `LOCALAPPDATA` 约束                                           | 缺失时执行器以 `policy_validation` 拒绝启动（presence-only 必需项）；补全后正常                                                                  |
| 11 | `TEMP`/`TMP` 行为                                           | 平台覆盖手动值，重定向到容器私有目录 `%LOCALAPPDATA%\Packages\<containerId>\AC\Temp`；`MPLCONFIGDIR` 手动值有效                                |
| 12 | 未授权目录读取                                                  | `PermissionError` 拦截（授权模型对读、写同时生效）                                                                                               |
| 13 | env 渗透观察                                                    | `inheritDefaultEnv: false` 下实测仍出现少量平台变量（`LSBOX_*`、`PYTHONHOME`），密钥隔离须全集枚举断言                                       |
| 14 | Sidecar 不可信定位                                              | 逻辑复核（同解释器内无法提供可信证明），列为 Phase 0 门禁用例                                                                                      |

**使用规则（V2.1 更新）**：栏目 1–8 为已复现基线；9–14 为本次补充复核（9–13 为实测，14 为逻辑复核）。以上结果仍不可代替 Phase 0 的安全门禁，也不可作为 Windows 10 官方支持证据。

## 附录 B：原有关键源码索引（2026-10-14 已核对，路径与行号与项目一致）

| 主题          | 文件                                                         |
| ------------- | ------------------------------------------------------------ |
| 审批闸门      | `data_analysis/runtime/harness.py`、`server/main.py`     |
| 注册/路由     | `data_analysis/tools/registry.py`                          |
| Python 工具   | `data_analysis/tools/code_tools.py`                        |
| Python runner | `data_analysis/runtime/_python_code_runner.py`             |
| Terminal 工具 | `data_analysis/tools/terminal_tools.py`                    |
| 执行上下文    | `data_analysis/runtime/execution_context.py`               |
| 子进程执行    | `data_analysis/runtime/sandbox.py`、`_sandbox_worker.py` |

## 附录 C：官方参考资料（本次核对）

- MXC 官方仓库：[microsoft/mxc](https://github.com/microsoft/mxc)
- MXC 稳定 JSON Schema、`process.env`、`process.inheritDefaultEnv`、containment 说明：[docs/schema.md](https://github.com/microsoft/mxc/blob/main/docs/schema.md)
- Windows 后端版本要求：[OS-version support](https://github.com/microsoft/mxc/blob/main/docs/backends/process-container/os-version-support.md)
- SDK 与非 SDK 入口：[README.md](https://github.com/microsoft/mxc/blob/main/README.md)
- 原生 host prep 操作及其影响：[docs/host-prep.md](https://github.com/microsoft/mxc/blob/main/docs/host-prep.md)
- 访问拒绝诊断及审计模式风险：[docs/logging-access-denied.md](https://github.com/microsoft/mxc/blob/main/docs/logging-access-denied.md)
- 测试脚本索引：[tests/scripts/README.md](https://github.com/microsoft/mxc/blob/main/tests/scripts/README.md)

---

## 最终实施决策

**保留 V1 的核心架构、最小侵入改造路线、`wxc-exec.exe` 直接调用方案和分阶段实施。**

**先落实 Phase 0 的安全门禁，再做 Phase 1 Python 接入；不要把“能运行”和“隔离可靠”混为一谈。** 未经过源码审查、系统能力验收和对抗/异常测试的 Windows 10 方案，定位为本地开发原型，而非可直接对外承诺的生产沙箱。
