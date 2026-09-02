"""
报告写作 Agent
职责：
  从已验证的 QuestionResult 生成完整竞赛报告草稿。
"""
from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..runtime.instrumented_llm import InstrumentedLLM
from ..schemas.context import DataProfile, ProjectContext
from ..schemas.paper import PaperDraft, PaperSection
from ..schemas.question import QuestionResult
from ..templates import get_template, PaperTemplate
from ..tools.code_executor import generate_all_figures
from ..tools.table_tools import (
    format_solution_table,
    format_metrics_table,
    format_validation_table,
    format_data_summary_table,
    format_comparison_table,
    format_assumptions_table,
    format_symbols_table,
    generate_latex_formula,
    public_evidence_pairs,
    zh_label_for_key,
    _unicode_to_latex,
    _is_mathematical_constraint,
)

__all__ = ["PaperWriter", "write_paper_node"]

_PAPER_WRITING_GUIDE_PATH = (
    Path(__file__).resolve().parent.parent / "prompts" / "writing_superclass.md"
)


def _as_mapping(value: Any, field_name: str) -> dict[str, Any]:
    """将不符合预期的映射字段降级为空字典，避免报告交付中断。"""
    if isinstance(value, dict):
        return value

    print(
        f"[writer] 忽略格式异常的 {field_name}（期望字典，实际 {type(value).__name__}）",
    )
    return {}


def _format_decision_variable(value: Any) -> str:
    """Format a string or LLM variable record for report prose."""
    if isinstance(value, Mapping):
        symbol = str(value.get("symbol") or value.get("name") or "").strip()
        meaning = str(value.get("meaning") or value.get("description") or "").strip()
        domain = str(value.get("domain") or "").strip()
        details = "；".join(part for part in (meaning, domain) if part)
        return f"{symbol}（{details}）" if symbol and details else symbol or details
    return str(value).strip()


def _format_decision_variables(value: Any) -> list[str]:
    """Normalize arbitrary variable collections before rendering a paper."""
    if not isinstance(value, (list, tuple)):
        value = [value]
    return [text for item in value if (text := _format_decision_variable(item))]


def _to_scalar_number(value: Any) -> float | None:
    """Best-effort conversion for report metrics that should be scalar."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        if hasattr(value, "shape") and hasattr(value, "size"):
            if int(value.size) != 1:
                return None
            return float(value.item())
    except Exception:  # noqa: BLE001
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt_numeric(value: Any, digits: int = 4) -> str:
    """Format scalar numbers and compact vector-like values for paper text."""
    scalar = _to_scalar_number(value)
    if scalar is not None:
        return f"{scalar:.{digits}f}"
    if hasattr(value, "tolist"):
        try:
            value = value.tolist()
        except Exception:  # noqa: BLE001
            return str(value)
    if isinstance(value, (list, tuple)):
        shown = [_fmt_numeric(item, digits) for item in list(value)[:6]]
        if len(value) > 6:
            shown.append("...")
        return "[" + ", ".join(shown) + "]"
    return str(value)


def _sequence_summary(value: Any, digits: int = 4) -> str:
    """Summarize long vector-like values for LLM report writing."""
    if hasattr(value, "tolist"):
        try:
            value = value.tolist()
        except Exception:  # noqa: BLE001
            return str(value)
    if not isinstance(value, (list, tuple)):
        return _fmt_numeric(value, digits)
    seq = list(value)
    if not seq:
        return "空序列"
    if seq and isinstance(seq[0], (list, tuple, dict)):
        return f"{len(seq)}项嵌套数据，首项={_fmt_numeric(seq[0], digits)}"
    numeric = [_to_scalar_number(item) for item in seq]
    nums = [n for n in numeric if n is not None]
    if len(nums) == len(seq):
        return (
            f"{len(seq)}项，首值={_fmt_numeric(seq[0], digits)}，"
            f"末值={_fmt_numeric(seq[-1], digits)}，"
            f"最小={_fmt_numeric(min(nums), digits)}，最大={_fmt_numeric(max(nums), digits)}"
        )
    shown = [_fmt_numeric(item, digits) for item in seq[:4]]
    if len(seq) > 4:
        shown.append("...")
    return f"{len(seq)}项，样例=[" + ", ".join(shown) + "]"


def _summarize_for_llm(value: Any, depth: int = 0) -> Any:
    """Build compact, faithful result material for LLM report prose."""
    if depth >= 3:
        return _sequence_summary(value) if isinstance(value, (list, tuple)) else _fmt_numeric(value)
    if hasattr(value, "tolist"):
        try:
            value = value.tolist()
        except Exception:  # noqa: BLE001
            return str(value)
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in list(value.items())[:20]:
            out[str(key)] = _summarize_for_llm(item, depth + 1)
        if len(value) > 20:
            out["_omitted"] = f"{len(value) - 20}项未展开"
        return out
    if isinstance(value, (list, tuple)):
        if len(value) <= 6:
            return [_summarize_for_llm(item, depth + 1) for item in value]
        return _sequence_summary(value)
    return _fmt_numeric(value)


def _build_llm_result_material(
    results: dict[str, Any],
    metrics: dict[str, Any],
    tables: list[str],
    figures: list[str],
) -> str:
    """Create compact result material that lets LLM select and polish evidence."""
    material = {
        "results_summary": _summarize_for_llm(results),
        "metrics_summary": _summarize_for_llm(metrics),
        "tables": "\n\n".join(tables)[:1500],
        "figures": [os.path.basename(fig) for fig in figures[:8]],
    }
    return json.dumps(material, ensure_ascii=False, default=str, indent=2)[:4000]


def _normalize_question_result(
    question_id: str,
    result: QuestionResult,
) -> QuestionResult:
    """规范化报告所依赖的嵌套映射字段，保留原始求解状态不变。"""
    computation = dict(_as_mapping(result.computation, f"{question_id}.computation"))
    computation["results"] = _as_mapping(
        computation.get("results", {}),
        f"{question_id}.computation.results",
    )
    computation["metrics"] = _as_mapping(
        computation.get("metrics", {}),
        f"{question_id}.computation.metrics",
    )

    formulation = dict(_as_mapping(result.formulation, f"{question_id}.formulation"))
    formulation["decision_variables"] = _format_decision_variables(
        formulation.get("decision_variables", [])
    )
    formulation["parameters"] = _as_mapping(
        formulation.get("parameters", {}),
        f"{question_id}.formulation.parameters",
    )

    return result.model_copy(
        update={
            "computation": computation,
            "formulation": formulation,
            "data_preparation": _as_mapping(
                result.data_preparation,
                f"{question_id}.data_preparation",
            ),
            "findings": _as_mapping(result.findings, f"{question_id}.findings"),
            "decision_record": _as_mapping(
                result.decision_record,
                f"{question_id}.decision_record",
            ),
            "validation": _as_mapping(result.validation, f"{question_id}.validation"),
        }
    )


def _instrumented_llm(state: dict) -> Any:
    """返回监控包装后的 LLM（全局阶段，无当前小问）。

    无预算管理器或无 LLM 时原样返回，避免改变既有无 LLM 降级路径。
    """
    llm = state.get("llm")
    budget_manager = state.get("budget_manager")
    if budget_manager is not None and llm is not None:
        return InstrumentedLLM(llm, budget_manager, qid_getter=None)
    return llm


def _render_table_safely(table_name: str, renderer: Any) -> str:
    """单张报告表失败时保留正文，并给出可追踪的占位说明。"""
    try:
        return renderer()
    except (TypeError, ValueError, KeyError) as exc:
        print(f"[writer] 跳过 {table_name}：{exc}")
        return f"（{table_name}生成失败，已保留其余报告内容）"


# ---------------------------------------------------------------------------
# 学术写作辅助函数
# ---------------------------------------------------------------------------

_INTERNAL_WRITING_TERMS = ("LLM", "问题驱动建模", "候选方法")


def _public_model_label(formulation: dict[str, Any]) -> str:
    """Return a paper-facing model label without exposing workflow internals."""
    description = str(formulation.get("description", "")).strip()
    if description and not any(term in description for term in _INTERNAL_WRITING_TERMS):
        return description
    return "本题数学模型"


def _write_model_from_evidence(qid: str, formulation: dict[str, Any]) -> str:
    """Describe only the recorded model elements; never infer a preset method."""
    parts: list[str] = []
    description = _public_model_label(formulation)
    if description != "本题数学模型":
        parts.append(description)
    variables = formulation.get("decision_variables", [])
    if variables:
        parts.append(f"模型以{', '.join(map(str, variables[:8]))}等变量刻画题设对象及其状态。")
    objective = formulation.get("objective_function", "")
    if objective:
        parts.append(f"核心数学关系由“{objective}”给出，并与题设条件共同确定计算过程。")
    constraints = formulation.get("constraints", [])
    if constraints:
        parts.append(f"模型同时纳入{len(constraints)}项已记录约束，以保证计算过程符合题设边界。")
    if not parts:
        return f"问题{qid}的模型要素尚未完整记录，无法补写未提供的变量、方程或约束。"
    return "\n\n".join(parts)


def _write_results_from_evidence(qid: str, computation: dict[str, Any]) -> str:
    """Summarize recorded scalar outputs without task-type assumptions.

    只引用能完整中文化的标量结论；内部流程键（model_name、figures 等）
    与原始 snake_case 键名一律不进入论文文本。
    """
    status = computation.get("status", "unknown")
    if status in {"failed", "error", "infeasible"}:
        return "当前计算未形成可用于论文结论的有效结果，需修正模型或参数后重新求解。"
    results = _as_mapping(computation.get("results", {}), f"{qid}.results")
    metrics = _as_mapping(computation.get("metrics", {}), f"{qid}.metrics")
    pairs = public_evidence_pairs(results, max_items=4) or \
        public_evidence_pairs(metrics, max_items=4)
    if not pairs:
        return (
            f"问题{qid}的求解形成了稳定的数值输出，关键数值结论、"
            "表格与图形见正文相应章节。"
        )
    shown = "；".join(f"{label}为{value}" for label, value in pairs)
    return (
        f"计算得到{shown}。上述输出仅在题设条件和已声明假设下成立。"
    )


def _write_conclusion_from_evidence(qid: str, computation: dict[str, Any]) -> str:
    """Produce a bounded conclusion based on available computation evidence."""
    if computation.get("status") in {"failed", "error", "infeasible"}:
        return f"问题{qid}尚未形成可交付结论，需要补充有效计算结果。"
    return f"问题{qid}的结论以本节列示的模型计算结果为依据，适用范围限于题设条件和已声明的模型假设。"


# 可复用结论中允许出现在论文里的内部流程标志（命中即整行丢弃）
_CONCLUSION_DROP_KEYWORDS = (
    "Phase", "题型验证待", "待 Phase", "计算状态", "确定性代码",
    "方法家族", "⚠", "generation_attempts",
    # 内部键名与产物路径痕迹：无论出现在结论行何处都整行丢弃
    "model_name", "model_summary", "figures", "figure",
    "artifacts", "code_path", "solution_code", "validation_details",
    ".png", ".jpg", ".csv", ".json", ".py",
)

# “原始键名 = 数值” 形式的结论行（键名可能无法中文化）
_RAW_CONCLUSION_PATTERN = None  # 延迟初始化，见 _public_conclusion_line


def _public_conclusion_line(line: str) -> str | None:
    """把一条内部结论转换为论文可用文本；无法安全展示时返回 None。

    - 命中内部流程关键词或本地路径的行直接丢弃；
    - “snake_case_key = 0.000000” 形式的行翻译为“中文标签为 0.000000”，
      键名无法完整中文化时丢弃，避免原始键名泄漏。
    """
    global _RAW_CONCLUSION_PATTERN
    import re

    text = line.strip()
    if not text:
        return None
    if any(kw in text for kw in _CONCLUSION_DROP_KEYWORDS):
        return None
    # 本地绝对路径（盘符）
    if re.search(r"[A-Za-z]:\\", text):
        return None
    if _RAW_CONCLUSION_PATTERN is None:
        _RAW_CONCLUSION_PATTERN = re.compile(
            r"^([A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+)\s*=\s*(.+)$"
        )
    match = _RAW_CONCLUSION_PATTERN.match(text)
    if match:
        label = zh_label_for_key(match.group(1))
        if not label:
            return None
        return f"{label}为{match.group(2).strip()}"
    # 非键值行：行内若残留 snake_case 键名则整行丢弃
    if re.search(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b", text):
        return None
    return text


def _validation_summary_from_evidence(qid: str, validation: dict[str, Any]) -> str:
    """Describe recorded validation evidence without inferring a task-specific test."""
    checks = validation.get("checks", [])
    if not checks:
        return f"问题{qid}未记录可展开的验证明细，结论仅限于已展示的计算输出。"
    passed = sum(1 for check in checks if isinstance(check, dict) and check.get("passed"))
    return f"本问记录了{len(checks)}项结果核查，其中{passed}项通过；具体核查内容见下表。"


def _remove_internal_writing_terms(text: str) -> str:
    """Keep implementation vocabulary out of the delivered paper.

    除固定词替换外，还以正则清除三类泄漏：
      1. 本地绝对路径（如 ``D:\\MM\\artifacts\\...``、``/tmp/...``）；
      2. ``model_name=...``、``figures=[...]`` 等内部键值对；
      3. ``snake_case_key = 0.0000`` 形式的原始结果键值对。
    """
    import re

    replacements = {
        "LLM 问题驱动建模": "本题数学模型",
        "LLM问题驱动建模": "本题数学模型",
        "LLM": "",
        "Agent": "",
    }
    for source, target in replacements.items():
        text = text.replace(source, target)

    # 本地绝对路径（盘符路径 / artifacts 相对路径），保留 Markdown 相对图片链接
    text = re.sub(r"[A-Za-z]:\\[^\s；;）)\]、，。]*", "", text)
    text = re.sub(r"(?<!\()(?:artifacts[/\\])\S+\.(?:png|jpg|csv|json|py)\b", "", text)

    # 内部键值对（model_name=、figures=[...]、status: success 等）
    text = re.sub(
        r"\b(?:model_name|model_summary|validation_details|artifacts_dir|"
        r"solution_code_snippet|generation_attempts)\s*[=:：]\s*[^；;\n]*",
        "",
        text,
    )
    text = re.sub(r"\bfigures\s*[=:：]\s*\[[^\]]*\]", "", text)
    text = re.sub(r"\bstatus\s*[=:：]\s*(?:success|error|failed|unknown)\b", "", text)

    # 原始 snake_case 键值对（如 effective_shielding_duration_seconds = 0.0000）
    text = re.sub(
        r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\s*=\s*-?\d[\d.,eE+-]*",
        "",
        text,
    )

    # 清理替换后留下的空 bullet / 空行残留
    text = re.sub(r"^[ \t]*-[ \t]*：?[ \t]*$", "", text, flags=re.MULTILINE)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


class PaperWriter:
    """报告写作 Agent（确定性模板，无 LLM）。

    从已验证的小问结果包生成完整报告草稿，
    遵循 architecture.md §6.2 推荐章节结构。
    集成可视化工具生成 PNG 图表，集成表格工具生成规范三线表。

    Usage::

        writer = PaperWriter()
        paper = writer.write(state, output_dir="artifacts/run_xxx")
    """

    def __init__(self, llm: Any | None = None) -> None:
        """初始化 PaperWriter。

        Args:
            llm: 可选的 LLM 客户端。提供时，"模型建立/结果解释"核心段落
                 优先由 LLM 起草（失败自动回退确定性模板）。
        """
        self._llm = llm
        self._prompt_dir = Path(__file__).resolve().parent.parent / "prompts"
        self._title: str = "数学建模报告"
        self._output_dir: str = ""
        self._all_figures: dict[str, list[str]] = {}
        self._fig_counter: int = 0
        self._tbl_counter: int = 0
        self._formula_counter: int = 0
        self._shown_conclusions: set[str] = set()
        self._template: PaperTemplate | None = None
        self._template_guide: str = ""  # 模板写作指导（注入 LLM 提示词）
        self._writing_guide: str = ""
        self._project_context: ProjectContext | None = None
        self._data_profile: DataProfile | None = None

    # ------------------------------------------------------------------
    # 主方法
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # LLM 起草辅助（P1-B3：报告核心章节 LLM 写作，失败回退模板）
    # ------------------------------------------------------------------

    def _load_prompt(self, name: str) -> str:
        """加载 prompt 模板文件（prompts/<name>.md）。"""
        path = self._prompt_dir / f"{name}.md"
        if not path.exists():
            raise FileNotFoundError(f"Prompt template not found: {path}")
        return path.read_text(encoding="utf-8")

    @staticmethod
    def _load_writing_guide() -> str:
        """加载 prompts 中的报告写作总纲，缺失时不阻断既有流程。"""
        try:
            return _PAPER_WRITING_GUIDE_PATH.read_text(encoding="utf-8").strip()
        except OSError as exc:
            print(f"[writer] 未加载报告写作总纲，使用现有写作提示词: {exc}")
            return ""

    @staticmethod
    def _render_prompt(template: str, **kwargs: Any) -> str:
        """渲染 prompt 模板：替换 {var} 占位符。"""
        result = template
        for key, value in kwargs.items():
            result = result.replace(f"{{{key}}}", str(value))
        return result

    @staticmethod
    def _strip_markdown_headers(text: str) -> str:
        """去掉 LLM 输出中可能误带的 Markdown 标题/分隔线，保持章节结构安全。"""
        import re

        lines = []
        for line in text.splitlines():
            stripped = line.strip()
            if re.match(r"^#{1,6}\s", stripped):
                continue  # 跳过 # 标题
            if re.match(r"^-{3,}$", stripped) or re.match(r"^={3,}$", stripped):
                continue  # 跳过分隔线
            lines.append(line)
        return "\n".join(lines).strip()

    def _llm_write_prompt(self, template_name: str, **kwargs: Any) -> str | None:
        """用 LLM 起草一段报告正文；失败返回 None（调用方回退确定性模板）。"""
        if self._llm is None:
            return None
        try:
            template = self._load_prompt(template_name)
            prompt = self._render_prompt(template, **kwargs)
            response = self._llm.invoke(prompt)
            text = getattr(response, "content", response)
            # 剥离 <think> 思维链，防止混入报告正文
            from ..tools.llm_response import strip_thinking

            text = strip_thinking(str(text))
            text = self._strip_markdown_headers(text)
            text = _remove_internal_writing_terms(text)
            if any(term in text for term in _INTERNAL_WRITING_TERMS):
                print(f"[writer] LLM 起草 {template_name} 包含内部流程术语，改用事实型正文")
                return None
            # 清洗后仍残留本地路径或多段 snake_case 键值对则拒绝整段草稿
            import re as _re

            if _re.search(r"[A-Za-z]:\\", text) or _re.search(
                r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+){2,}\s*[=:：]", text
            ):
                print(f"[writer] LLM 起草 {template_name} 残留内部键名/路径，改用事实型正文")
                return None
            return text or None
        except Exception as e:
            print(f"[writer] LLM 起草 {template_name} 失败，回退模板: {e}")
            return None

    def write(self, state: dict, output_dir: str = "") -> PaperDraft:
        """从 question_results 生成完整 PaperDraft。

        Args:
            state: 项目状态，需包含 question_results、project_context、data_profile。
                  可选包含 review_report（修订时传入）和 _gf_retry_count。
            output_dir: 产物输出目录，用于保存图表 PNG 文件。

        Returns:
            完整的 PaperDraft 对象。
        """
        # 提取状态数据
        raw_results = _as_mapping(
            state.get("question_results", {}),
            "question_results",
        )
        project_context: ProjectContext | None = state.get("project_context")
        data_profile: DataProfile | None = state.get("data_profile")
        review_report = state.get("review_report")
        gf_retry = state.get("_gf_retry_count", 0)

        normalized_results = {
            qid: _normalize_question_result(qid, result)
            for qid, result in raw_results.items()
            if isinstance(result, QuestionResult)
        }

        # 只使用已验证的结果；blocked 结果单独保留用于占位章节
        validated: dict[str, QuestionResult] = {
            qid: r for qid, r in normalized_results.items() if r.status == "validated"
        }
        blocked: dict[str, QuestionResult] = {
            qid: r for qid, r in normalized_results.items() if r.status == "blocked"
        }

        if gf_retry > 0:
            print(f"[writer] 第 {gf_retry} 次修订，基于审查反馈改进报告")
        print(f"[writer] 开始报告写作: {len(validated)} 个已验证小问"
              + (f"，{len(blocked)} 个小问被阻塞（占位章节）" if blocked else ""))

        # 设置输出目录
        self._output_dir = output_dir or state.get("output_dir", "artifacts/paper")
        self._fig_counter = 0
        self._tbl_counter = 0
        self._formula_counter = 0
        self._shown_conclusions = set()
        self._project_context = project_context
        self._data_profile = data_profile
        self._writing_guide = self._load_writing_guide()
        if self._writing_guide:
            print("[writer] 已加载报告写作总纲，用于 LLM 核心章节写作")

        # 生成所有图表 PNG（包括 blocked 结果，只要含有 computation 数据即可生成图表）
        all_results_for_figures = {**validated, **blocked}
        if all_results_for_figures:
            try:
                executed_figures = {
                    qid: list(result.figures)
                    for qid, result in all_results_for_figures.items()
                    if result.figures
                }
                self._all_figures = executed_figures or generate_all_figures(
                    all_results_for_figures, data_profile, self._output_dir
                )
                total_figs = sum(len(v) for v in self._all_figures.values())
                print(f"[writer] 已生成 {total_figs} 张图表")
            except Exception as e:
                print(f"[writer] 图表生成失败（不影响报告写作）: {e}")
                self._all_figures = {}

        # 派生标题
        self._title = self._derive_title(project_context)

        # 选择报告模板（基于题型）
        self._template = self._select_template(project_context)

        # 构建大纲（基于模板定制章节结构）
        sections = self._build_outline(validated, blocked)

        # 填充非小问章节
        for section in sections:
            if section.question_id is not None:
                continue
            if section.section_id == "1":
                section.content = self._write_problem_restatement(project_context)
            elif section.section_id == "2":
                section.content = self._write_assumptions(validated)
            elif section.section_id == "3":
                section.content = self._write_data_description(data_profile)
            elif section.section_id == "4":
                section.content = self._write_question_intro(validated, blocked)
            elif section.section_id == "5":
                section.content = self._write_evaluation(validated)
            elif section.section_id == "6":
                section.content = self._write_references_text(validated)
            elif section.section_id == "7":
                section.content = self._write_appendix(validated)

        # 填充小问章节
        for section in sections:
            if section.question_id is not None:
                result = validated.get(section.question_id)
                if result is not None:
                    filled = self._write_question_section(
                        section.question_id, result
                    )
                    section.content = filled.content
                    section.figures = filled.figures
                    section.tables = filled.tables
                    section.formulas = filled.formulas
                elif section.question_id in blocked:
                    # blocked 小问：生成占位章节（说明阻塞原因），避免整节消失
                    section.content = self._write_blocked_section(
                        section.question_id, blocked[section.question_id]
                    )

        # 生成摘要（最后生成，不引入新数字）
        abstract = self._write_abstract(validated, sections)

        # 组装完整 Markdown 文本
        revision_notes = self._build_revision_notes(review_report, gf_retry)
        full_text = self._assemble_full_text(sections, abstract, revision_notes)

        # 收集引用列表（从参考文献章节文本解析）
        references_text = next(
            (s.content for s in sections if s.section_id == "6"), ""
        )
        references = self._collect_references(references_text)

        print(
            f"[writer] 报告写作完成: {self._title} "
            f"({len(sections)} 节, {len(full_text)} 字符, "
            f"{self._fig_counter} 图, {self._tbl_counter} 表)"
        )

        return PaperDraft(
            title=self._title,
            sections=sections,
            abstract=abstract,
            references=references,
            full_text=full_text,
        )

    # ------------------------------------------------------------------
    # 模板选择
    # ------------------------------------------------------------------

    def _select_template(
        self, project_context: ProjectContext | None
    ) -> PaperTemplate:
        """加载统一报告模板。

        模板已统一为「公共骨架 + 可伸缩问题章节」结构，不再按题型
        （A-F）区分，因此无论任务类型如何都返回同一套模板。
        保留 project_context 参数以兼容既有调用。

        Args:
            project_context: 项目上下文（保留参数兼容性，当前未使用）。

        Returns:
            统一的 PaperTemplate。
        """
        self._template = get_template()
        self._template_guide = self._build_template_guide()
        print(f"[writer] 加载统一报告模板: {self._template.name}")
        return self._template

    @staticmethod
    def _build_template_guide() -> str:
        """从统一模板构建写作指导文本，注入 LLM 提示词。

        覆盖：全局写作要点、摘要要求、问题章节五段式结构、可选方法库。
        这样 LLM 写作时严格遵循统一模板规范，而不是自由发挥。
        """
        from ..templates.paper_templates import UNIFIED_TEMPLATE, build_problem_section

        parts: list[str] = []

        tips = UNIFIED_TEMPLATE.writing_tips
        if tips:
            parts.append(
                "【全局写作要点】\n" + "\n".join(f"- {t}" for t in tips)
            )

        if UNIFIED_TEMPLATE.abstract_guide:
            parts.append(
                "【摘要写作要求】\n" + UNIFIED_TEMPLATE.abstract_guide
            )

        problem_guide = build_problem_section(1).writing_guide
        parts.append("【问题章节组织原则】\n" + problem_guide)

        return "\n\n".join(parts)

    # ------------------------------------------------------------------
    # 大纲构建
    # ------------------------------------------------------------------

    def _build_outline(
        self,
        question_results: dict[str, QuestionResult],
        blocked: dict[str, QuestionResult] | None = None,
    ) -> list[PaperSection]:
        """构建报告大纲。

        根据报告模板定制章节标题，保持章节 ID 与内容填充逻辑兼容。
        模板提供各题型的推荐章节结构和写作指导，
        本方法将模板章节映射到固定的 section_id 体系（1-7），
        以确保 write() 中的内容填充逻辑正常工作。

        映射关系：
          模板"问题重述"类  → section_id="1"
          模板"假设/符号"类 → section_id="2"
          模板"分析/技术路线"类 → section_id="3"
          模板"问题一~N"类  → section_id="4" + 子节 "4.1"~"4.N"
          模板"评价/改进"类 → section_id="5"
          模板"参考文献"     → section_id="6"
          模板"附录"        → section_id="7"
        """
        template = self._template or get_template()
        sections: list[PaperSection] = []
        sorted_qids = sorted(question_results.keys())

        # --- Section 1: 问题重述 ---
        title_1 = self._match_template_title(
            template, ["重述", "简介"], "问题重述与问题分析"
        )
        sections.append(
            PaperSection(section_id="1", title=title_1, order=10)
        )

        # --- Section 2: 模型假设与符号说明 ---
        title_2 = self._match_template_title(
            template, ["假设", "符号"], "模型假设与符号说明"
        )
        sections.append(
            PaperSection(section_id="2", title=title_2, order=20)
        )

        # --- Section 3: 数据说明 / 问题分析 ---
        title_3 = self._match_template_title(
            template, ["分析", "技术路线", "数据"], "数据说明与预处理"
        )
        sections.append(
            PaperSection(section_id="3", title=title_3, order=30)
        )

        # --- Section 4: 各小问的模型建立、求解、结果和检验 ---
        title_4 = "各小问的模型建立、求解、结果和检验"
        sections.append(
            PaperSection(section_id="4", title=title_4, order=40)
        )

        for i, qid in enumerate(sorted_qids, start=1):
            # 尝试从模板中获取问题章节标题格式
            q_title = self._get_question_section_title(template, i, qid)
            sections.append(
                PaperSection(
                    section_id=f"4.{i}",
                    title=q_title,
                    question_id=qid,
                    order=40 + i,
                )
            )

        # blocked 小问追加占位章节（编号接在已验证小问之后，标题标注未完成）
        blocked_qids = sorted(blocked.keys()) if blocked else []
        for j, qid in enumerate(blocked_qids, start=len(sorted_qids) + 1):
            q_title = self._get_question_section_title(template, j, qid)
            sections.append(
                PaperSection(
                    section_id=f"4.{j}",
                    title=f"{q_title}（未完成）",
                    question_id=qid,
                    order=40 + j,
                )
            )

        # --- Section 5: 模型评价 ---
        title_5 = self._match_template_title(
            template, ["评价", "改进", "推广", "总结"], "模型评价、优缺点与推广"
        )
        sections.append(
            PaperSection(section_id="5", title=title_5, order=50)
        )

        # --- Section 6: 参考文献 ---
        sections.append(
            PaperSection(section_id="6", title="参考文献", order=60)
        )

        # --- Section 7: 附录 ---
        sections.append(
            PaperSection(section_id="7", title="附录", order=70)
        )

        return sections

    @staticmethod
    def _match_template_title(
        template: PaperTemplate, keywords: list[str], default: str
    ) -> str:
        """返回默认章节标题。

        模板中的章节标题来自示例报告，含有领域特定内容，
        直接使用会导致标题泄露（如"交叉分发方案研究"出现在
        农作物种植策略报告中）。因此统一使用通用默认标题。

        Args:
            template: 报告模板（保留参数兼容性，当前未使用）。
            keywords: 关键词列表（保留参数兼容性，当前未使用）。
            default: 默认标题。

        Returns:
            默认标题。
        """
        return default

    @staticmethod
    def _get_question_section_title(
        template: PaperTemplate, index: int, qid: str
    ) -> str:
        """生成第 index 个问题章节的标题。

        使用通用格式 "问题X"（X 为中文数字），不使用模板中的
        领域特定标题，避免将模板示例问题的标题泄露到实际报告中。

        Args:
            template: 报告模板（保留参数兼容性，当前未使用）。
            index: 问题序号（从 1 开始）。
            qid: 问题 ID。

        Returns:
            问题章节标题，如 "问题一"。
        """
        cn_nums = ["一", "二", "三", "四", "五", "六", "七", "八", "九", "十"]
        if 1 <= index <= len(cn_nums):
            return f"问题{cn_nums[index - 1]}"
        return f"问题 {qid}"

    # ------------------------------------------------------------------
    # 摘要（最后生成）
    # ------------------------------------------------------------------

    def _abstract_background(self) -> str:
        """提取摘要用的问题背景文本（去页码标记，适度截断）。"""
        if not (self._project_context and self._project_context.background_summary):
            return ""
        import re as _re

        bg_text = _re.sub(
            r"---\s*第\s*\d+\s*页\s*---", "", self._project_context.background_summary
        ).strip()
        if len(bg_text) > 800:
            bg_text = bg_text[:800] + "……"
        return bg_text

    def _build_abstract_question_material(
        self, qid: str, result: QuestionResult
    ) -> dict[str, Any]:
        """为摘要 LLM 起草构建单问材料（仅含论文可公开信息）。"""
        computation = _as_mapping(result.computation, f"{qid}.computation")
        results = _as_mapping(computation.get("results", {}), f"{qid}.results")
        metrics = _as_mapping(computation.get("metrics", {}), f"{qid}.metrics")
        formulation = _as_mapping(result.formulation, f"{qid}.formulation")
        interp = result.problem_interpretation

        model_desc = str(formulation.get("description", "")).strip()
        if not model_desc:
            model_name = str(computation.get("model_name", "")).strip()
            if model_name and "LLM" not in model_name:
                model_desc = model_name
        if len(model_desc) > 300:
            model_desc = model_desc[:300] + "……"

        pairs = public_evidence_pairs(results, max_items=6) or \
            public_evidence_pairs(metrics, max_items=6)

        return {
            "问题编号": qid,
            "问题任务": (
                interp.math_task_description if interp and interp.math_task_description
                else f"小问 {qid}"
            ),
            "模型与方法": model_desc or "（模型描述见正文）",
            "关键数值结论": [f"{label} = {value}" for label, value in pairs],
            "图表": f"{len(result.figures or [])} 张图、{len(result.tables or [])} 张表",
        }

    def _write_abstract(
        self,
        question_results: dict[str, QuestionResult],
        sections: list[PaperSection],
    ) -> str:
        """生成摘要（最后生成，不引入新数字）。

        优先由 LLM 按"背景-方法-结果-结论"结构起草；失败时回退到
        确定性模板。所有数值均来自 QuestionResult，不引入新数字。
        """
        template = self._template or get_template()
        lines: list[str] = []
        sorted_qids = sorted(question_results.keys())
        n = len(sorted_qids)

        # 问题背景概述（基于项目上下文生成具体描述）
        category_desc = self._get_category_description(template)
        bg_text = self._abstract_background()
        bg_brief = bg_text[:200] + "……" if len(bg_text) > 200 else bg_text

        if n == 0:
            # 无任何已验证结果：明确说明，避免产出"已完整建模"的误导性摘要
            notice = (
                "【重要说明】本次运行未能产出任何有效的建模与求解结果——"
                "所有子问题均因数据缺失、代码执行失败或验证未通过而被阻塞。"
                "请检查：1) 是否上传了必需的数据文件；2) LLM 模型配置是否可用；"
                "3) 任务参数是否完整。本报告为占位文档，不包含实际建模内容。"
            )
            return (bg_text + "\n" if bg_text else "") + notice

        # 优先由 LLM 按"背景-方法-结果-结论"结构起草；失败回退确定性模板
        question_materials = [
            self._build_abstract_question_material(qid, question_results[qid])
            for qid in sorted_qids
        ]
        abstract_llm = self._llm_write_prompt(
            "paper_abstract",
            background=bg_text or "（见正文问题重述）",
            questions=json.dumps(question_materials, ensure_ascii=False, indent=2),
            template_guide=self._template_guide,
            writing_guide=self._writing_guide,
        )
        if abstract_llm:
            keywords = self._collect_keywords(question_results)
            if keywords:
                abstract_llm += "\n\n**关键词**：" + "；".join(keywords)
            return abstract_llm

        # --- 回退：确定性模板摘要 ---
        if bg_text:
            lines.append(bg_text)
            lines.append(
                f"本文针对{category_desc}，建立了完整的数学建模与求解框架，"
                f"共完成 {n} 个子问题的建模、求解与验证。"
            )
        else:
            lines.append(
                f"本文针对{category_desc}，建立了完整的数学建模与求解框架，"
                f"共完成 {n} 个子问题的建模、求解与验证。"
            )
        lines.append("")

        lines.append(
            "全文以题设条件为起点，将已归档的变量、数学关系、约束和计算输出"
            "组织为可核查的建模过程；文中的数值结论、表格和图形均对应实际求解记录。"
        )
        lines.append("")

        # 各问关键结果
        lines.append("各子问题的主要研究结果如下：")
        lines.append("")
        for qid in sorted_qids:
            result = question_results[qid]
            computation = result.computation
            lines.append(f"针对问题 {qid}，{_write_results_from_evidence(qid, computation)}")

        # 总体结论（基于实际结果生成结论）
        lines.append("")
        lines.append(
            "综上，结论以各问已展示的模型与计算证据为限；其适用性受题设条件、"
            "数据范围和已声明假设约束。"
        )

        # 关键词（仅取各问实际关键词；模板中的 keyword_suggestions
        # 是选取规则的指导语，不是可直接使用的关键词，故不再合并）
        keywords = self._collect_keywords(question_results)
        if keywords:
            lines.append("")
            lines.append("**关键词**：" + "；".join(keywords))

        return "\n".join(lines)

    @staticmethod
    def _get_category_description(template: PaperTemplate) -> str:
        """生成问题描述短语。

        模板已统一为不分题型的单一结构，此处返回通用描述。
        保留 template 参数以兼容既有调用。

        Args:
            template: 报告模板（保留参数兼容性，当前未使用）。

        Returns:
            问题描述短语。
        """
        return "数学建模问题"

    # ------------------------------------------------------------------
    # 问题重述与问题分析
    # ------------------------------------------------------------------

    def _write_problem_restatement(
        self, project_context: ProjectContext | None
    ) -> str:
        """生成问题重述与问题分析。"""
        lines: list[str] = []

        if project_context is None:
            lines.append("（项目上下文缺失，问题重述待补充）")
            return "\n".join(lines)

        # 1.1 问题背景
        lines.append("### 1.1 问题背景")
        lines.append("")
        if project_context.background_summary:
            # 清理背景文本中的页码标记
            bg = project_context.background_summary
            import re
            bg = re.sub(r"---\s*第\s*\d+\s*页\s*---", "", bg).strip()
            lines.append(bg)
        else:
            lines.append("（背景描述待补充）")

        # 1.2 问题分析
        lines.append("")
        lines.append("### 1.2 问题分析")
        lines.append("")
        analysis = self._generate_problem_analysis(project_context)
        lines.append(analysis)

        # 1.3 研究目标
        lines.append("")
        lines.append("### 1.3 研究目标")
        lines.append("")
        if project_context.objectives:
            for obj in project_context.objectives:
                lines.append(f"- {obj}")
        else:
            lines.append("（研究目标待补充）")

        # 1.4 问题清单
        lines.append("")
        lines.append("### 1.4 问题清单")
        lines.append("")
        if project_context.questions:
            for q in project_context.questions:
                desc = q.objective or q.original_text[:100]
                lines.append(f"- **{q.question_id}**：{desc}")
        else:
            lines.append("（问题清单待补充）")

        return "\n".join(lines)

    def _generate_problem_analysis(
        self, project_context: ProjectContext | None
    ) -> str:
        """根据问题文本自动生成问题分析段落。

        分析问题的结构、关键挑战和解题思路。
        """
        lines: list[str] = []

        if project_context is None or not project_context.questions:
            lines.append(
                "本文围绕题设对象、已知条件和所需输出展开，并以各问的实际建模记录组织后续论述。"
            )
            return "\n".join(lines)

        n_questions = len(project_context.questions)
        lines.append(
            f"本题共包含 {n_questions} 个子问题，"
            "各问的衔接关系以题目陈述、输入条件和前序计算结果为准。"
        )
        lines.append(
            "报告对每一问分别呈现已建立的数学关系、实际计算输出和可核查的验证信息，"
            "不以预设的问题类别或通用方法框架替代具体分析。"
        )

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 模型假设与符号说明
    # ------------------------------------------------------------------

    def _write_assumptions(
        self, question_results: dict[str, QuestionResult]
    ) -> str:
        """生成模型假设与符号说明（使用表格工具生成规范格式）。"""
        lines: list[str] = []

        # 2.1 模型假设
        lines.append("### 2.1 模型假设")
        lines.append("")
        lines.append(format_assumptions_table(question_results))

        # 2.2 符号说明
        lines.append("")
        lines.append("### 2.2 符号说明")
        lines.append("")
        lines.append(format_symbols_table(question_results))

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 数据说明与预处理
    # ------------------------------------------------------------------

    def _write_data_description(self, data_profile: DataProfile | None) -> str:
        """生成数据说明与预处理。"""
        lines: list[str] = []

        if data_profile is None:
            lines.append("（数据画像缺失，数据说明待补充）")
            return "\n".join(lines)

        # 3.1 数据来源
        lines.append("### 3.1 数据来源")
        lines.append("")
        if data_profile.files:
            lines.append("| 文件名 | 类型 | 大小 | 读取状态 |")
            lines.append("|--------|------|------|----------|")
            for f in data_profile.files:
                lines.append(
                    f"| {f.file_name} | {f.file_type} | "
                    f"{f.file_size} | {f.read_status} |"
                )
        else:
            lines.append("无附件数据。")

        # 3.2 数据概况
        lines.append("")
        lines.append("### 3.2 数据概况")
        lines.append("")
        if data_profile.tables:
            for t in data_profile.tables:
                sheet = f"（Sheet: {t.sheet_name}）" if t.sheet_name else ""
                lines.append(
                    f"- **{t.source_file}**{sheet}："
                    f"{t.n_rows} 行 × {t.n_cols} 列"
                )
        else:
            lines.append("无数据表。")

        # 3.3 字段说明
        lines.append("")
        lines.append("### 3.3 字段说明")
        lines.append("")
        if data_profile.fields:
            lines.append("| 字段名 | 类型 | 缺失率 | 取值范围/示例 |")
            lines.append("|--------|------|--------|---------------|")
            for f in data_profile.fields[:20]:
                # 清理取值范围中的换行符，并截断过长内容
                vr = (f.value_range or "-").replace("\n", " ").replace("|", "\\|")
                if len(vr) > 60:
                    vr = vr[:57] + "..."
                lines.append(
                    f"| {f.field_name} | {f.dtype} | "
                    f"{f.missing_rate:.2%} | {vr} |"
                )
            if len(data_profile.fields) > 20:
                lines.append(f"| ... | 共 {len(data_profile.fields)} 个字段 | | |")
        else:
            lines.append("无字段画像。")

        # 3.4 数据质量
        lines.append("")
        lines.append("### 3.4 数据质量")
        lines.append("")
        if data_profile.quality_issues:
            for issue in data_profile.quality_issues:
                lines.append(
                    f"- [{issue.severity}] {issue.source_file}: {issue.message}"
                )
        else:
            lines.append("未发现明显数据质量问题。")

        # 3.5 初步发现
        if data_profile.preliminary_findings:
            lines.append("")
            lines.append("### 3.5 初步发现")
            lines.append("")
            for finding in data_profile.preliminary_findings:
                lines.append(f"- {finding}")

        # 数据画像图表引用
        dp_figs = self._all_figures.get("data_profile", [])
        if dp_figs:
            lines.append("")
            lines.append("**数据可视化**：")
            lines.append("")
            for fig_path in dp_figs:
                self._fig_counter += 1
                fig_name = os.path.basename(fig_path)
                rel_path = f"figures/{fig_name}"
                lines.append(f"![数据画像图]({rel_path})")
                lines.append("")
                lines.append(f"**图 {self._fig_counter}**：数据画像可视化")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 小问章节引导
    # ------------------------------------------------------------------

    def _write_question_intro(
        self,
        question_results: dict[str, QuestionResult],
        blocked: dict[str, QuestionResult] | None = None,
    ) -> str:
        """生成各小问章节的引导段落。"""
        n = len(question_results)
        blocked = blocked or {}

        # 汇总对比表
        comparison_table = format_comparison_table(question_results)
        comparison_table = self._strip_table_title(comparison_table)
        self._tbl_counter += 1

        lines: list[str] = [
            f"本章汇集 {n} 个子问题的建模与求解记录。每问只呈现与该问相关的"
            "模型关系、实际求解结果、图表和验证证据；章节内的叙述顺序由材料本身决定。",
            "",
            "各子问题的关键计算结果汇总如下表所示。",
            "",
            f"**表 {self._tbl_counter}：各子问题求解结果汇总**",
            "",
            comparison_table,
        ]

        # blocked 小问提示
        if blocked:
            blocked_ids = "、".join(sorted(blocked.keys()))
            lines.extend([
                "",
                f"> 注：小问 {blocked_ids} 因验证未通过被标记为阻塞，"
                f"相关章节仅作状态说明，未纳入结果汇总与模型评价。",
            ])

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 单问章节
    # ------------------------------------------------------------------

    def _professional_formula_lead(self, qid: str, formulation: dict) -> str:
        """Build a lead-in paragraph before formula display."""
        variables = _format_decision_variables(formulation.get("decision_variables", []))
        parameters = formulation.get("parameters") or {}
        variable_text = (
            f"其中，核心决策变量包括{', '.join(variables[:4])}；"
            if variables
            else "其中，变量和参数均由题意约束及数据字段确定；"
        )
        parameter_text = (
            f"参数体系主要由{', '.join(list(parameters.keys())[:4])}等量刻画。"
            if parameters
            else "相关参数在数据预处理和模型计算阶段统一给定。"
        )
        return (
            f"为保证问题{qid}的模型表述具有可复现性，本文将目标函数、约束条件和关键变量统一写成数学形式。"
            f"{variable_text}{parameter_text}"
        )

    def _professional_solution_lead(
        self, qid: str, method: str, status: str, computation: dict
    ) -> str:
        """Build a polished solving paragraph based on computation status."""
        results = computation.get("results", {})
        metrics = computation.get("metrics", {})
        evidence = []
        if results:
            evidence.append("求解结果")
        if metrics:
            evidence.append("评价指标")
        evidence_text = "、".join(evidence) if evidence else "中间计算记录"

        if status in ("success", "optimal", "feasible"):
            return (
                f"基于上述{method}模型，本文按照“数据整理-参数确定-模型求解-结果校验”的流程完成问题{qid}的计算。"
                f"本节展示的{evidence_text}均由模型计算过程直接生成，避免在报告写作阶段对数值结论进行主观修饰。"
            )
        if status == "generic_stats":
            return (
                f"在当前可获得数据和求解条件下，问题{qid}首先进行描述性统计与结构化整理，"
                f"以提取后续建模所需的尺度、分布和变化特征。"
            )
        if status == "no_data":
            return (
                f"由于问题{qid}缺少足够的外部观测数据，本文采用任务条件和代表性参数完成模型演算，"
                f"并在结论部分明确该结果依赖的前提范围。"
            )
        if status in ("infeasible", "failed"):
            return (
                f"问题{qid}在当前参数与约束设定下未能直接得到可行解，因此本文将求解状态、冲突来源和可能的约束调整方向一并列出，"
                f"以避免将不可行计算误写为确定性结论。"
            )
        return (
            f"问题{qid}已完成模型计算，本节从结果表、关键指标和检验信息三个层面呈现求解过程。"
        )

    def _write_blocked_section(
        self, qid: str, result: QuestionResult
    ) -> str:
        """生成 blocked 小问的占位章节：说明阻塞原因，避免该问在报告中整节消失。

        Args:
            qid: 小问 ID。
            result: status="blocked" 的 QuestionResult。

        Returns:
            占位章节的 Markdown 文本。
        """
        lines: list[str] = []
        lines.append(f"#### {qid}.1 问题描述")
        lines.append("")
        interp = result.problem_interpretation
        if interp is not None and interp.math_task_description:
            lines.append(interp.math_task_description)
        else:
            lines.append(f"本问为小问 {qid}，未能完成求解与验证。")
        lines.append("")
        lines.append(f"#### {qid}.2 求解状态")
        lines.append("")
        lines.append("本小问在求解与验证环节未通过，已标记为**阻塞（blocked）**，"
                     "未产生可复现的数值结论。")
        lines.append("")
        error = (result.error_message or "").strip()
        if error:
            lines.append(f"**阻塞原因**：{error}")
        else:
            lines.append("**阻塞原因**：未通过 GQ 质量门验证（重试预算已耗尽）。")
        lines.append("")
        lines.append("> 说明：本小问未纳入最终结果与模型评价；"
                     "后续若重新运行，可优先针对上述阻塞原因修复后继续求解。")
        lines.append("")
        return "\n".join(lines)

    def _write_question_section(
        self, qid: str, result: QuestionResult
    ) -> PaperSection:
        """生成单个小问的完整章节。

        包含：问题描述、方法选择、模型建立、求解与结果、结果检验、结论。
        集成图表工具和可视化工具生成规范的图表。
        文字描述采用学术报告风格，充分阐述建模思路和结果分析。
        """
        lines: list[str] = []
        formulas: list[str] = []
        figures: list[str] = list(result.figures)
        tables: list[str] = list(result.tables)

        # 获取计算结果和数据准备
        computation = dict(_as_mapping(result.computation, f"{qid}.computation"))
        computation["results"] = _as_mapping(
            computation.get("results", {}),
            f"{qid}.computation.results",
        )
        computation["metrics"] = _as_mapping(
            computation.get("metrics", {}),
            f"{qid}.computation.metrics",
        )
        data_prep = _as_mapping(result.data_preparation, f"{qid}.data_preparation")
        feature_names = data_prep.get("feature_names", []) if data_prep else []

        # 获取题型和方法信息
        interp = result.problem_interpretation
        findings = _as_mapping(result.findings, f"{qid}.findings")

        # --- 问题描述 ---
        lines.append(f"#### {qid}.1 问题描述")
        lines.append("")
        if interp is not None:
            # 基于 ProblemInterpretation 生成问题描述
            if interp.math_task_description:
                lines.append(interp.math_task_description)
                lines.append("")
            else:
                lines.append(
                    "本问围绕题设给定的对象、条件、约束和目标建立可计算的数学关系。"
                )
                lines.append("")

            # 利用 interpretation 的丰富字段生成分析
            analysis_parts: list[str] = []
            if interp.decision_variables:
                analysis_parts.append(
                    f"涉及的决策变量包括{', '.join(_format_decision_variables(interp.decision_variables)[:5])}等"
                )
            if interp.objective_function:
                analysis_parts.append(
                    f"优化目标为{interp.objective_function}"
                )
            if interp.constraints:
                analysis_parts.append(
                    f"需考虑{'; '.join(interp.constraints[:3])}等约束条件"
                )
            if analysis_parts:
                lines.append(
                    f"本问需明确{ '，'.join(analysis_parts)}。"
                )
                lines.append("")
        else:
            lines.append("（问题理解待补充）")

        # --- 建模思路 ---
        formulation_preview = dict(
            _as_mapping(result.formulation, f"{qid}.formulation")
        )
        lines.append("")
        lines.append(f"#### {qid}.2 建模思路")
        lines.append("")

        objective_and_constraints_parts: list[str] = []
        if formulation_preview.get("objective_function"):
            objective_and_constraints_parts.append(
                f"目标：{formulation_preview['objective_function']}"
            )
        constraints_preview = formulation_preview.get("constraints", [])
        if constraints_preview:
            shown_constraints = [
                str(c) for c in constraints_preview[:5] if str(c).strip()
            ]
            if shown_constraints:
                objective_and_constraints_parts.append(
                    "约束：" + "；".join(shown_constraints)
                )

        approach_analysis = self._llm_write_prompt(
            "paper_approach_section",
            question_text=(
                interp.math_task_description if interp and interp.math_task_description
                else f"小问 {qid}"
            ),
            model_description=formulation_preview.get("description", ""),
            decision_variables=_format_decision_variables(
                formulation_preview.get("decision_variables", [])
            ),
            objective_and_constraints="\n".join(objective_and_constraints_parts)
            or "（未记录）",
            template_guide=self._template_guide,
            writing_guide=self._writing_guide,
        )
        if approach_analysis:
            lines.append(approach_analysis)
        else:
            # 回退：用模型设计描述组织建模思路（无内部流程词汇）
            description = _public_model_label(formulation_preview)
            if description != "本题数学模型":
                lines.append(
                    f"本问的核心任务是{interp.math_task_description if interp else '求解题设要求的目标量'}。"
                    f"为此，将题设对象、条件与约束抽象为数学关系，"
                    f"建立{description}，并通过数值计算完成求解。"
                )
            else:
                lines.append(
                    "本问将题设对象、条件与约束抽象为可计算的数学关系："
                    "先明确刻画对象状态与过程的变量，再依据题设条件建立变量之间的"
                    "数学关系与约束边界，最后通过数值计算得到题设要求的目标量。"
                )
        lines.append("")

        # --- 模型建立 ---
        lines.append("")
        lines.append(f"#### {qid}.3 模型建立")
        lines.append("")
        formulation = formulation_preview
        formulation["parameters"] = _as_mapping(
            formulation.get("parameters", {}),
            f"{qid}.formulation.parameters",
        )
        if formulation:
            # 输出 formulation 描述（去重，避免多问重复相同描述）
            desc = formulation.get("description", "")
            if desc and desc not in self._shown_conclusions:
                self._shown_conclusions.add(desc)
                lines.append(desc)
                lines.append("")

            # LLM 起草模型建立段落（失败回退到证据式描述）
            model_analysis = self._llm_write_prompt(
                "paper_model_section",
                question_text=(
                    interp.math_task_description if interp and interp.math_task_description
                    else f"小问 {qid}"
                ),
                decision_variables=_format_decision_variables(
                    formulation.get("decision_variables", [])
                ),
                objective_function=formulation.get("objective_function", ""),
                constraints=formulation.get("constraints", []),
                parameters=formulation.get("parameters", {}),
                template_guide=self._template_guide,
                writing_guide=self._writing_guide,
            )
            if not model_analysis:
                model_analysis = _write_model_from_evidence(qid, formulation)
            if model_analysis:
                lines.append(model_analysis)
                lines.append("")

            decision_variables = _format_decision_variables(
                formulation.get("decision_variables", [])
            )
            if decision_variables:
                lines.append(
                    f"**决策变量**：{', '.join(decision_variables)}"
                )
                lines.append("")

            # 使用 LaTeX 公式工具生成规范公式
            # 将 ProjectContext 转换为 dict（函数期望 dict 接口）
            ctx_dict = None
            if self._project_context is not None:
                ctx_dict = {
                    "problem_description": self._project_context.problem_text,
                    "background_summary": self._project_context.background_summary,
                }
            # 传递方法名以便公式生成器选择方法特定的公式
            if ctx_dict is None:
                ctx_dict = {}
            latex_formulas = generate_latex_formula(
                formulation, qid, ctx_dict
            )
            if latex_formulas:
                lines.append(self._professional_formula_lead(qid, formulation))
                lines.append("")
                lines.append("**数学模型**：")
                lines.append("")
                for label, formula in latex_formulas:
                    # 添加公式编号
                    self._formula_counter += 1
                    numbered_formula = f"{formula} \\tag{{{self._formula_counter}}}"
                    lines.append(f"**{label}**：")
                    lines.append("")
                    lines.append(f"$$ {numbered_formula} $$")
                    lines.append("")
                    formulas.append(formula)

            # 检查 LaTeX 公式是否已包含约束条件（避免重复展示）
            has_constraint_in_latex = any(
                "约束" in label or "非负" in label
                for label, _ in latex_formulas
            )

            # 约束条件（仅当 LaTeX 公式未包含时，展示原始数学约束）
            if not has_constraint_in_latex and formulation.get("constraints"):
                math_constraints = [
                    c for c in formulation["constraints"]
                    if _is_mathematical_constraint(c)
                ]
                if math_constraints:
                    lines.append("**约束条件**：")
                    lines.append("")
                    for c in math_constraints:
                        # 将 Unicode 符号转换为 LaTeX
                        c_latex = _unicode_to_latex(c)
                        self._formula_counter += 1
                        numbered_c = f"{c_latex} \\tag{{{self._formula_counter}}}"
                        lines.append(f"$$ {numbered_c} $$")
                        lines.append("")
                        formulas.append(c_latex)

            # 参数说明
            params = formulation.get("parameters", {})
            if params:
                self._tbl_counter += 1
                lines.append(f"**表 {self._tbl_counter}：模型参数说明**")
                lines.append("")
                lines.append("| 参数 | 含义 |")
                lines.append("|------|------|")
                for param_name, param_desc in params.items():
                    lines.append(f"| {param_name} | {param_desc} |")
                lines.append("")
        else:
            lines.append("（模型表述待补充）")

        # --- 求解与结果 ---
        lines.append(f"#### {qid}.4 求解与结果")
        lines.append("")
        status = computation.get("status", "unknown")
        results = computation.get("results", {})
        metrics = computation.get("metrics", {})

        # 根据计算状态生成学术性描述
        if status in ("success", "optimal", "feasible"):
            lines.append("计算结果表明，模型在当前数据与约束条件下能够形成稳定的数值输出。")
        elif status == "generic_stats":
            lines.append(
                "统计结果用于刻画样本的基本分布和变量关系，"
                "为后续模型解释提供量化依据。"
            )
        elif status == "no_data":
            lines.append("因此，相关结论应理解为在代表性参数假设下的模型推演结果。")
        elif status in ("infeasible", "failed"):
            lines.append("该结果提示当前约束体系或参数组合存在需要进一步放松或校准的部分。")
        lines.append("")

        # 求解结果表（使用表格工具）
        q_tables: list[str] = []
        if results and results.get("optimal_solution"):
            self._tbl_counter += 1
            lines.append(f"**表 {self._tbl_counter}：问题 {qid} 最优解**")
            lines.append("")
            sol_table = _render_table_safely(
                f"问题 {qid} 最优解表",
                lambda: format_solution_table(computation, qid, feature_names),
            )
            # 去除工具自带的标题行
            sol_table = self._strip_table_title(sol_table)
            lines.append(sol_table)
            q_tables.append(sol_table)
            lines.append("")

        # 数据摘要表（蒙特卡洛等）
        if results and ("simulation" in results or "data_summary" in results):
            self._tbl_counter += 1
            lines.append(f"**表 {self._tbl_counter}：问题 {qid} 数据统计**")
            lines.append("")
            ds_table = _render_table_safely(
                f"问题 {qid} 数据统计表",
                lambda: format_data_summary_table(computation, qid),
            )
            ds_table = self._strip_table_title(ds_table)
            lines.append(ds_table)
            q_tables.append(ds_table)
            lines.append("")

        # 关键指标表（仅保留可中文化的公共指标；无可展示行时跳过该表）
        metrics_table = _render_table_safely(
            f"问题 {qid} 关键指标表",
            lambda: format_metrics_table(computation, qid),
        )
        if metrics_table:
            metrics_table = self._strip_table_title(metrics_table)
            self._tbl_counter += 1
            lines.append(f"**表 {self._tbl_counter}：问题 {qid} 关键指标**")
            lines.append("")
            lines.append(metrics_table)
            q_tables.append(metrics_table)
            lines.append("")

        # 结果分析段落：LLM 起草，失败时回退到证据式摘要
        llm_result_material = _build_llm_result_material(
            results,
            metrics,
            q_tables,
            self._all_figures.get(qid, []),
        )
        result_analysis = self._llm_write_prompt(
            "paper_result_section",
            question_text=(
                interp.math_task_description if interp and interp.math_task_description
                else f"小问 {qid}"
            ),
            status=status,
            results=llm_result_material,
            metrics="已并入上方结果写作材料摘要",
            tables="\n\n".join(q_tables)[:1500],
            template_guide=self._template_guide,
            writing_guide=self._writing_guide,
        )
        lines.append(result_analysis or _write_results_from_evidence(qid, computation))
        lines.append("")

        # 补充数值结果：仅在 LLM 结果分析缺失时作为确定性安全网，
        # 且只列出能完整中文化的标量结论，杜绝原始键名与路径泄漏
        if result_analysis is None:
            extra_pairs = public_evidence_pairs(results, max_items=8) or \
                public_evidence_pairs(metrics, max_items=8)
            if extra_pairs:
                lines.append("**关键数值结果**：")
                lines.append("")
                for label, value in extra_pairs:
                    lines.append(f"- {label}：{value}")
                lines.append("")

        if computation.get("error"):
            err_text = _remove_internal_writing_terms(
                str(computation["error"])
            ).strip()
            if len(err_text) > 120:
                err_text = err_text[:120] + "……"
            if err_text:
                lines.append("")
                lines.append(f"**求解异常说明**：{err_text}")

        # 数据准备说明（过滤代码细节，仅保留学术性描述）
        if data_prep and data_prep.get("data_source"):
            lines.append("")
            lines.append("**数据说明**：")
            lines.append("")
            lines.append(
                f"本研究使用的数据来源于 {data_prep.get('data_source', '附件数据')}。"
                f"在数据预处理阶段，对原始数据进行了清洗、筛选和格式化处理，"
                f"提取了建模所需的关键变量和参数。"
            )

        # 图表引用（嵌入实际 PNG 图片）
        qid_figs = self._all_figures.get(qid, [])
        if qid_figs:
            lines.append("")
            lines.append("**可视化结果**：")
            lines.append("")
            for fig_path in qid_figs:
                self._fig_counter += 1
                fig_name = os.path.basename(fig_path)
                rel_path = f"figures/{fig_name}"
                # 根据文件名推断图注
                caption = self._infer_figure_caption(fig_name, qid)
                lines.append(f"![{caption}]({rel_path})")
                lines.append("")
                lines.append(f"**图 {self._fig_counter}**：{caption}")
                lines.append("")
                figures.append(rel_path)

        # --- 结果检验 ---
        lines.append(f"#### {qid}.5 结果检验")
        lines.append("")
        validation = _as_mapping(result.validation, f"{qid}.validation")
        if validation:
            # 检验分析段落
            lines.append(_validation_summary_from_evidence(qid, validation))
            lines.append("")

            self._tbl_counter += 1
            val_table = format_validation_table(validation, qid)
            val_table = self._strip_table_title(val_table)
            lines.append(f"**表 {self._tbl_counter}：问题 {qid} 验证结果**")
            lines.append("")
            lines.append(val_table)
        else:
            lines.append(_validation_summary_from_evidence(qid, {}))

        # --- 结论 ---
        lines.append("")
        lines.append(f"#### {qid}.6 结论")
        lines.append("")
        key_result = findings.get("key_result", "")

        # 学术性结论段落
        lines.append(_write_conclusion_from_evidence(qid, computation))
        lines.append("")

        # 关键结果（源端已过滤内部键；此处再做防御性清洗与小数位规范）
        public_key_result = _public_conclusion_line(key_result) if key_result else None
        if public_key_result:
            import re as _re

            def _fmt_key_result(text: str) -> str:
                def _replace_float(m):
                    val = float(m.group(0))
                    return f"{val:.4f}"
                return _re.sub(r"\d+\.\d{6,}", _replace_float, text)

            public_key_result = _fmt_key_result(public_key_result)
            lines.append(f"**关键结果**：{public_key_result}")

        # 可复用结论（过滤内部技术结论、跨问题引用与原始键名）
        if result.reusable_summary is not None:
            conclusions = result.reusable_summary.verified_conclusions
            filtered_conclusions: list[str] = []
            for c in conclusions:
                # 跳过已在前问展示过的结论
                if c in self._shown_conclusions:
                    continue
                # 跨问继承的结论行不再重复展示
                if c.startswith("问题 ") and not c.startswith(f"问题 {qid}"):
                    continue
                # 翻译原始键名 / 丢弃内部流程行
                public_line = _public_conclusion_line(c)
                if not public_line:
                    continue
                filtered_conclusions.append(public_line)
                self._shown_conclusions.add(c)
            if filtered_conclusions:
                lines.append("")
                lines.append("**主要发现**：")
                lines.append("")
                for c in filtered_conclusions:
                    lines.append(f"- {c}")

        # 局限（去重 + 过滤内部引用 + 过滤代码细节）
        if result.limitations:
            seen_lims: set[str] = set()
            filtered_lims: list[str] = []
            for lim in result.limitations:
                # 跳过内部技术引用
                if any(kw in lim for kw in ["Phase", "题型验证待", "待 Phase"]):
                    continue
                # 跳过代码级细节与流程状态标记
                if any(kw in lim for kw in ["确定性代码", "可复现", "⚠", "计算状态"]):
                    continue
                # 去重
                lim_normalized = lim.replace("：", ":").strip()
                if lim_normalized not in seen_lims:
                    seen_lims.add(lim_normalized)
                    filtered_lims.append(lim)
            if filtered_lims:
                lines.append("")
                lines.append("**模型局限**：")
                lines.append("")
                for lim in filtered_lims:
                    lines.append(f"- {lim}")

        content = "\n".join(lines)

        return PaperSection(
            section_id=f"4.{qid}",
            title=f"问题 {qid}",
            content=content,
            question_id=qid,
            figures=figures,
            tables=tables,
            formulas=formulas,
            order=40,
        )

    # ------------------------------------------------------------------
    # 模型评价、优缺点与推广
    # ------------------------------------------------------------------

    def _write_evaluation(
        self, question_results: dict[str, QuestionResult]
    ) -> str:
        """生成模型评价、优缺点与推广。"""
        lines: list[str] = []

        lines.extend([
            "### 5.1 模型总结",
            "",
            "本节仅归纳各问已经展示的模型要素、计算结果和验证记录，不引入内部的方法选择或题型标签。",
            "",
            "### 5.2 适用范围与局限",
            "",
        ])
        seen: set[str] = set()
        for qid, result in sorted(question_results.items()):
            for limitation in result.limitations:
                if limitation and limitation not in seen and "Phase" not in limitation:
                    seen.add(limitation)
                    lines.append(f"- [{qid}] {limitation}")
        if not seen:
            lines.append("- 各问的适用范围以题设条件、输入数据和正文中声明的模型假设为限。")

        directions: list[str] = []
        for qid, result in sorted(question_results.items()):
            if result.reusable_summary is None:
                continue
            for direction in result.reusable_summary.improvement_directions:
                if direction and "Phase" not in direction:
                    directions.append(f"- [{qid}] {direction}")
        if directions:
            lines.extend(["", "### 5.3 可进一步完善的方向", "", *directions])
        return "\n".join(lines)

    def _write_references_text(
        self, question_results: dict[str, QuestionResult]
    ) -> str:
        """生成参考文献章节文本。

        优先由 LLM 依据各问实际模型描述著录 GB/T 7714 格式文献；
        无可用方法材料或 LLM 失败时回退为待补充占位。
        """
        method_parts: list[str] = []
        seen: set[str] = set()
        for qid, result in sorted(question_results.items()):
            formulation = _as_mapping(result.formulation, f"{qid}.formulation")
            desc = str(formulation.get("description", "")).strip()
            if desc and desc not in seen:
                seen.add(desc)
                method_parts.append(f"- 问题 {qid}：{desc[:200]}")
        if not method_parts:
            return "（参考文献待补充）"

        refs_llm = self._llm_write_prompt(
            "paper_references",
            method_material="\n".join(method_parts),
        )
        if refs_llm:
            import re as _re

            entries: list[str] = []
            for raw_line in refs_llm.splitlines():
                line = raw_line.strip()
                # 去掉 LLM 可能自带的 bullet 与编号，编号由系统统一添加
                line = _re.sub(r"^[-•*\s]+", "", line)
                line = _re.sub(r"^\[\d+\]\s*", "", line)
                if line:
                    entries.append(line)
            if entries:
                return "\n".join(f"[{i}] {e}" for i, e in enumerate(entries, 1))
        return "（参考文献待补充）"

    # ------------------------------------------------------------------
    # 附录
    # ------------------------------------------------------------------

    def _write_appendix(
        self, question_results: dict[str, QuestionResult]
    ) -> str:
        """生成附录章节文本。"""
        lines: list[str] = []
        lines.append("### 7.1 代码")
        lines.append("")
        lines.append("各小问求解代码见产物目录 questions/ 下对应子目录。")

        lines.append("")
        lines.append("### 7.2 补充数据")
        lines.append("")
        has_content = False
        for qid in sorted(question_results.keys()):
            result = question_results[qid]
            if result.figures or result.tables:
                has_content = True
                lines.append(f"**问题 {qid}**：")
                for fig in result.figures:
                    lines.append(f"- {fig}")
                for tbl in result.tables:
                    lines.append(f"- {tbl}")
                lines.append("")
        if not has_content:
            lines.append("（无补充数据）")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 组装完整文本
    # ------------------------------------------------------------------

    def _assemble_full_text(
        self, sections: list[PaperSection], abstract: str,
        revision_notes: str = "",
    ) -> str:
        """将章节和摘要组装为完整 Markdown 文本。

        优化：避免过多空行，每个章节之间恰好一个空行。
        """
        parts: list[str] = []

        # 标题
        parts.append(f"# {self._title}")
        parts.append("")

        # 摘要
        parts.append("## 摘要")
        parts.append("")
        parts.append(abstract)
        parts.append("")

        # 各章节（按 order 排序）
        sorted_sections = sorted(sections, key=lambda s: s.order)
        for section in sorted_sections:
            # 问题子章节（如"问题 q1"）用 ###，主章节用 ##
            if section.question_id is not None:
                parts.append(f"### {section.title}")
            else:
                parts.append(f"## {section.title}")
            parts.append("")
            if section.content:
                # 清理内容中过多的连续空行（最多保留一个）
                content = section.content
                while "\n\n\n" in content:
                    content = content.replace("\n\n\n", "\n\n")
                parts.append(content)
            parts.append("")

        # 修订说明（如果有）
        if revision_notes:
            parts.append(revision_notes)
            parts.append("")

        return _remove_internal_writing_terms("\n".join(parts))

    def _build_revision_notes(self, review_report: Any, gf_retry: int) -> str:
        """构建修订说明（基于审查反馈）。"""
        if gf_retry == 0 or review_report is None:
            return ""

        lines: list[str] = []
        lines.append("## 修订说明")
        lines.append("")
        lines.append(f"本文经过第 {gf_retry} 轮审查修订，主要改进如下：")
        lines.append("")

        for issue in review_report.issues:
            severity = issue.severity
            category = issue.category
            message = issue.message
            suggested_fix = issue.suggested_fix or ""

            if severity == "critical":
                lines.append(f"- **[严重-{category}]** {message}")
                if suggested_fix:
                    lines.append(f"  - 处理：{suggested_fix}")
            elif severity == "major":
                lines.append(f"- **[重要-{category}]** {message}")
                if suggested_fix:
                    lines.append(f"  - 改进：{suggested_fix}")

        lines.append("")
        lines.append(
            "注：部分计算状态受限于确定性求解器的适用范围，"
            "实际竞赛中建议结合具体问题特征选择或定制求解器。"
        )

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    def _infer_figure_caption(self, fig_name: str, qid: str) -> str:
        """根据文件名推断图注。"""
        name_lower = fig_name.lower()
        if "solution_bar" in name_lower:
            return f"问题 {qid} 最优解分布"
        elif "allocation_pie" in name_lower:
            return f"问题 {qid} 资源分配占比"
        elif "mc_distribution" in name_lower:
            return f"问题 {qid} 蒙特卡洛模拟结果分布"
        elif "confidence_interval" in name_lower:
            return f"问题 {qid} 置信区间"
        elif "scenario_objectives" in name_lower:
            return f"问题 {qid} 各场景目标值"
        elif "pred_vs_actual" in name_lower:
            return f"问题 {qid} 实际值与预测值对比"
        elif "residual_plot" in name_lower:
            return f"问题 {qid} 残差分析"
        elif "comparison_objectives" in name_lower:
            return "各子问任务标值对比"
        elif "deterministic_vs_stochastic" in name_lower:
            return "确定性 vs 不确定性优化对比"
        elif "data_table_sizes" in name_lower:
            return "数据表规模"
        elif "field_missing_rates" in name_lower:
            return "字段缺失率"
        return f"问题 {qid} 可视化结果"

    @staticmethod
    def _strip_table_title(table_text: str) -> str:
        """去除表格工具自带的首行标题（如 **表：问题 xxx 最优解**）。

        表格工具生成的 Markdown 表格通常以 **表：...** 开头，
        后跟空行和实际表格。PaperWriter 会自行添加编号标题，
        因此需要去除工具自带的标题以避免重复。
        """
        lines = table_text.split("\n")
        # 找到第一个表格行（以 | 开头）
        table_start = 0
        for i, line in enumerate(lines):
            if line.strip().startswith("|"):
                table_start = i
                break
        # 如果表格前面有标题行，去除它们
        if table_start > 0:
            return "\n".join(lines[table_start:])
        return table_text

    @staticmethod
    def _fmt_value(v: Any) -> str:
        """格式化数值用于展示，浮点数保留 4 位小数。"""
        if isinstance(v, bool):
            return "是" if v else "否"
        if isinstance(v, float):
            return _fmt_numeric(v)
        if isinstance(v, int):
            return str(v)
        if hasattr(v, "tolist"):
            return _fmt_numeric(v)
        if isinstance(v, list):
            items = [PaperWriter._fmt_value(item) for item in v[:10]]
            if len(v) > 10:
                items.append("...")
            return "[" + ", ".join(items) + "]"
        if isinstance(v, dict):
            items = [
                f"{k}: {PaperWriter._fmt_value(val)}"
                for k, val in list(v.items())[:5]
            ]
            if len(v) > 5:
                items.append("...")
            return "{" + ", ".join(items) + "}"
        if v is None:
            return "-"
        return str(v)

    def _derive_title(self, project_context: ProjectContext | None) -> str:
        """从项目上下文派生报告标题。

        优先从问题文本中提取"X题"标题模式；
        无法提取时使用背景摘要首行；最后回退到默认标题。
        """
        import re

        if project_context is None:
            return "数学建模报告"

        # 尝试从 problem_text 提取标题
        if project_context.problem_text:
            text = project_context.problem_text
            clean = re.sub(r"---\s*第\s*\d+\s*页\s*---", "", text).strip()

            title_match = re.search(
                r"([A-Z])\s*题\s+(.+?)(?:\n|$)",
                clean,
            )
            if title_match:
                title_line = f"{title_match.group(1)}题 {title_match.group(2).strip()}"
                if 5 <= len(title_line) <= 80:
                    return title_line

            comp_match = re.search(r"(\d{4}\s*年.*?竞赛题目)", clean)
            if comp_match:
                return comp_match.group(1).strip()[:80]

            strategy_match = re.search(r"([\u4e00-\u9fa5]{2,15}(?:策略|问题|模型|优化|分析))", clean)
            if strategy_match:
                return strategy_match.group(1)

        if project_context.background_summary:
            summary = project_context.background_summary.strip()
            summary = re.sub(r"---\s*第\s*\d+\s*页\s*---", "", summary).strip()
            if len(summary) > 5:
                first_line = summary.split("\n")[0].strip()
                if len(first_line) > 5:
                    return first_line[:60]

        return "数学建模报告"

    def _collect_references(
        self, references_text: str
    ) -> list[str]:
        """从参考文献章节文本中解析文献条目（去掉 [n] 编号）。"""
        import re as _re

        entries: list[str] = []
        for raw_line in references_text.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("（"):
                continue
            line = _re.sub(r"^\[\d+\]\s*", "", line)
            if line:
                entries.append(line)
        return entries

    def _collect_keywords(
        self, question_results: dict[str, QuestionResult]
    ) -> list[str]:
        """Collect paper keywords from public model evidence only."""
        keywords: set[str] = set()
        for result in question_results.values():
            formulation = _as_mapping(result.formulation, "formulation")
            for variable in formulation.get("decision_variables", [])[:3]:
                if isinstance(variable, str) and variable.strip():
                    keywords.add(variable.strip())
            description = _public_model_label(formulation)
            if description != "本题数学模型":
                keywords.add(description[:24])
            objective = formulation.get("objective_function", formulation.get("objective", ""))
            if isinstance(objective, str) and objective.strip():
                keywords.add("模型求解")
        return sorted(keywords)


# ---------------------------------------------------------------------------
# LangGraph 节点封装
# ---------------------------------------------------------------------------


def write_paper_node(state: dict) -> dict:
    """LangGraph 节点：报告写作。

    读取 question_results，调用 PaperWriter，输出 paper_draft。
    修订时会读取 review_report 和 _gf_retry_count 以改进报告。

    Args:
        state: 项目状态。需要包含 question_results。
              修订时可选包含 review_report 和 _gf_retry_count。
              可选包含 output_dir 用于保存图表。

    Returns:
        状态更新字典，包含 paper_draft。
    """
    writer = PaperWriter(llm=_instrumented_llm(state))
    output_dir = state.get("output_dir", "artifacts/paper")
    paper_draft = writer.write(state, output_dir=output_dir)
    return {"paper_draft": paper_draft}
