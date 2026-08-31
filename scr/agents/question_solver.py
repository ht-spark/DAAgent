"""完成单个子问题的端到端求解。

协调问题澄清、问题驱动建模、模型计算和自评修订，将过程整理为标准的 QuestionResult 与可供后续子问题复用的摘要。
"""
from __future__ import annotations

from collections.abc import Mapping
import time
from typing import Any

from ..runtime.instrumented_llm import InstrumentedLLM
from ..runtime.logging import get_run_logger, log_step
from ..schemas.context import DataProfile
from ..schemas.question import (
    CurrentQuestionContext,
    ProblemInterpretation,
    QuestionResult,
    ReusableSummary,
)
from .base import BaseAgent
from .model_builder import ModelBuilder


class QuestionSolver(BaseAgent):
    """小问求解器 Agent。

    基于完整问题上下文驱动 LLM 建模、执行和质量验证。

    Args:
        llm: 可选的 LLM 客户端。
        search_tool: 兼容保留参数，当前主流程不预选方法。
        budget_manager: 可选的预算管理器，用于运行监控与 GQ 验证迭代。
    """

    def __init__(
        self,
        llm: Any | None = None,
        search_tool: Any | None = None,
        budget_manager: Any | None = None,
    ) -> None:
        super().__init__(llm=llm)
        self._builder = ModelBuilder(llm=llm, budget_manager=budget_manager)
        self._budget_manager = budget_manager

    @staticmethod
    def _normalize_decision_variables(raw: Any) -> list[str]:
        """Convert LLM variable records into report-ready display strings.

        Code-model prompts ask the LLM for objects such as ``{"symbol": "x",
        "meaning": "投放时刻", "domain": "continuous"}``, while the shared
        ``ProblemInterpretation`` contract requires ``list[str]``. Normalize at
        this boundary so downstream report writing never receives dictionaries.
        """
        if not isinstance(raw, (list, tuple)):
            raw = [raw]

        normalized: list[str] = []
        for item in raw:
            if isinstance(item, Mapping):
                symbol = str(item.get("symbol") or item.get("name") or "").strip()
                meaning = str(item.get("meaning") or item.get("description") or "").strip()
                domain = str(item.get("domain") or "").strip()
                details = "；".join(part for part in (meaning, domain) if part)
                text = f"{symbol}（{details}）" if symbol and details else symbol or details
            else:
                text = str(item).strip()
            if text:
                normalized.append(text)
        return normalized

    @staticmethod
    def _normalize_assumptions(raw: Any) -> list[dict]:
        """将假设统一规范化为 QuestionResult 要求的 list[dict]。

        兼容两种来源：
          - ``list[str]``：LLM 决策输出 / 旧 fallback 方法定义（如
            "目标函数和约束条件可线性化"）
          - ``list[dict]``：heuristic ``_format_assumptions``（含
            description / type / verifiable）

        统一为 ``{"description": ..., "type": ..., "verifiable": ...}``，
        避免 pydantic 校验失败导致整个 solve 崩溃。
        """
        normalized: list[dict] = []
        for a in raw or []:
            if isinstance(a, dict):
                entry = dict(a)
                desc = entry.get("description") or entry.get("content") or entry.get("assumption") or ""
                entry["description"] = str(desc)
                entry.setdefault("type", "method_inherent")
                entry.setdefault("verifiable", True)
                normalized.append(entry)
            elif isinstance(a, str):
                normalized.append({
                    "description": a,
                    "type": "method_inherent",
                    "verifiable": True,
                })
            else:
                normalized.append({
                    "description": str(a),
                    "type": "method_inherent",
                    "verifiable": True,
                })
        return normalized

    def solve(
        self,
        context: CurrentQuestionContext,
        data_profile: DataProfile | None = None,
        output_dir: str | None = None,
        feedback: str = "",
    ) -> QuestionResult:
        """求解当前小问。

        Args:
            context: 当前小问的上下文包。
            data_profile: 数据画像（供建模数据准备）。
            output_dir: 产物目录（LLM 生成的解题代码将保存到其 questions/<qid>/ 下）。

        Returns:
            status="validating" 的 QuestionResult。
        """
        qid = context.question_id
        logger = get_run_logger()

        # 步骤 1: 问题澄清（§5.2）
        t0 = time.monotonic()
        log_step(logger, "solve.interpret", "started", question_id=qid)
        interpretation = self._interpret_problem(context)
        log_step(
            logger,
            "solve.interpret",
            "completed",
            question_id=qid,
            duration=time.monotonic() - t0,
            detail="问题理解完成",
        )

        # LLM 直接根据题面、数据、约束和前问结论建立模型；不预选候选方法。
        decision_record = {
            "selected_method": "LLM 问题驱动建模",
            "selection_reason": "根据当前问题、数据和约束直接推理具体数学模型",
            "assumptions": interpretation.necessary_assumptions,
            "required_outputs": [],
            "validation_requirements": [],
        }
        selected_method = decision_record["selected_method"]

        # 步骤 3: 建模计算与可视化（§5.5）
        t0 = time.monotonic()
        log_step(logger, "solve.build", "started", question_id=qid)
        model_output = self._builder.build(
            context, interpretation, decision_record, data_profile,
            output_dir=output_dir,
            feedback=feedback,
        )
        comp_status = model_output["computation"].get("status", "unknown")
        build_detail = (
            f"计算状态: {comp_status}，"
            f"图表 {len(model_output.get('figures', []))} 张，"
            f"表格 {len(model_output.get('tables', []))} 张"
        )
        if comp_status == "error":
            build_detail += (
                f"，错误: {model_output['computation'].get('error', '')[:200]}"
            )
        log_step(
            logger,
            "solve.build",
            "completed",
            question_id=qid,
            duration=time.monotonic() - t0,
            detail=build_detail,
        )

        # 用 LLM 建模产物回填问题理解的空缺字段（内容来自模型设计输出，保持 LLM-only；
        # interpretation 在 build 前以中性记录创建，此处补全供报告写作与审查使用）
        intermediate = model_output["computation"].get("intermediate_values") or {}
        if not interpretation.decision_variables and intermediate.get("variables"):
            interpretation.decision_variables = self._normalize_decision_variables(
                intermediate["variables"]
            )
        if not interpretation.objective_function and intermediate.get("objective"):
            interpretation.objective_function = intermediate["objective"]

        # 步骤 4: 提取假设（统一规范化为 list[dict]，兼容 LLM list[str] 与 heuristic list[dict]）
        assumptions = self._normalize_assumptions(
            decision_record.get("assumptions", [])
        )

        # 步骤 5: 生成可复用摘要（包含实际计算结果）
        t0 = time.monotonic()
        reusable_summary = self._build_summary(
            qid, context, interpretation, decision_record, model_output
        )
        log_step(
            logger,
            "solve.summary",
            "completed",
            question_id=qid,
            duration=time.monotonic() - t0,
            detail=f"生成 {len(reusable_summary.verified_conclusions)} 条可复用结论",
        )

        # 步骤 6: 组装 QuestionResult
        computation = model_output["computation"]
        comp_status = computation.get("status", "unknown")

        # 构建发现摘要
        findings = self._build_findings(
            qid, interpretation, decision_record, model_output
        )
        result = QuestionResult(
            question_id=qid,
            status="validating",
            problem_interpretation=interpretation,
            inherited_context={"inherited_summaries": context.inherited_summaries},
            method_candidates=[],
            decision_record=decision_record,
            assumptions=assumptions,
            formulation=model_output["formulation"],
            data_preparation=model_output["data_preparation"],
            computation=computation,
            validation={},         # Phase 5 填充
            findings=findings,
            figures=model_output["figures"],
            tables=model_output["tables"],
            reusable_summary=reusable_summary,
            limitations=self._build_limitations(
                decision_record, interpretation, model_output
            ),
        )

        print(f"[solver] 小问 {qid} 求解完成 "
              f"(模型={selected_method}, computation={comp_status})")
        log_step(
            logger,
            "solve",
            "completed",
            question_id=qid,
            detail=(
                f"小问求解完成: 模型={selected_method}, computation={comp_status}"
            ),
        )
        return result

    def _build_findings(
        self,
        qid: str,
        interpretation: ProblemInterpretation,
        decision_record: dict,
        model_output: dict,
    ) -> dict:
        """构建 findings 字段，包含实际计算结果摘要。"""
        selected_method = decision_record.get("selected_method", "未知方法")
        computation = model_output["computation"]
        comp_status = computation.get("status", "unknown")
        results = computation.get("results", {})
        metrics = computation.get("metrics", {})

        findings = {
            "summary": f"小问 {qid} 建模完成: {selected_method} (状态: {comp_status})",
            "result_form": interpretation.result_form,
            "selected_method": selected_method,
            "validation_method": decision_record.get("validation_method", ""),
            "computation_status": comp_status,
            "has_numerical_results": bool(results) and comp_status == "success",
            "figures_count": len(model_output.get("figures", [])),
            "tables_count": len(model_output.get("tables", [])),
        }

        # 关键结果：只回显计算产物中的标量结论，不按预设方法键名猜重点
        if comp_status == "success":
            if "data_summary" in results:
                findings["key_result"] = "描述统计完成"
            else:
                scalar_items = [
                    f"{key}={value:.4f}" if isinstance(value, float) else f"{key}={value}"
                    for key, value in results.items()
                    if isinstance(value, (int, float, str))
                    and not isinstance(value, bool)
                    and key != "method"
                ]
                if not scalar_items:
                    scalar_items = [
                        f"{key}={value:.4f}" if isinstance(value, float) else f"{key}={value}"
                        for key, value in metrics.items()
                        if isinstance(value, (int, float, str))
                        and not isinstance(value, bool)
                    ]
                findings["key_result"] = (
                    "；".join(scalar_items[:3]) if scalar_items else "计算成功"
                )

            # 记录关键指标
            if metrics:
                findings["key_metrics"] = {
                    k: round(v, 4) if isinstance(v, float) else v
                    for k, v in list(metrics.items())[:5]
                }

        return findings

    def _interpret_problem(
        self,
        context: CurrentQuestionContext,
    ) -> ProblemInterpretation:
        """Create a neutral context record without pre-classifying the task."""
        qid = context.question_id
        relation = "independent"
        relation_desc = ""
        if context.inherited_summaries:
            relation = "inherit"
            dep_ids = [s.get("question_id", "?") for s in context.inherited_summaries]
            relation_desc = f"继承前问 {', '.join(dep_ids)} 的可复用结论"

        return ProblemInterpretation(
            question_id=qid,
            math_task="composite",
            math_task_description=context.objective or context.question_text,
            decision_variables=[],
            objective_function="",
            constraints=context.global_constraints[:],
            evaluation_metrics=[],
            result_form="",
            available_data=context.required_data[:],
            missing_data=[],
            necessary_assumptions=[],
            acceptable_simplifications=[],
            relation_to_previous=relation,
            relation_description=relation_desc,
        )

    def _build_summary(
        self,
        qid: str,
        context: CurrentQuestionContext,
        interpretation: ProblemInterpretation,
        decision_record: dict,
        model_output: dict,
    ) -> ReusableSummary:
        """构建可复用摘要（Phase 4 版本）。

        包含选中方法信息和实际计算结果，供后续小问参考。
        """
        selected_method = decision_record.get("selected_method", "未知")
        computation = model_output.get("computation", {})
        comp_status = computation.get("status", "unknown")
        results = computation.get("results", {})
        metrics = computation.get("metrics", {})
        model_name = computation.get("model_name", "") or selected_method

        conclusions = [
            f"小问 {qid} 建模完成: {model_name}",
            f"计算状态: {comp_status}",
        ]

        # 关键数值结论：只回显标量形式的计算产物，不按预设方法键名提取
        if comp_status == "success":
            for key, value in list(results.items()) + list(metrics.items()):
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    continue
                if isinstance(value, float):
                    conclusions.append(f"{key} = {value:.6f}")
                else:
                    conclusions.append(f"{key} = {value}")
                if len(conclusions) >= 8:
                    break

        # 如果继承了前问结论，也记录到可复用摘要
        if context.inherited_summaries:
            for s in context.inherited_summaries:
                if s.get("status") == "validated":
                    prev_conclusions = s.get("verified_conclusions", [])
                    conclusions.extend(prev_conclusions[:2])  # 只取前 2 条

        # 提取关键参数
        key_params: dict[str, float | str] = {}
        if metrics:
            for k, v in list(metrics.items())[:4]:
                if isinstance(v, (int, float)):
                    key_params[k] = round(float(v), 6)
                else:
                    key_params[k] = str(v)

        # 可复用数据集
        reusable_datasets = []
        data_prep = model_output.get("data_preparation", {})
        if data_prep.get("data_source"):
            reusable_datasets.append(data_prep["data_source"])

        return ReusableSummary(
            question_id=qid,
            verified_conclusions=conclusions,
            reusable_datasets=reusable_datasets,
            model_interface=model_name,
            key_parameters=key_params,
            limitations=self._build_limitations(decision_record, interpretation, model_output),
            improvement_directions=[
                "Phase 5: 完成题型验证与结果沉淀",
                "Phase 6: 整合到报告写作",
            ],
        )

    def _build_limitations(
        self,
        decision_record: dict,
        interpretation: ProblemInterpretation,
        model_output: dict | None = None,
    ) -> list[str]:
        """构建局限列表（Phase 4 版本，包含实际计算状态）。"""
        limitations: list[str] = []
        computation = {}
        if model_output:
            computation = model_output.get("computation", {})

        comp_status = computation.get("status", "unknown")

        if comp_status == "success":
            limitations.append("数值结果已由确定性代码生成，可复现")
        elif comp_status == "no_data":
            limitations.append("⚠ 无可用数据，计算未执行，结果为占位")
        elif comp_status == "insufficient_data":
            limitations.append("⚠ 数据不足，无法完成完整计算")
        elif comp_status == "error":
            limitations.append(f"⚠ 计算错误: {computation.get('error', '未知错误')[:100]}")
        elif comp_status == "stub":
            limitations.append("⚠ 当前方法为占位实现，需要具体问题建模")
        else:
            limitations.append("计算状态未知")

        # 验证待完成
        limitations.append("题型验证待 Phase 5 完成")

        # 添加方法局限
        selected_details = decision_record.get("selected_details", {})
        for con in selected_details.get("cons", [])[:2]:
            limitations.append(f"方法局限: {con}")

        # 降级标记
        if selected_details.get("degraded"):
            limitations.append("⚠ 降级选择：数据不完全满足方法要求")

        return limitations


# ---------------------------------------------------------------------------
# LangGraph 节点封装
# ---------------------------------------------------------------------------


def solve_question_node(state: dict) -> dict:
    """LangGraph 节点：小问求解。

    读取 current_context，调用 QuestionSolver，输出 current_result。

    Args:
        state: 项目状态。需要包含 current_context。

    Returns:
        状态更新字典，包含 current_result。
    """
    current_context: CurrentQuestionContext | None = state.get("current_context")
    data_profile: DataProfile | None = state.get("data_profile")
    llm = state.get("llm")
    budget_manager = state.get("budget_manager")
    output_dir = state.get("output_dir")
    retry_count = state.get("_solve_retry_count", 0)

    # 监控包装：记录 TIME/TOKEN（不阻塞）；按小问区分消耗
    inst_llm = (
        InstrumentedLLM(
            llm,
            budget_manager,
            qid_getter=lambda: state.get("current_question_id"),
        )
        if (budget_manager is not None and llm is not None)
        else llm
    )

    if current_context is None:
        return {
            "errors": [{"msg": "current_context missing in solve_question_node"}],
            "_gq_action": "blocked",
        }

    solver = QuestionSolver(
        llm=inst_llm,
        budget_manager=budget_manager,
    )

    # 每次求解都基于题面与上一轮 GQ 反馈重新推理模型，不复用候选方法列表。
    result = solver.solve(
        current_context,
        data_profile=data_profile,
        output_dir=output_dir,
        feedback=state.get("_gq_feedback", ""),
    )

    if retry_count > 0:
        result.retry_count = retry_count
        result.findings["retry_note"] = f"第 {retry_count} 次重试"

    return {"current_result": result}
