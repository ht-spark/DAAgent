# DAAgent Skills

每个 Skill 放在本目录的独立子目录中。目录名与 `SKILL.md` frontmatter 的
`name` 必须一致，并使用 kebab-case。

```text
skills/
└── ab-testing/
    ├── SKILL.md
    ├── DAAGENT.yaml
    ├── references/
    ├── assets/
    └── scripts/
```

`SKILL.md` 示例：

```markdown
---
name: ab-testing
title: A/B 实验分析
description: 实验设计、统计检验和结果复盘
---

先检查数据质量，再依据 references 中声明的统计规则进行分析。
```

`DAAGENT.yaml` 示例：

```yaml
version: 1
references:
  - id: statistics-rules
    path: references/statistics.md
    description: 统计检验规则
assets:
  - id: report-template
    path: assets/report_template.docx
    description: 报告模板
scripts:
  - id: check-srm
    path: scripts/check_srm.py
    description: 检查样本比例失配
    runtime: python
    timeout_seconds: 30
    requires_approval: true
    permissions:
      read_data_files: false
      read_assets: []
      write_artifacts: false
      network: false
```

脚本从 stdin 读取一个 JSON 对象，并在 stdout 只输出一个 JSON 对象：

```json
{"ok": true, "result": {}, "artifacts": []}
```

日志写入 stderr。脚本不能自行寻找数据、读取凭据、安装依赖或启动后台进程。
上传安装的 Skill 中，所有脚本入口都会被强制设置为逐次人工审批。

## 兼容无清单的 Skill

如果 Skill 没有 `DAAGENT.yaml`，系统会按约定目录自动发现：

- `references/` 内的普通文件，以及 Skill 根目录中的 Markdown、文本、JSON、YAML、CSV 文件，登记为参考资料；
- `assets/` 内的普通文件，以及根目录中的其他非说明文件，登记为资产；
- `scripts/` 内的 Python 文件登记为 `python-cli` 脚本，测试文件和以下划线开头的内部文件除外。

自动发现的脚本会读取模块文档字符串作为用途和 Usage 说明。调用时参数格式为：

```json
{"argv": ["输入文件绝对路径", "--output", "结果文件名"]}
```

绝对路径参数只能指向本轮用户附件或当前运行输出目录；相对输出文件会写入
`skill-artifacts/<skill-id>/`。脚本仍需人工批准，并由 Skill Runtime 独立于
ToolRegistry 执行。建议生产 Skill 补充 `DAAGENT.yaml`，以显式声明资源、权限、
超时和依赖版本。
