# DAAgent — 架构说明

面向产品经理与数据分析师的 **RAG 智能体**：基于私有知识库的检索增强生成 + Agent 自主决策。核心能力是从用户上传的文档中检索相关知识、自主判断信息是否充分、多次检索后生成带引用的回答；同时可按需调用数据分析和建模工具。

## 设计原则

- **检索优先**：回答基于知识库中的真实内容，不依赖 LLM 参数化记忆
- **Agent 自主决策**：LLM 自主决定是否检索、如何改写查询、检索几次、何时信息足够
- **引用可追溯**：每个结论都能定位到源文档的具体片段
- **现有代码零侵入**：复用 `KnowledgeBase/` 的检索管线和 `scr/` 的建模/执行能力
- **Loop / ReAct / Harness 分离**：决策与执行解耦，独立测试、独立演进

## 与纯 RAG 的区别

|            | 纯 RAG             | RAG Agent（本项目）                  |
| ---------- | ------------------ | ------------------------------------ |
| 流程       | 固定：检索 → 生成 | 动态：LLM 自主规划检索策略           |
| 检索次数   | 一次               | 可多次，可改写查询，可交叉验证       |
| 信息充分性 | 不判断             | LLM 自主评估"够不够，不够继续查"     |
| 工具       | 只有检索           | 检索 + 数据分析 + 建模 + 可视化      |
| 多跳推理   | 不支持             | "A 文档说 X，B 文档说 Y，对比得出 Z" |

## 项目结构

```
data_analysis/
├── ARCHITECTURE.md          ← 本文件
│
├── agent/                   #   Agent 决策编排层（大脑）
│   ├── graph.py             #   驱动循环：串联 loop（决策）与 harness（执行）
│   ├── loop.py              #   决策循环：LLM 推理 → 输出 Action（不执行）
│   ├── state.py             #   Agent 状态：对话历史 + 检索结果 + 工具结果
│   └── planner.py           #   复杂问题分解：将大问题拆成多跳检索子问题

├── rag/                     # RAG 管线层（检索基础设施）
│   ├── ingest/              #   文档摄入管线
│   │   ├── loader.py        #     文档加载：PDF/DOCX/PPTX/MD/TXT/图片
│   │   ├── parser.py        #     内容解析：MinerU 转 Markdown（公式/表格）
│   │   ├── chunker.py       #     分块策略：语义分块 / 递归分块 / 按标题分块
│   │   └── pipeline.py      #     摄入编排：加载→解析→分块→嵌入→入库
│   ├── retrieve/            #   检索引擎
│   │   ├── dense.py         #     稠密检索：BGE 语义向量
│   │   ├── sparse.py        #     稀疏检索：BM25 关键词
│   │   ├── hybrid.py        #     混合检索：双路召回 + RRF 融合
│   │   ├── reranker.py      #     重排序：Cross-encoder 精排 Top-K
│   │   └── query_rewrite.py #     查询改写（供 agent 调用，也可独立使用）
│   ├── index/               #   索引管理
│   │   ├── vector_store.py  #     向量存储：Qdrant 读写封装
│   │   ├── embedding.py     #     嵌入模型：BGE 调用与批量编码
│   │   └── index_manager.py #     索引生命周期：创建/更新/删除/重建
│   └── schemas.py           #   RAG 数据模型：Document, Chunk, RetrievalResult, Citation
│
├── runtime/                 # Agent 运行时（身体）
│   ├── harness.py           #   执行框架：Action → Observation（超时、重试、日志）
│   ├── session.py           #   会话管理：多轮对话状态、上下文压缩
│   ├── persistence.py       #   状态持久化：中断/恢复
│   ├── streaming.py         #   流式输出：token 级流式 + 检索进度推送
│   ├── interrupt.py         #   中断/恢复
│   ├── queue.py             #   任务队列：并发调度
│   ├── sandbox.py           #   沙箱执行：LLM 生成代码的隔离运行
│   └── config.py            #   运行时配置：模型、检索参数、工具开关
│
├── gateway/                 # 模型网关层
│   ├── router.py            #   模型路由：简单问答用小模型，复杂推理用大模型
│   ├── fallback.py          #   降级策略：主模型超时自动切换备用
│   ├── cache.py             #   缓存：相同查询的检索结果缓存 + LLM 响应缓存
│   ├── ratelimit.py         #   限流
│   └── metering.py          #   计量：token 用量与检索次数归属
│
├── memory/                  # 记忆系统
│   ├── working.py           #   工作记忆：当前任务已检索的片段、已得出的中间结论
│   ├── short_term.py        #   短期记忆：对话历史、用户偏好
│   ├── long_term.py         #   长期记忆：用户过往分析、领域知识图谱
│   └── episodic.py          #   情景记忆：历史检索轨迹，"上次类似问题查了什么"
│
├── observability/           # 可观测性
│   ├── trace.py             #   追踪：完整链路（查询改写→检索→重排→生成）
│   ├── metrics.py           #   指标：检索命中率、引用准确率、token 用量
│   ├── logging.py           #   结构化日志
│   └── dashboard.py         #   面板接口
│
├── security/                # 安全与权限
│   ├── input_filter.py      #   输入过滤：prompt injection 检测
│   ├── output_guard.py      #   输出审查：防泄露、防幻觉标记
│   ├── permissions.py       #   权限：知识库级访问控制（不同用户看不同库）
│   └── audit.py             #   操作审计
│
├── prompts/             #   Agent System Prompt 与工具描述模板
│     ├── system.md        #     系统提示词（角色、检索策略、引用规范）
│     ├── tool_descriptions.md  # 工具描述（供 LLM 选择工具时参考）
│     ├── query_rewrite.md   #     查询改写提示词（同义扩展、多跳拆解）
│     └── reflection.md    #     信息充分性自评（够不够？要不要再查？）
│
├── tools/                     # Agent 可调用的工具定义（schema + 描述，不含执行逻辑）
│     ├── registry.py          # 工具注册与发现机制
│     ├── retrieval_tools.py   # 知识库检索（核心工具）
│     ├── data_tools.py        # 数据加载、画像、探索
│     ├── viz_tools.py         # 图表生成
│     ├── modeling_tool.py     # 触发建模工作流（包装 scr/math_modeling_agent）
│     └── export_tools.py      # 导出报告（带引用的 Markdown / DOCX）
│
├── evaluation/              # 评测与质量
│   ├── test_cases/          #   固定测试集：问题 + 期望引用 + 期望答案
│   ├── runner.py            #   回归测试
│   ├── retrieval_eval.py    #   检索质量评估：Recall@K, MRR, NDCG
│   ├── faithfulness_eval.py #   忠实度评估：回答是否基于检索内容
│   └── feedback.py          #   在线反馈
│
├── server/                  # API 服务层
│   ├── app.py               #   FastAPI 入口
│   ├── routes_chat.py       #   对话接口
│   ├── routes_kb.py         #   知识库管理：上传文档、建索引、删库
│   ├── routes_files.py      #   文件接口
│   └── routes_admin.py      #   管理接口
│
└── web/                     # 前端界面
    ├── chat/                #   对话界面：消息流 + 引用标注 + 检索过程可视化
    ├── kb/                  #   知识库管理：文档上传、索引状态、检索测试
    ├── dashboard/           #   任务面板
    └── shared/              #   共享组件
```

## 分层依赖关系

```
web/ ──→ server/ ──→ agent/graph.py
                         │
              ┌──────────┼──────────┐
              ▼          ▼          ▼
         agent/loop  runtime/harness  memory/
         (决策)      (执行)           (记忆)
              │          │
              ▼          ▼
         agent/tools  runtime/sandbox
              │
    ┌─────────┼─────────┐
    ▼         ▼         ▼
  rag/     scr/      tools/
  (检索)   (建模)    (代码执行)
```

- `agent/` 是决策核心，不直接执行工具；`runtime/harness.py` 负责安全执行
- `rag/` 是检索基础设施，被 `agent/tools/retrieval_tools.py` 调用
- `agent/tools/modeling_tool.py` 调用现有 `scr/` 的 `run_graph()`，不修改其内部
- `server/` 和 `web/` 只与 `agent/graph.py` 交互
- `observability/`、`security/`、`evaluation/` 横切所有层

## Agent Loop 与 Harness 的边界

```
agent/loop.py（决策层 — 大脑）          runtime/harness.py（执行层 — 身体）
─────────────────────────            ─────────────────────────
"下一步该做什么？"                      "怎么安全地执行这个 action？"

  组装 prompt（系统提示+对话+工具列表）    权限检查
  调 LLM                              超时控制
  解析输出 → Action(tool_name, args)   沙箱/隔离调用工具
  或 Finish(answer)                   错误捕获与标准化
  返回给 graph.py                     记录 trace 日志
  （不自己执行）                        返回 Observation(result, error, duration)
```

驱动循环：

```python
while not done:
    action = loop.step(state)              # agent/loop.py 决策
    if action.is_finish:
        break
    observation = harness.execute(action)  # runtime/harness.py 执行
    state.append(observation)              # 更新状态，进入下一轮
```

**ReAct 是 loop 内的思考策略**（Thought → Action → Observation），不是独立模块。

## RAG Agent 典型执行轨迹

```
用户: "我们产品的退款政策和竞品相比有什么优势？"

第 1 轮
  Loop:  思考"需要先查我们的退款政策"
         → Action("retrieve", {"query": "产品退款政策", "kb": "internal"})
  Harness: 混合检索 + 重排 → 返回 5 个片段（含引用）

第 2 轮
  Loop:  看到我们的政策，但缺竞品信息
         → Action("retrieve", {"query": "竞品退款政策对比", "kb": "market"})
  Harness: 检索市场资料库 → 返回 3 个片段

第 3 轮
  Loop:  信息足够 → Finish("我们的优势是...[1][2]，竞品普遍...[3][4]")
         引用标注：每个结论对应具体文档片段
```

## 与现有模块的复用映射

| 现有模块                       | 在本项目中的角色                                    | 改动           |
| ------------------------------ | --------------------------------------------------- | -------------- |
| `KnowledgeBase/` 检索管线    | 被`rag/retrieve/` 复用：混合检索（BGE+BM25+RRF）  | 封装为独立模块 |
| `KnowledgeBase/` 摄入流程    | 被`rag/ingest/` 复用：MinerU 转换 + 分块 + 向量化 | 封装为独立模块 |
| `scr/math_modeling_agent/`   | 被`agent/tools/modeling_tool.py` 按需调用         | 无             |
| `scr/tools/code_executor.py` | 被`code_tools.py` 和 `runtime/sandbox.py` 复用  | 无             |
| `scr/tools/md2docx.py`       | 被`export_tools.py` 复用                          | 无             |
