# 数据分析 Agent 专项能力实现方案

## 1. 总体架构

在现有 `Agent Loop + Harness + Tool Registry + Python Sandbox + RAG` 架构上增加数据分析专项层，不改动通用 Agent 底座。

```text
User Query
   ↓
Question Compiler
   ↓
AnalysisSpec
   ↓
Data Semantic Profiler
   ↓
Knowledge Context
   ↓
Analysis Planner
   ↓
Agent Loop
   ↓
Harness / Python / SQL / File
   ↓
Structured Observation
   ↓
Validation & Evidence
   ↓
Report
```

---

## 2. Question Compiler

将用户自然语言问题转换为结构化分析任务 `AnalysisSpec`。

```python
class AnalysisSpec(BaseModel):
    business_question: str
    analysis_type: str
    target_metrics: list[str]
    dimensions: list[str]
    time_range: dict | None
    baseline: dict | None
    filters: list[dict]
    candidate_factors: list[str]
    required_evidence: list[str]
```

示例：

```json
{
  "business_question": "为什么9月GMV下降",
  "analysis_type": "root_cause_analysis",
  "target_metrics": ["GMV"],
  "dimensions": ["channel", "region", "user_type"],
  "candidate_factors": ["traffic", "conversion_rate", "aov"],
  "required_evidence": ["trend", "comparison", "contribution"]
}
```

执行流程：

```text
用户问题
→ LLM结构化解析
→ Pydantic校验
→ 缺少关键条件则进入G0澄清
→ 生成AnalysisSpec
```

---

## 3. Data Semantic Profiler

读取数据后生成统一的数据语义画像 `DataProfile`。

```python
class ColumnProfile(BaseModel):
    name: str
    dtype: str
    semantic_type: str
    business_role: str | None
    missing_rate: float
    unique_count: int

class DataProfile(BaseModel):
    dataset_name: str
    grain: str | None
    primary_keys: list[str]
    time_columns: list[str]
    metrics: list[str]
    dimensions: list[str]
    columns: list[ColumnProfile]
    quality_issues: list[str]
```

识别内容：

```text
主键
时间字段
指标字段
维度字段
ID字段
类别字段
数据粒度
时间频率
缺失值
重复值
异常值
潜在表关系
```

执行方式：

```text
profile_data
→ pandas统计
→ 规则识别
→ LLM补充业务语义判断
→ DataProfile
```

---

## 4. Knowledge Context

分析开始前，根据 `AnalysisSpec` 自动检索知识库中的业务信息。

```python
class AnalysisContext(BaseModel):
    metric_definitions: list
    business_rules: list
    data_dictionary: list
    historical_findings: list
    domain_constraints: list
```

流程：

```text
AnalysisSpec
→ 生成检索Query
→ RAG检索
→ 结果压缩与去重
→ AnalysisContext
```

重点检索：

```text
指标口径
字段说明
业务规则
历史分析结论
相关业务文档
```

---

## 5. Analysis Planner

由 LLM 根据：

```text
AnalysisSpec
+
DataProfile
+
AnalysisContext
+
历史Observation
```

动态生成分析计划。

```python
class AnalysisStep(BaseModel):
    id: str
    objective: str
    hypothesis: str | None
    method: str
    required_data: list[str]
    depends_on: list[str]
    validation_rules: list[str]
    status: str

class AnalysisPlan(BaseModel):
    objective: str
    steps: list[AnalysisStep]
```

示例：

```text
目标：分析9月GMV下降原因

Step 1：确认GMV整体趋势
Step 2：拆解流量、转化率、客单价变化
Step 3：按渠道分析贡献
Step 4：按用户类型继续下钻
Step 5：验证主要异常维度
Step 6：形成原因排序和证据链
```

Planner 不固定分析流程。

执行过程中允许：

```text
新增分析步骤
删除无效步骤
修改假设
增加下钻维度
调整统计方法
提前终止无价值方向
```

---


## 6. Plan-and-Execute + ReAct 核心架构

采用单 Agent 的 **Plan-and-Execute + ReAct** 混合架构：

```text
User Query
    ↓
Question Compiler
    ↓
Initial Planner
    ↓
Todo List
    ↓
Todo Scheduler
    ↓
Current Todo
    ↓
┌──────────────────────┐
│       ReAct Loop     │
│                      │
│ Thought              │
│   ↓                  │
│ Action               │
│   ↓                  │
│ Harness / Tool       │
│   ↓                  │
│ Observation          │
│   ↓                  │
│ Thought ...          │
└──────────┬───────────┘
           ↓
      Finish Todo
           ↓
      Plan Review
   ┌───────┼────────┐
   ↓       ↓        ↓
  Keep    Modify    Add
           ↓
       Next Todo
           ↓
      Final Review
           ↓
         Report
```

核心逻辑：

```text
Plan-and-Execute
负责：
先规划整体分析任务
→ 生成Todo List
→ 管理任务顺序和依赖
→ 根据分析结果动态调整计划

ReAct
负责：
执行当前Todo
→ Thought
→ Action
→ Observation
→ Thought
→ 持续执行直到当前Todo完成
```

Todo 使用结构化状态：

```python
class TodoItem(BaseModel):
    id: str
    objective: str
    status: str
    depends_on: list[str]
    expected_outputs: list[str]
    result_summary: str | None
```

建议状态：

```text
pending
in_progress
completed
failed
skipped
```

执行过程中允许重新规划：

```text
新增Todo
修改Todo
跳过Todo
调整执行顺序
增加下钻任务
```

整体执行循环：

```python
create_plan()

while has_unfinished_todo():
    todo = select_next_todo()

    while not todo_finished:
        decision = react_loop.step(state)

        if decision.type == "action":
            observation = harness.execute(decision)
            state.record_observation(observation)

        elif decision.type == "finish_todo":
            complete_todo(todo)
            break

    review_plan()

final_review()
generate_report()
```

该架构继续复用现有：

```text
Agent Loop
Harness
Tool Registry
Subprocess Sandbox
execute_python
Observation
SSE
```

新增核心模块：

```text
Planner
Todo Manager
Todo Scheduler
Plan Reviewer
```


## 7. Structured Observation

避免把 Python stdout、完整 DataFrame 和大段工具结果持续注入上下文。

定义统一的数据分析 Observation：

```python
class AnalysisObservation(BaseModel):
    type: str
    step_id: str
    summary: str
    metrics: dict
    evidence_refs: list[str]
    artifacts: list[str]
    validation_status: str | None
```

Observation 类型：

```text
DataProfileObservation
MetricObservation
StatisticalObservation
DataQualityObservation
ChartObservation
HypothesisObservation
ValidationObservation
ArtifactObservation
```

执行方式：

```text
Tool Raw Result
→ Result Parser
→ 提取关键数字
→ 生成摘要
→ 原始结果落盘
→ 返回Structured Observation
```

例如：

```json
{
  "type": "MetricObservation",
  "step_id": "step_03",
  "summary": "渠道A GMV同比下降18.4%，贡献整体降幅的41.2%",
  "metrics": {
    "gmv_change": -0.184,
    "contribution": 0.412
  },
  "evidence_refs": [
    "artifact://channel_decomposition.csv"
  ],
  "artifacts": [
    "channel_decomposition.csv",
    "channel_gmv.png"
  ],
  "validation_status": "passed"
}
```

---

## 8. Validation Engine

每个关键分析结果进入统一验证流程。

### 8.1 数据验证

```text
缺失值
重复值
异常值
数据类型
时间完整性
样本量
数据粒度
主键唯一性
```

### 8.2 方法验证

```text
统计方法适用条件
样本量是否合理
分组是否可比
异常值是否影响结果
回归共线性
时间序列数据泄漏
显著性检验合理性
```

### 8.3 结论验证

检查：

```text
结论是否有数据支持
数字是否能够复算
结论是否超出证据范围
相关性是否被错误解释为因果
不同分析结果是否矛盾
```

输出：

```python
class ValidationResult(BaseModel):
    target_id: str
    status: str
    checks: list
    risks: list[str]
    recommendation: str | None
```

---

## 9. Evidence Tracking

建立完整证据链：

```text
Claim
↓
Evidence
↓
Computation
↓
Artifact
↓
Source Data
```

数据结构：

```python
class Evidence(BaseModel):
    claim_id: str
    claim: str
    evidence_type: str
    source_artifact: str
    computation_step: str
    supporting_metrics: dict
    source_data: list[str]
    validation_status: str
```

例如：

```text
Claim:
渠道A是本月GMV下降的主要来源

Evidence:
渠道A GMV下降18.4%
贡献整体GMV降幅41.2%

Computation:
step_03

Artifact:
channel_decomposition.csv

Source:
orders.parquet
```

最终报告中的关键结论必须关联 Evidence。

---

## 10. Hypothesis 管理

复杂分析采用假设驱动方式。

```python
class Hypothesis(BaseModel):
    id: str
    statement: str
    status: str
    supporting_evidence: list[str]
    contradicting_evidence: list[str]
```

状态：

```text
pending
testing
supported
rejected
uncertain
```

执行方式：

```text
Planner提出假设
→ Agent执行分析
→ Observation
→ Validation
→ 更新Hypothesis
→ 决定是否继续下钻
```

例如：

```text
H1：GMV下降主要由流量下降导致
H2：转化率下降导致GMV下降
H3：客单价下降导致GMV下降

验证H1
→ 不成立

验证H2
→ 成立

→ 继续分析哪些渠道转化率下降
```

---

## 11. AgentState 扩展

在现有状态模型上增加：

```python
class AgentState:
    user_query: str

    analysis_spec: AnalysisSpec | None
    data_profile: DataProfile | None
    analysis_context: AnalysisContext | None
    analysis_plan: AnalysisPlan | None

    hypotheses: list[Hypothesis]
    observations: list[AnalysisObservation]
    evidence: list[Evidence]

    iteration: int
```

每轮 LLM 输入改为：

```text
System Prompt
+
Rolling Memory
+
AnalysisSpec
+
DataProfile摘要
+
AnalysisContext摘要
+
AnalysisPlan当前状态
+
Hypothesis状态
+
最近有效Observations
+
Evidence摘要
```

避免反复注入全部工具原始输出。

---

## 12. 分析产物管理

建议每个任务建立独立运行目录：

```text
runs/
└── {run_id}/
    ├── state.json
    ├── analysis_spec.json
    ├── data_profile.json
    ├── analysis_plan.json
    │
    ├── code/
    ├── data/
    ├── tables/
    ├── charts/
    ├── evidence/
    │
    ├── validation.json
    └── report.md
```

LLM 上下文只保存摘要和引用。

完整结果通过 artifact 路径按需读取。

---

## 13. 推荐目录结构

```text
data_analysis/
├── agent/
│   ├── loop.py
│   ├── graph.py
│   └── state.py
│
├── intelligence/
│   ├── question_compiler.py
│   ├── semantic_profiler.py
│   ├── planner.py
│   ├── hypothesis.py
│   ├── result_parser.py
│   ├── validator.py
│   └── evidence.py
│
├── models/
│   ├── analysis_spec.py
│   ├── data_profile.py
│   ├── analysis_context.py
│   ├── analysis_plan.py
│   ├── hypothesis.py
│   ├── observation.py
│   ├── validation.py
│   └── evidence.py
│
├── tools/
├── runtime/
├── context/
└── artifacts/
```

---

## 14. 实施顺序

### Phase 1：分析输入标准化

实现：

```text
AnalysisSpec
DataProfile
```

目标：

```text
明确用户要分析什么
明确当前数据是什么
```

### Phase 2：分析规划

实现：

```text
AnalysisContext
AnalysisPlanner
AnalysisPlan
Hypothesis
```

目标：

```text
让LLM先规划，再执行分析
```

### Phase 3：执行结果结构化

实现：

```text
Result Parser
Structured Observation
Artifact Manager
```

目标：

```text
原始结果落盘
关键结果进入上下文
```

### Phase 4：可信分析

实现：

```text
Validation Engine
Evidence Tracking
```

目标：

```text
分析结果可验证
关键结论可追溯
```

### Phase 5：知识闭环

实现：

```text
知识库 → AnalysisContext
Report / Evidence → 知识库
```

最终执行链路：

```text
Business Question
→ AnalysisSpec
→ DataProfile
→ AnalysisContext
→ AnalysisPlan
→ Hypothesis
→ Agent Execution
→ Structured Observation
→ Validation
→ Evidence
→ Report
```
