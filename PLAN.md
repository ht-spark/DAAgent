# RAG Agent — 开发计划

> 基于 `ARCHITECTURE.md` 架构设计，按 compose-next 工作流拆分为多个独立可交付的功能块。

## 总览

```
F1: rag/ 检索管线（地基）
 └→ F2: agent/ 决策循环 + harness（大脑+双手）
      └→ F3: runtime/ 会话与流式输出（能对话）
           └→ F4: server/ API 服务（能访问）
                ├→ F5: 多跳推理 + 查询改写（更聪明）
                ├→ F6: 数据分析/建模工具扩展（更全能）
                └→ F7: 可观测性 + 评测（可运营）
```

每个 Feature 走一次完整的 compose-next 流程：Grill → Workspace → Spec → Implement → Verify → Review → Finalize → Finish。

---

## F1 — RAG 检索管线

**目标**：从 `KnowledgeBase/` 抽取检索能力，封装为独立的 `rag/` 模块。

**交付物**：

- `rag/schemas.py` — Document, Chunk, RetrievalResult, Citation 数据模型
- `rag/ingest/loader.py` — 文档加载（PDF/DOCX/PPTX/MD/TXT）
- `rag/ingest/parser.py` — MinerU 转 Markdown
- `rag/ingest/chunker.py` — 分块策略
- `rag/ingest/pipeline.py` — 摄入编排
- `rag/index/embedding.py` — BGE 嵌入模型封装
- `rag/index/vector_store.py` — Qdrant 读写
- `rag/index/index_manager.py` — 索引生命周期管理
- `rag/retrieve/dense.py` — 稠密检索
- `rag/retrieve/sparse.py` — BM25 稀疏检索
- `rag/retrieve/hybrid.py` — 混合检索 + RRF 融合
- `rag/retrieve/reranker.py` — Cross-encoder 重排

**验收标准**：

- 给定一组文档，完成 摄入→分块→嵌入→入库 全流程
- 给定查询，返回 Top-K 带引用的检索结果
- 混合检索优于单一稠密或稀疏检索

**Grill 待决策**：

- [ ] 复用 KnowledgeBase 的程度：直接 import vs 复制代码独立封装
- [ ] 嵌入模型：BGE 中文模型 vs 多语言模型 vs 可配置
- [ ] 分块策略默认值：chunk_size、overlap、按标题分块 vs 递归分块
- [ ] 向量存储：复用现有 Qdrant 实例 vs 新建独立 collection
- [ ] MinerU 是否必须：无 MinerU 环境时的降级策略

**预计工作量**：中等（主要是封装 + 测试）

---

## F2 — Agent 决策循环 + Harness

**目标**：最小可用的 ReAct Agent，能自主决定检索并返回带引用的回答。

**交付物**：

- `agent/state.py` — Agent 状态模型（对话历史 + 检索结果 + 工具结果）
- `agent/loop.py` — ReAct 决策循环（Thought → Action → Observation）
- `agent/graph.py` — 驱动循环（串联 loop 与 harness）
- `agent/prompts/system.md` — 系统提示词（角色、检索策略、引用规范）
- `agent/prompts/reflection.md` — 信息充分性自评
- `agent/tools/registry.py` — 工具注册与发现
- `agent/tools/retrieval_tools.py` — 检索工具（包装 F1 的 rag/）
- `runtime/harness.py` — 执行框架（超时、错误捕获、日志）

**验收标准**：

- 输入问题 → Agent 自主决定是否检索 → 调用检索工具 → 返回带引用的回答
- Agent 能判断"信息不足，需要再次检索"
- 回答中每个结论可追溯到具体文档片段
- 工具执行失败时不崩溃，返回标准化错误

**Grill 待决策**：

- [ ] LLM 调用方式：langchain ChatOpenAI vs 原生 openai SDK
- [ ] 工具调用格式：function calling vs ReAct 文本解析
- [ ] 最大迭代轮数：防止无限循环的上限
- [ ] 引用格式：[1] 数字标注 vs 脚注 vs 内联链接
- [ ] 系统提示词语言：中文 vs 英文 vs 双语

**预计工作量**：最大（核心逻辑都在这里）

**依赖**：F1

---

## F3 — 会话与流式输出

**目标**：支持多轮对话和实时流式输出。

**交付物**：

- `runtime/session.py` — 会话管理（多轮状态、上下文压缩）
- `runtime/streaming.py` — 流式输出（token 级流式 + 检索进度推送）
- `runtime/config.py` — 运行时配置（模型、检索参数、工具开关）

**验收标准**：

- 多轮对话中上下文正确保持（第 2 轮能引用第 1 轮的内容）
- 超长对话时自动压缩历史，不超 token 限制
- 流式输出可用：用户能实时看到 Agent 的思考和检索过程

**Grill 待决策**：

- [ ] 上下文压缩策略：滑动窗口 vs 摘要压缩 vs 混合
- [ ] 流式协议：SSE vs WebSocket
- [ ] 会话存储：内存 vs SQLite vs Redis

**预计工作量**：中等

**依赖**：F2

---

## F4 — API 服务

**目标**：可通过 HTTP 访问完整功能。

**交付物**：

- `server/app.py` — FastAPI 应用入口
- `server/routes_chat.py` — 对话接口（发送消息、流式回复）
- `server/routes_kb.py` — 知识库管理（上传文档、建索引、删库、检索测试）

**验收标准**：

- `POST /chat` 能发起对话，返回流式回复
- `POST /kb/upload` 能上传文档并触发摄入
- `GET /kb/search` 能测试检索效果
- 错误处理：无效输入、超时、内部错误均有标准 JSON 响应

**Grill 待决策**：

- [ ] 认证方式：无认证（内网）vs API Key vs JWT
- [ ] 文件上传大小限制
- [ ] 是否需要 WebSocket 实时推送

**预计工作量**：中等偏小

**依赖**：F3

---

## F5 — 多跳推理 + 查询改写

**目标**：Agent 能处理复杂问题，自主拆解为多次检索。

**交付物**：

- `agent/planner.py` — 复杂问题分解为多跳检索子问题
- `rag/retrieve/query_rewrite.py` — 查询改写（同义扩展、指代消解）
- `agent/prompts/query_rewrite.md` — 查询改写提示词
- `agent/tools/registry.py` 更新 — 注册新工具

**验收标准**：

- "对比 A 和 B" 类问题 → Agent 自动拆为两次检索
- "它" "这个" 等指代词 → 自动消解为具体实体
- 多跳推理链可追溯：能看到 Agent 的完整检索路径

**依赖**：F2

---

## F6 — 数据分析/建模工具扩展

**目标**：Agent 除检索外，还能做数据分析和建模。

**交付物**：

- `agent/tools/data_tools.py` — 数据加载、画像、探索
- `agent/tools/code_tools.py` — 执行 Python 分析代码
- `agent/tools/viz_tools.py` — 图表生成
- `agent/tools/modeling_tool.py` — 包装 scr/math_modeling_agent
- `agent/tools/export_tools.py` — 导出报告
- `runtime/sandbox.py` — 代码执行沙箱

**验收标准**：

- Agent 能加载 CSV/Excel 并自动画像
- Agent 能生成并执行分析代码，返回结果
- Agent 能触发完整建模工作流并获取结论
- 代码执行有超时和错误隔离

**依赖**：F2

---

## F7 — 可观测性 + 评测

**目标**：可追踪、可评估、可优化。

**交付物**：

- `observability/trace.py` — 完整执行链路追踪
- `observability/metrics.py` — 指标采集（token、耗时、命中率）
- `observability/logging.py` — 结构化日志
- `evaluation/test_cases/` — 固定测试集
- `evaluation/runner.py` — 回归测试
- `evaluation/retrieval_eval.py` — 检索质量评估（Recall@K, MRR）
- `evaluation/faithfulness_eval.py` — 忠实度评估

**验收标准**：

- 每次 Agent 执行有完整 trace，可回放决策路径
- 检索质量有量化指标，改参数后能对比效果
- 回答忠实度可自动检测（是否基于检索内容）

**依赖**：F2

---

## 开发顺序与里程碑

```
里程碑 M1（F1 完成）：检索管线可用，能独立测试检索质量
里程碑 M2（F2 完成）：Agent 能对话，端到端跑通
里程碑 M3（F3+F4 完成）：可通过 API 访问，多轮对话可用
里程碑 M4（F5+F6 完成）：复杂问题 + 数据分析能力
里程碑 M5（F7 完成）：可观测 + 可评测，具备运营条件
```

| 阶段   | Feature         | 依赖 | 可并行   |
| ------ | --------------- | ---- | -------- |
| 第一批 | F1 检索管线     | 无   | —       |
| 第二批 | F2 Agent 循环   | F1   | —       |
| 第三批 | F3 会话流式     | F2   | —       |
| 第四批 | F4 API 服务     | F3   | —       |
| 第五批 | F5 多跳推理     | F2   | F5 ∥ F6 |
|        | F6 数据分析工具 | F2   |          |
| 第六批 | F7 可观测+评测  | F2   | —       |

## 当前状态

- [X] 架构设计（ARCHITECTURE.md）
- [X] 开发计划（本文件）
- [ ] F1: RAG 检索管线 ← **下一步**
- [ ] F2: Agent 决策循环 + Harness
- [ ] F3: 会话与流式输出
- [ ] F4: API 服务
- [ ] F5: 多跳推理 + 查询改写
- [ ] F6: 数据分析/建模工具
- [ ] F7: 可观测性 + 评测
