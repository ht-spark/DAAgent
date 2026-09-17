# DeepSeek Harness 基于文件夹的项目开发与管理机制

## 1. 核心思路

DeepSeek Harness 将**文件夹作为项目的实际工作空间**。

整体关系为：

```text
Workspace（项目）
    ↓
对应一个真实文件夹
    ↓
创建 Session（会话）
    ↓
Session.cwd = Workspace.path
    ↓
文件、Shell、Sandbox、项目上下文
全部基于 Session.cwd 工作
```

核心原则：

> Workspace 负责项目管理，Session.cwd 负责项目运行时隔离。

---

## 2. Workspace：管理项目文件夹

用户添加一个项目文件夹：

```text
D:/Projects/RAG-Agent
```

Harness 会创建一个 Workspace：

```ts
interface Workspace {
    id: string;
    path: string;
    title: string;
    sessionIds: string[];
}
```

例如：

```json
{
  "id": "workspace-001",
  "path": "D:/Projects/RAG-Agent",
  "title": "RAG-Agent",
  "sessionIds": [
    "session-001",
    "session-002"
  ]
}
```

创建 Workspace 时，会先对路径进行规范化：

```text
用户选择目录
    ↓
realpath / canonicalize
    ↓
得到真实绝对路径
    ↓
注册 Workspace
```

这样可以避免：

```text
D:/project/../project
```

和：

```text
D:/project
```

被识别成两个不同项目。

---

## 3. 一个 Workspace 可以包含多个 Session

Workspace 和 Session 不是同一个概念。

例如：

```text
RAG-Agent/
│
├── Session 1：搭建项目结构
├── Session 2：实现向量检索
├── Session 3：开发前端
└── Session 4：修复 Bug
```

创建 Session 时：

```ts
session.cwd = workspace.path;
```

例如：

```text
Workspace:
D:/Projects/RAG-Agent

Session 1:
cwd = D:/Projects/RAG-Agent

Session 2:
cwd = D:/Projects/RAG-Agent
```

因此多个聊天可以共同操作同一个项目。

---

## 4. Session.cwd 是整个项目运行的核心

Session 创建后，会保存：

```ts
interface Session {
    id: string;
    cwd: string;
}
```

例如：

```text
Session A
cwd = D:/Projects/RAG-Agent

Session B
cwd = D:/Projects/Data-Agent
```

Harness 不通过：

```python
os.chdir(...)
```

修改整个程序的工作目录。

而是每个 Session 独立保存自己的 `cwd`。

因此同一个 Harness Server 可以同时运行：

```text
Harness Server
│
├── Session A → Project A
├── Session B → Project B
├── Session C → Project C
└── Session D → Project D
```

互不影响。

---

## 5. 文件工具基于 Session.cwd

假设：

```text
session.cwd =
D:/Projects/RAG-Agent
```

模型调用：

```text
read("src/main.py")
```

Harness 实际执行：

```text
resolve(
    session.cwd,
    "src/main.py"
)
```

得到：

```text
D:/Projects/RAG-Agent/src/main.py
```

因此所有文件工具：

```text
read
write
edit
grep
search
```

都应该使用：

```text
Session.cwd + 相对路径
```

而不是系统全局工作目录。

---

## 6. Shell 同样基于 Session.cwd

执行：

```bash
npm install
```

实际上相当于：

```python
subprocess.run(
    command,
    cwd=session.cwd
)
```

如果 Agent 指定：

```text
workdir = frontend
```

则：

```text
实际目录 =
session.cwd/frontend
```

例如：

```text
D:/Projects/RAG-Agent/frontend
```

因此：

```text
文件工具
Shell
终端
```

使用完全相同的项目根目录。

---

## 7. Sandbox 也绑定 Session.cwd

项目目录同时可以作为 Sandbox 的 Workspace Root。

例如：

```text
Session.cwd =
D:/Projects/RAG-Agent
```

则：

```text
Sandbox Root =
D:/Projects/RAG-Agent
```

在 `workspace-write` 模式下：

```text
允许：

D:/Projects/RAG-Agent/**

限制：

D:/Projects/Other-Agent/**
```

因此不同项目天然可以形成文件访问隔离。

整体链路为：

```text
Workspace.path
      ↓
Session.cwd
      ↓
File System Root
      ↓
Shell Workdir
      ↓
Sandbox Root
```

---

## 8. AGENTS.md 实现项目级上下文

Harness 可以从当前项目目录加载项目说明。

例如：

```text
RAG-Agent/
│
├── .git/
├── AGENTS.md
├── backend/
│   ├── AGENTS.md
│   └── app.py
└── frontend/
```

根目录：

```text
AGENTS.md
```

可以定义：

```text
- 项目使用 Python 3.12
- 后端使用 FastAPI
- 使用 Qdrant
- 测试使用 pytest
- 不允许修改 migrations
```

Agent 启动 Session 后，根据：

```text
Session.cwd
```

查找项目级：

```text
AGENTS.md
CLAUDE.md
```

进入子目录时还可以继续加载：

```text
backend/AGENTS.md
```

因此可以形成：

```text
全局规则
    ↓
项目规则
    ↓
目录规则
```

---

## 9. Session 持久化

Session 历史可以按照项目目录进行组织。

例如：

```text
sessions/
│
├── project-rag-agent/
│   ├── session-001/
│   │   └── session.jsonl
│   └── session-002/
│       └── session.jsonl
│
└── project-data-agent/
    └── session-003/
        └── session.jsonl
```

Session Header 至少保存：

```json
{
  "id": "session-001",
  "cwd": "D:/Projects/RAG-Agent",
  "createdAt": "..."
}
```

程序重新启动时，只需要读取：

```text
session.id
session.cwd
```

即可根据 `cwd` 将 Session 重新归类到对应 Workspace。

---

## 10. 删除 Workspace 不删除真实项目

Workspace 本质上只是：

> Harness 对某个真实文件夹的注册记录。

因此：

```text
Remove Workspace
```

应该只删除：

```text
Workspace Registry 中的记录
```

不应该执行：

```bash
rm -rf project
```

也不应该自动删除 Session 历史。

即：

```text
删除 Workspace
     ↓
取消 Harness 项目注册
     ↓
真实代码目录仍然存在
```

---

## 11. 推荐的数据结构

自己实现类似机制时，可以使用：

```ts
interface Project {
    id: string;
    name: string;

    // 规范化后的项目绝对路径
    rootPath: string;

    sessionIds: string[];

    createdAt: string;
    updatedAt: string;
}

interface Session {
    id: string;

    projectId: string;

    // 创建后尽量保持不可变
    cwd: string;

    createdAt: string;
}
```

---

## 12. 推荐实现流程

```text
用户选择文件夹
        ↓
获取真实绝对路径 realpath
        ↓
创建 / 查找 Project
        ↓
创建 Session
        ↓
Session.cwd = Project.rootPath
        ↓
加载 AGENTS.md
        ↓
初始化 Sandbox
        ↓
Agent 开始运行
        ↓
文件工具 ─────┐
Shell ────────┤
Git ──────────┤
搜索工具 ─────┤
Sandbox ──────┘
        ↓
全部使用 Session.cwd
```

---

## 13. 最小实现原则

最重要的是不要采用：

```python
os.chdir(project_path)
```

因为这是进程级状态，多 Session 并发时容易互相干扰。

应该采用：

```python
class Session:
    cwd: str
```

然后每个工具显式接收：

```python
cwd=session.cwd
```

例如：

```python
subprocess.run(
    command,
    cwd=session.cwd
)
```

文件路径：

```python
path = resolve_path(
    session.cwd,
    relative_path
)
```

Sandbox：

```python
sandbox_root = session.cwd
```

项目上下文：

```python
load_agent_instructions(
    session.cwd
)
```

最终形成：

```text
Project / Workspace
        │
        ├── Session A
        │      └── cwd
        │
        ├── Session B
        │      └── cwd
        │
        └── Session C
               └── cwd
                    ↓
        File / Shell / Git / Sandbox / Context
```

## 总结

DeepSeek Harness 的项目管理机制可以概括为：

```text
文件夹 = 项目实际载体

Workspace = 文件夹的管理记录

Session = 一次独立 Agent 会话

Session.cwd = 当前 Session 对应的项目目录

所有工具 = 基于 Session.cwd 执行
```

其中最关键的设计是：

> **不要让“项目目录”成为整个 Agent 进程的全局状态，而应该让它成为每个 Session 独立的运行时状态。**

这样才能自然支持多项目、多会话、文件操作、Shell、Sandbox、Git 和项目级上下文。