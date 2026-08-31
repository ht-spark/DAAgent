"""记录子任务的计算证据与已知风险。

职责：
  1. 对 ``QuestionResult`` 记录计算状态、输出、数值有限性、模型要素和产物
  2. 将缺口记录为风险，不按任务类别推断应有的公式、指标或试验
  3. 生成供 GQ 与报告写作使用的证据报告

设计要点：
  - 不承担归档、重试或阻塞决策；这些由 GQ 统一完成
  - 不按预测、优化、仿真等粗粒度类别套用专项检查
"""
from __future__ import annotations

from typing import Any

import numpy as np

from ..schemas.question import QuestionResult


__all__ = ["ResultValidator", "validate_result_node"]


# 检查严重级别
_SEV_INFO = "info"
_SEV_WARNING = "warning"
_SEV_ERROR = "error"

def _make_check(
    name: str,
    category: str,
    passed: bool,
    severity: str,
    detail: str,
) -> dict[str, Any]:
    """构造单条检查结果字典。"""
    return {
        "name": name,
        "category": category,
        "passed": passed,
        "severity": severity,
        "detail": detail,
    }


class ResultValidator:
    """结果验证器 Agent（Phase 5）。

    对 QuestionResult.computation 记录计算证据与已知风险，产出验证报告，
    写入 ``QuestionResult.validation``。

    Args:
        llm: 可选的 LLM 客户端（当前确定性验证不使用，保留以兼容接口）。
    """

    def __init__(self, llm: Any | None = None) -> None:
        self._llm = llm

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    def validate(self, question_result: QuestionResult) -> dict[str, Any]:
        """记录一个小问的计算证据和风险。

        Args:
            question_result: status="validating" 的小问结果包。

        Returns:
            验证报告字典，包含：
              - status: "passed" / "warning" / "failed"
              - math_task: 题型
              - checks: 检查结果列表
              - narrative: 结果叙述文本
              - risks: 风险描述列表
              - summary: 检查统计
        """
        qid = question_result.question_id
        # 键名归一化只用于统一读取结果，不推断题型或预设方法。
        if question_result.computation:
            from ..tools.result_keys import normalize_computation

            normalize_computation(question_result.computation)

        checks = self._validate_evidence(question_result)
        has_warning = any(not c["passed"] for c in checks)
        status = "warning" if has_warning else "passed"

        risks = [
            c["detail"]
            for c in checks
            if (not c["passed"]) and c["severity"] in (_SEV_WARNING, _SEV_ERROR)
        ]

        narrative = self._build_evidence_narrative(question_result, risks)

        report: dict[str, Any] = {
            "status": status,
            "math_task": "",
            "checks": checks,
            "narrative": narrative,
            "risks": risks,
            "summary": {
                "total_checks": len(checks),
                "passed": sum(1 for c in checks if c["passed"]),
                "warnings": sum(
                    1 for c in checks
                    if (not c["passed"]) and c["severity"] == _SEV_WARNING
                ),
                "errors": sum(
                    1 for c in checks
                    if (not c["passed"]) and c["severity"] == _SEV_ERROR
                ),
            },
        }

        print(
            f"[validator] 小问 {qid} 验证完成: {status} "
            f"(checks={len(checks)}, "
            f"errors={report['summary']['errors']}, "
            f"warnings={report['summary']['warnings']})"
        )

        return report

    def _validate_evidence(self, question_result: QuestionResult) -> list[dict[str, Any]]:
        """Check recorded evidence without deriving requirements from a task label."""
        computation = question_result.computation or {}
        results = computation.get("results", {})
        metrics = computation.get("metrics", {})
        formulation = question_result.formulation or {}
        status = computation.get("status", "unknown")
        checks = [
            _make_check(
                "computation_recorded", "evidence", bool(computation),
                _SEV_INFO if computation else _SEV_WARNING,
                "已记录计算过程" if computation else "未记录计算过程",
            ),
            _make_check(
                "computation_status", "evidence", status in {"success", "optimal", "feasible"},
                _SEV_INFO if status in {"success", "optimal", "feasible"} else _SEV_WARNING,
                f"计算状态: {status}",
            ),
            _make_check(
                "outputs_recorded", "evidence", bool(results or metrics),
                _SEV_INFO if results or metrics else _SEV_WARNING,
                "已记录计算输出" if results or metrics else "未记录计算输出",
            ),
            _make_check(
                "model_recorded", "evidence", bool(formulation),
                _SEV_INFO if formulation else _SEV_WARNING,
                "已记录模型表述" if formulation else "未记录模型表述",
            ),
            _make_check(
                "assumptions_recorded", "evidence", bool(question_result.assumptions),
                _SEV_INFO if question_result.assumptions else _SEV_WARNING,
                "已记录模型假设" if question_result.assumptions else "未记录模型假设",
            ),
        ]
        non_finite = [
            key for key, value in metrics.items()
            if isinstance(value, float) and not np.isfinite(value)
        ]
        checks.append(_make_check(
            "numeric_finiteness", "evidence", not non_finite,
            _SEV_INFO if not non_finite else _SEV_WARNING,
            "数值指标均为有限值" if not non_finite else f"存在非有限数值: {non_finite}",
        ))
        return checks

    @staticmethod
    def _build_evidence_narrative(
        question_result: QuestionResult, risks: list[str],
    ) -> str:
        """Build a neutral evidence summary for the report and GQ."""
        if risks:
            return "已记录计算证据与模型材料；仍需关注：" + "；".join(risks)
        return "计算证据、模型表述与假设记录完整，供子任务质量检查进一步核验。"

    # ------------------------------------------------------------------
    # 通用检查
    # ------------------------------------------------------------------


# ---------------------------------------------------------------------------
# LangGraph 节点封装
# ---------------------------------------------------------------------------


def validate_result_node(state: dict) -> dict:
    """LangGraph 节点：结果验证（Phase 5）。

    读取 ``current_result``，调用 :class:`ResultValidator` 记录计算证据，
    将验证报告写入 ``current_result.validation``，并按报告状态
    更新 ``current_result.status``：
      - passed / warning → ``validated``（warning 时风险已记录）
      - failed → 保持 ``validating``，交由 GQ 门决定重试或阻塞

    Args:
        state: 项目状态。需要包含 ``current_result``。

    Returns:
        状态更新字典，包含更新后的 ``current_result``。
    """
    current_result: QuestionResult | None = state.get("current_result")

    if current_result is None:
        return {
            "errors": [{"msg": "current_result missing in validate_result_node"}],
        }

    validator = ResultValidator()
    report = validator.validate(current_result)

    # 写入验证报告
    current_result.validation = report

    # 根据报告更新状态
    if report["status"] in ("passed", "warning"):
        current_result.status = "validated"
    else:
        # failed：保持 validating，交由 GQ 门处理重试/阻塞
        current_result.status = "validating"

    return {"current_result": current_result}
