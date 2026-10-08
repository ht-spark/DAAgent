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
