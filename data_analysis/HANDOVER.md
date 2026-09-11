# DAAgent — 交接文档

> 本文档总结了从项目理解到新智能体架构设计的完整讨论过程，供后续开发参考。

---

## 1. 现有项目理解

### 1.1 项目概况

**MMAgent** 是一个数学建模智能体平台，位于 `D:\MMAgent`。核心源码目录为 `scr/`（注意不是 `src/`）。

技术栈：LangGraph 编排、Pydantic 结构化、OpenAI 兼容 LLM、FastAPI 服务层。

### 1.2 scr/ 模块结构

```
scr/
├── math_modeling_agent/   # 入口与编排（graph.py, state.py, llm.py, main.py）
├── agents/                # LLM 智能体层（base.py, problem_analyst, question_solver 等）
├── workflow/              # 工作流节点（intake, project_context, question_loop）
├── gates/                 # 质量门（G0 输入门, GQ 小问门, GF 交付门）
├── schemas/               # Pydantic 数据模型（context, question, paper, evidence 等）
├── tools/                 # 工具层（code_executor, tavily_search, md2docx 等）
├── runtime/               # 运行时（budget, artifacts, checkpoint, logging）
├── prompts/               # LLM 提示词模板
└── templates/             # 报告模板
```

### 1.3 核心流程（LangGraph 主图）

```
intake → context → [G0 质量门]
  → select_question → assemble_context → solve_question → validate_result → [GQ 质量门] → archive
  （循环逐问）
  → configure_delivery_budget → global_review → write_paper → review_paper → [GF 质量门] → deliver
```

### 1.4 关键设计

- **状态分区**：全局只读上下文 → 当前小问可写 → 已完成结果库；后续小问只继承 `reusable_summary`
- **节点异常降级**：任何节点抛错不终止整题运行，`solve_question` 失败自动标记 `blocked`
- **LLM-only 策略**：不预设方法目录，由 LLM + 联网搜索（Tavily）自行选型
- **确定性验证**：`result_validator` 和 `reviewer` 不依赖 LLM，用规则检查
- **预算控制**：验证迭代、代码修复、报告修订等有预算上限（`runtime/budget.py`）
- **代码沙箱**：LLM 生成的 Python 代码在独立子进程执行（`tools/code_executor.py`），超时强制终止

### 1.5 架构定性结论

**现有 scr/ 是一个确定性建模工作流（workflow），不是 agent。**

判断依据：
- 控制流是硬编码的图拓扑（静态边声明），LLM 无法决定"下一步做什么"
- `*Agent` 类只是"LLM 函数"——输入 prompt 输出结构化数据，没有自主循环
- 质量门（G0/GQ/GF）是确定性 if-else 规则，不是 LLM 判断
- 唯一的"自主性"在 `model_builder` 内部（LLM 生成代码→执行→自评→修复），但是局部的

定位：属于 Andrew Ng 说的 **"agentic workflow"**（工作流里嵌了 LLM），不是 **"autonomous agent"**（LLM 驱动工作流）。这个设计对数学建模任务是合理的——需要可追溯、可复现。

---

## 2. 新项目定位与设计

### 2.1 目标

在 `data_analysis/` 下构建面向**产品经理、数据分析师**的自主数据分析智能体。底层复用现有 `scr/` 建模工作流作为能力工具，上层构建真正的 Agent 决策循环。

### 2.2 核心设计原则

1. **现有代码零侵入**：`scr/` 建模工作流保持独立，通过工具接口调用
2. **Agent 自主决策**：控制流由 LLM 驱动，不预定义固定流程
3. **分层解耦**：每层可独立替换、独立测试、独立扩展

### 2.3 推荐方案

**Agent 包装现有工作流（方案 A）**：不改造现有代码，把 `run_graph()` 包装成一个工具。Agent 不知道 G0/GQ/GF 的存在，只知道"调这个工具能得到建模结论和报告"。

被否决的方案：
- B. 把建模工作流拆成细粒度工具 → 质量门逻辑难拆干净
- C. 完全重写 → 丢掉现有建模能力，成本极高

---

## 3. 架构设计

### 3.1 分层全景

```
┌──────────────────────────────────────────────────┐
│                     产品层 (Product)               │
│   对话 UI · 任务面板 · 产物预览 · 权限/多租户 · 计费   │
├──────────────────────────────────────────────────┤
│                     编排层 (Orchestration)          │
│   Agent 决策循环 · 工具路由 · 多 Agent 协作           │
├──────────────────────────────────────────────────┤
│                     能力层 (Capabilities)           │
│   工具集 · LLM 调用 · RAG 检索 · 代码执行 · 文件处理   │
├──────────────────────────────────────────────────┤
│                     运行时 (Runtime)                │
│   会话管理 · 状态持久化 · 沙箱 · 流式输出 · 中断/恢复   │
├──────────────────────────────────────────────────┤
│                     基础设施 (Infra)                │
│   模型网关 · 向量存储 · 对象存储 · 队列 · 日志/监控     │
└──────────────────────────────────────────────────┘
```

### 3.2 Loop / ReAct / Harness 三者关系（重点澄清）

这是最容易混淆的概念，明确如下：

| 概念 | 位置 | 职责 | 一句话 |
|---|---|---|---|
| **Loop** | `agent/graph.py` | while 外层循环，驱动整个流程直到结束 | "反复做，直到完成" |
| **ReAct** | `agent/loop.py` | 每轮内部的思考策略：Thought → Action → Observation | "这一轮怎么想" |
| **Harness** | `runtime/harness.py` | 安全执行 Action：权限、超时、沙箱、错误捕获、trace | "安全地把事做了" |

```
Loop（外层循环）          "反复做，直到完成"
  │
  ├─ 每一轮用 ReAct 模式   "这一轮怎么想：想→做→看"
  │   ├─ Thought: LLM 推理当前情况，决定下一步
  │   ├─ Action:  选出工具和参数
  │   └─ Observation: 拿到执行结果
  │
  └─ Action 交给 Harness   "安全地把这件事做了"
      ├─ 权限、超时、沙箱、错误捕获
      └─ 返回结果给 Loop，进入下一轮
```

驱动循环伪代码：

```python
while not done:
    action = loop.step(state)              # agent/loop.py 决策（ReAct）
    if action.is_finish:
        break
    observation = harness.execute(action)  # runtime/harness.py 执行
    state.append(observation)              # 更新状态，进入下一轮
```

**为什么必须分开**：
- 换沙箱方案只改 `harness.py`，决策逻辑不动
- 换模型/prompt 策略只改 `loop.py`，执行代码不动
- 测试决策逻辑只需 mock LLM；测试执行逻辑只需喂假 Action

**ReAct 不是独立模块**，是 loop 里的一种实现策略。后续可替换为 Plan-and-Execute 等其他策略，外层 Loop 结构不变。

### 3.3 具体交互示例

用户说：*"帮我看看这份销售数据里哪个产品线利润最高"*

```
第 1 轮
  Loop:   思考"需要先加载数据" → Action("load_data", {"file": "sales.csv"})
  Harness: 权限检查 ✓ → 沙箱里 read_csv → 超时30s → 返回数据画像

第 2 轮
  Loop:   看到数据画像 → 思考"需要按产品线聚合利润" → Action("run_code", {...})
  Harness: 危险操作检查 ✓ → 子进程执行 → 超时60s → 返回 "A产品线利润最高32%"

第 3 轮
  Loop:   结论已有 → Finish("A产品线利润最高，占比32%...")
```

---

## 4. 目录结构

```
data_analysis/
├── ARCHITECTURE.md          # 架构说明文档
│
├── agent/                   # Agent 决策编排层（大脑）
│   ├── graph.py             #   驱动循环：串联 loop（决策）与 harness（执行）
│   ├── loop.py              #   决策循环：LLM 推理 → 输出 Action（不执行）
│   ├── state.py             #   Agent 状态：对话历史 + 工具结果 + 项目上下文
│   ├── planner.py           #   复杂任务分解：将大问题拆成子任务 DAG
│   ├── prompts/             #   Agent System Prompt 与工具描述模板
│   │   ├── system.md        #     系统提示词（角色、能力边界、行为准则）
│   │   ├── tool_descriptions.md
│   │   └── reflection.md    #     自我反思提示词
│   └── tools/               #   工具定义（schema + 描述，不含执行逻辑）
│       ├── registry.py      #     工具注册与发现机制
│       ├── data_tools.py    #     数据加载、画像、探索、清洗
│       ├── query_tools.py   #     数据筛选、聚合、SQL 查询
│       ├── code_tools.py    #     执行 Python 分析代码
│       ├── viz_tools.py     #     图表生成
│       ├── modeling_tool.py #     包装 scr/math_modeling_agent
│       └── export_tools.py  #     导出报告、Excel、幻灯片
│
├── runtime/                 # Agent 运行时（身体）
│   ├── harness.py           #   执行框架：Action → Observation
│   ├── session.py           #   会话管理：多轮对话状态、上下文压缩
│   ├── persistence.py       #   状态持久化：中断/恢复
│   ├── streaming.py         #   流式输出
│   ├── interrupt.py         #   中断/恢复
│   ├── queue.py             #   任务队列：并发调度
│   ├── sandbox.py           #   沙箱执行
│   └── config.py            #   运行时配置
│
├── gateway/                 # 模型网关层
│   ├── router.py            #   模型路由
│   ├── fallback.py          #   降级策略
│   ├── cache.py             #   Prompt 缓存
│   ├── ratelimit.py         #   限流
│   └── metering.py          #   计量/计费
│
├── memory/                  # 记忆系统
│   ├── working.py           #   工作记忆：当前任务中间状态
│   ├── short_term.py        #   短期记忆：对话历史
│   ├── long_term.py         #   长期记忆：项目/领域知识（向量检索）
│   └── episodic.py          #   情景记忆：历史执行日志
│
├── observability/           # 可观测性
│   ├── trace.py             #   分布式追踪
│   ├── metrics.py           #   指标采集
│   ├── logging.py           #   结构化日志
│   └── dashboard.py         #   面板接口
│
├── security/                # 安全与权限
│   ├── input_filter.py      #   prompt injection 检测
│   ├── output_guard.py      #   输出审查
│   ├── permissions.py       #   工具级 RBAC
│   └── audit.py             #   操作审计
│
├── evaluation/              # 评测与质量
│   ├── test_cases/          #   固定测试集
│   ├── runner.py            #   回归测试
│   ├── feedback.py          #   在线反馈
│   └── trajectory_analyzer.py
│
├── server/                  # API 服务层
│   ├── app.py               #   FastAPI 入口
│   ├── routes_chat.py       #   对话接口
│   ├── routes_files.py      #   文件接口
│   └── routes_admin.py      #   管理接口
│
└── web/                     # 前端界面
    ├── chat/                #   对话界面
    ├── dashboard/           #   任务面板
    └── shared/              #   共享组件
```

---

## 5. 与现有 scr/ 的复用映射

| 现有模块 | 在新项目中的角色 | 改动 |
|---|---|---|
| `scr/math_modeling_agent/` | `modeling_tool.py` 的后端，被 Agent 按需调用 | 无 |
| `scr/tools/code_executor.py` | 被 `code_tools.py` 和 `sandbox.py` 复用 | 无 |
| `scr/workflow/intake.py` | 被 `data_tools.py` 的数据画像复用 | 无 |
| `scr/runtime/budget.py` | 被 `gateway/metering.py` 参考 | 无 |
| `scr/tools/md2docx.py` | 被 `export_tools.py` 复用 | 无 |
| `KnowledgeBase/` | RAG 检索能力，可作为 agent 的 `search_knowledge` 工具 | 无 |

---

## 6. 成熟 Agent 产品的完整能力清单

### 6.1 Runtime（最容易被低估）

| 能力 | 缺了会怎样 |
|---|---|
| 会话管理 | 长对话丢上下文、token 爆炸 |
| 状态持久化 | 长任务崩溃就全丢 |
| 流式输出 | 用户盯着空白页等 30 秒 |
| 中断/恢复 | 长任务不可控 |
| 超时与重试 | 一个 API 抖动卡死整个任务 |
| 并发控制 | 多用户互相抢锁 |
| 沙箱执行 | 代码出错污染宿主环境 |

### 6.2 模型网关

路由（按复杂度选模型）、降级（超时切备用）、缓存（相同 prompt 命中）、限流（QPS 控制）、计量（token 归属计费）、审计（调用可追溯）。

### 6.3 工具系统

注册表（装饰器注册 + schema）、动态发现（按任务上下文显示不同工具子集）、权限控制、执行沙箱、结果缓存。

### 6.4 记忆系统

| 层次 | 用途 |
|---|---|
| 工作记忆 | 当前任务中间状态 |
| 短期记忆 | 当前对话历史 |
| 长期记忆 | 用户过往项目、领域知识 |
| 情景记忆 | "上次类似分析的效果" |

### 6.5 可观测性

每次 LLM 调用的 prompt/response/token/耗时、每个工具的输入/输出/错误、Agent 每步决策理由。没有这些无法回答"为什么选错了工具"。

### 6.6 安全与权限

输入过滤（prompt injection）、输出审查（防泄露）、数据隔离（多租户）、操作审计、人工确认（高风险操作）。

### 6.7 评测与质量

固定测试集、在线反馈（点赞/点踩）、轨迹分析（找失败模式）、A/B 测试。

---

## 7. RAG Agent 概念

**RAG = Retrieval-Augmented Generation**：回答前先从外部知识库检索相关内容，塞进 prompt 让 LLM 基于内容回答。

**RAG Agent = RAG + Agent 自主决策**：区别在于 LLM 自主决定要不要检索、怎么检索、检索几次。

| | 纯 RAG | RAG Agent |
|---|---|---|
| 流程 | 固定：检索→生成 | 动态：LLM 决定检索策略 |
| 检索次数 | 一次 | 可多次、可改写查询 |
| 工具 | 只有检索 | 检索 + 计算 + 可视化等 |

在新项目中，RAG 作为 agent 的一个工具（`search_knowledge`），复用 `KnowledgeBase/` 的混合检索能力（稠密 BGE + 稀疏 BM25 + RRF 融合）。

---

## 8. 建设优先级

### 第一阶段 — 能用（MVP）
1. `agent/loop.py` + `agent/graph.py` — 决策循环与驱动
2. `runtime/harness.py` — 执行框架（超时、错误捕获、日志）
3. `agent/tools/` — 工具注册表 + 数据加载、探索、可视化、建模包装
4. `runtime/session.py` + `runtime/persistence.py` — 会话与状态
5. `runtime/streaming.py` — 流式输出
6. `server/` — 基础对话 API

### 第二阶段 — 好用
7. `gateway/` — 模型路由、降级、限流
8. `observability/` — 完整 trace 与统计
9. `memory/` — 对话记忆 + 项目上下文
10. `evaluation/` — 固定测试集 + 回归

### 第三阶段 — 可运营
11. `security/` — 多租户、权限、审计
12. `gateway/metering.py` — 计费与配额
13. `evaluation/feedback.py` — 在线反馈闭环
14. `agent/planner.py` — 多 Agent 协作

---

## 9. 关键决策记录

| 决策 | 选择 | 理由 |
|---|---|---|
| 新旧代码关系 | Agent 包装现有工作流 | 零侵入，建模能力立即可用 |
| Loop vs Harness | 分开放在 agent/ 和 runtime/ | 独立测试、独立演进 |
| ReAct 定位 | loop.py 内的策略，非独立模块 | 可替换为其他策略 |
| 现有 scr/ 定性 | 确定性工作流，不是 agent | 控制流硬编码，LLM 无自主决策权 |
| 目标用户 | 产品经理、数据分析师 | 决定工具粒度和交互形态 |
