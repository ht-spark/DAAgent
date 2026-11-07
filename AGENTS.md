# Repository Guidelines

### 所有代码注释均所有中文

**脚本设计超参数的，要详细注释超参数的作用**

### Project Objectives and Functions

**Objective**: DAAgent is a task-oriented data analysis agent. It helps users answer questions, understand data, and produce evidence-grounded conclusions and actionable insights.

**Functions**: Based on user questions, data, attached documents, and project context, the agent autonomously plans and executes the analysis needed for the task. It can inspect and process data, perform calculations, create visualizations, retrieve supporting information, and deliver conclusions, code, files, or reports as appropriate. Mathematical modeling is one available tool, used only when it serves the analysis objective; it is not a required stage or the product's primary focus.

### Core Project Settings

1. The agent serves general data analysis tasks. All proposed optimizations or error corrections must take generality into consideration; localized modifications tailored only to one specific problem are prohibited.
2. The core capability is understanding the user's analysis objective, planning an appropriate analysis path, obtaining evidence through tools, and communicating reliable findings. The analysis workflow adopts an **LLM-only decision-making strategy**: no predefined methodologies are imposed, and problems are not rigidly categorized to apply fixed sets of methods. The LLM decides which tools to use and how to proceed based on the task, available data, and observed results; calculations and factual evidence must come from actual tool execution rather than unsupported model assertions.
3. Mathematical modeling is an optional tool within the unified data analysis agent, alongside data inspection, code execution, visualization, retrieval, and file operations. Do not treat it as the agent's central capability, a mandatory workflow, or a separate product line. Product copy, frontend entry points, prompts, and architecture descriptions must emphasize data analysis tasks and represent modeling only within its actual supported scope.

## Coding Style & Naming Conventions

Use Python 3.11+, four-space indentation, `snake_case` for modules/functions/variables, `PascalCase` for classes, and `UPPER_CASE` for constants. Add type annotations and Google-style docstrings to public Python functions. Keep imports ordered: standard library, third-party, then project modules. Prefer `logging` over `print`, catch specific exceptions, and keep prompts under `data_analysis/prompts/` rather than embedding large prompt strings in code.

Use TypeScript/React components in `PascalCase`; keep page-specific styles in `web/src/forms.css` and avoid unrelated CSS rewrites. Follow existing formatting; this repository does not currently enforce Black, isort, or ESLint through scripts.

## Testing Guidelines

Write `pytest` tests named `test_<behavior>.py` or `test_<behavior>` and cover malformed LLM/tool output at schema boundaries. Add a regression test for every bug fix. Mock LLM and network calls; tests must not require API keys. Run the focused test first, then the relevant suite, and run `npm run build` after frontend changes.

## Configuration & Safety

Store local provider settings in `.env` and frontend-only API settings in browser storage. Validate file paths before writing under `artifacts/`, and preserve the existing allowed-artifact checks in `server/runs.py`.
