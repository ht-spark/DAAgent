"""表格格式化工具 — 从计算结果生成规范的 Markdown 表格。

确定性工具，不依赖 LLM。

功能：
  1. 格式化优化结果表（决策变量取值、约束满足情况）
  2. 格式化验证报告表（检查项、通过/失败、详情）
  3. 格式化数据摘要表（统计量、缺失率）
  4. 格式化指标对比表（多问横向对比）
  5. 生成 LaTeX 公式字符串
"""
from __future__ import annotations

from typing import Any

import numpy as np

__all__ = [
    "format_solution_table",
    "format_metrics_table",
    "format_validation_table",
    "format_data_summary_table",
    "format_comparison_table",
    "format_assumptions_table",
    "format_symbols_table",
    "generate_latex_formula",
    "format_formula_with_number",
    "generate_all_tables",
    "zh_label_for_key",
    "public_evidence_pairs",
]


def _fmt_num(v: Any, decimals: int = 4) -> str:
    """安全格式化数值或结构化值，不假设序列一定由数值组成。"""
    if isinstance(v, bool):
        return "是" if v else "否"
    if isinstance(v, (int, float, np.number)):
        value = float(v)
        return f"{value:.{decimals}f}" if np.isfinite(value) else "N/A"
    if isinstance(v, np.ndarray):
        v = v.tolist()
    if isinstance(v, (list, tuple)):
        if all(isinstance(item, (int, float, np.number)) and not isinstance(item, bool) for item in v):
            shown = [f"{float(item):.{decimals}f}" for item in v[:5]]
            if len(v) > 5:
                shown.append("...")
            return "[" + ", ".join(shown) + "]"
        return _structured_summary(v)
    if isinstance(v, dict):
        return _structured_summary(v)
    return str(v)


def _structured_summary(value: Any) -> str:
    """将嵌套结果压缩为可写入报告的安全摘要。"""
    if isinstance(value, dict):
        keys = list(value)[:5]
        suffix = "，..." if len(value) > 5 else ""
        return f"{len(value)} 个字段：" + "、".join(map(str, keys)) + suffix
    if isinstance(value, (list, tuple)):
        if not value:
            return "空列表"
        first = value[0]
        if isinstance(first, dict):
            keys = list(first)[:4]
            return f"{len(value)} 条记录，字段：" + "、".join(map(str, keys))
        return f"{len(value)} 项非标量数据"
    return str(value)


# ---------------------------------------------------------------------------
# 结果键名中文化（防止原始 snake_case 键名泄漏进论文）
# ---------------------------------------------------------------------------

# 建模流程内部键 — 任何情况下都不应出现在论文正文或表格中
_INTERNAL_EVIDENCE_KEYS = {
    "model_name", "model", "method", "method_key", "canonical_method",
    "status", "message", "success", "solver", "solver_status",
    "figures", "figure", "tables", "table", "code", "code_path",
    "artifacts_dir", "validation_details", "note", "notes",
    "error", "error_message", "generation_attempts",
    "solution_code_snippet", "intermediate_values", "parameters_used",
    "model_summary", "baseline_objective", "scenario_objectives",
    "variable_count", "constraint_count",
}

# 通用建模词汇表（问题无关），用于把结果键名逐词译为中文
_KEY_WORD_ZH = {
    "effective": "有效", "total": "总", "overall": "总体",
    "average": "平均", "mean": "均值", "max": "最大", "maximum": "最大",
    "min": "最小", "minimum": "最小", "value": "值", "count": "数量",
    "num": "数量", "number": "数量", "n": "数量", "rate": "速率",
    "ratio": "比率", "time": "时间", "start": "起始", "begin": "起始",
    "end": "结束", "finish": "结束", "duration": "时长",
    "seconds": "秒", "second": "秒", "s": "秒", "minutes": "分钟",
    "minute": "分钟", "hours": "小时", "hour": "小时", "days": "天",
    "day": "天", "years": "年", "year": "年", "distance": "距离",
    "length": "长度", "width": "宽度", "height": "高度", "depth": "深度",
    "area": "面积", "volume": "体积", "mass": "质量", "weight": "权重",
    "threshold": "阈值", "tolerance": "容差", "position": "位置",
    "coordinate": "坐标", "coordinates": "坐标", "x": "横坐标",
    "y": "纵坐标", "z": "竖坐标", "initial": "初始", "final": "最终",
    "speed": "速度", "velocity": "速度", "acceleration": "加速度",
    "angle": "角度", "slope": "斜率", "intercept": "截距",
    "coefficient": "系数", "cost": "成本", "profit": "利润",
    "revenue": "收益", "price": "价格", "budget": "预算",
    "objective": "目标", "optimal": "最优", "expected": "期望",
    "worst": "最坏", "best": "最好", "case": "情形", "scenario": "场景",
    "sample": "样本", "samples": "样本", "sampled": "采样",
    "feature": "特征", "features": "特征", "error": "误差",
    "accuracy": "精度", "index": "指标", "level": "水平", "score": "得分",
    "rank": "排名", "allocation": "分配", "capacity": "容量",
    "demand": "需求", "supply": "供给", "constraint": "约束",
    "variable": "变量", "variables": "变量", "param": "参数",
    "parameter": "参数", "parameters": "参数", "solution": "解",
    "iteration": "迭代", "iterations": "迭代次数", "convergence": "收敛",
    "residual": "残差", "residuals": "残差", "prediction": "预测",
    "predicted": "预测", "forecast": "预测", "actual": "实际",
    "observed": "观测", "true": "真实", "fitted": "拟合",
    "variance": "方差", "std": "标准差", "deviation": "偏差",
    "median": "中位数", "quantile": "分位数", "percentile": "百分位",
    "interval": "区间", "confidence": "置信", "simulation": "模拟",
    "simulated": "模拟", "path": "路径", "target": "目标点",
    "source": "来源", "input": "输入", "output": "输出",
    "result": "结果", "results": "结果", "summary": "汇总",
    "utilization": "利用率", "remaining": "剩余", "available": "可用",
    "used": "已用", "free": "空闲", "unit": "单位", "coverage": "覆盖率",
    "risk": "风险", "return": "回报", "gap": "差距",
    "improvement": "改进", "reduction": "削减", "increase": "增量",
    "decrease": "减量", "upper": "上", "lower": "下", "bound": "界",
    "limit": "限度", "offset": "偏移", "scale": "尺度", "factor": "因子",
    "term": "项", "matrix": "矩阵", "vector": "向量", "norm": "范数",
    "gradient": "梯度", "step": "步长", "energy": "能量", "power": "功率",
    "force": "力", "flow": "流量", "stock": "存量", "inventory": "库存",
    "order": "订单", "orders": "订单", "quantity": "数量",
    "amount": "金额", "annual": "年度", "monthly": "月度",
    "daily": "每日", "weekly": "每周", "grid": "网格", "node": "节点",
    "edge": "边", "layer": "层", "region": "区域", "zone": "分区",
    "point": "点", "segment": "分段", "stage": "阶段", "state": "状态",
    "transition": "转移", "probability": "概率", "distribution": "分布",
    "frequency": "频数", "correlation": "相关性", "sensitivity": "敏感性",
    "robustness": "鲁棒性", "efficiency": "效率", "performance": "性能",
    "quality": "质量", "service": "服务", "waiting": "等待",
    "arrival": "到达", "departure": "离开", "population": "总量",
    "growth": "增长", "decay": "衰减", "peak": "峰值", "sum": "和",
    "difference": "差", "product": "积", "first": "第一",
    "last": "最后", "current": "当前", "next": "下一",
    "previous": "前一", "drop": "投放", "release": "释放",
}

# 已知指标键 → (中文标签, 说明)
_METRIC_LABELS = {
    "n_samples": ("样本数", "参与计算的数据行数"),
    "n_features": ("特征数", "参与计算的数值列数"),
    "objective_value": ("目标值", "优化目标函数值"),
    "optimal_objective": ("最优目标值", "优化目标函数最优值"),
    "total_allocation": ("总分配量", "所有决策变量之和"),
    "capacity_utilization": ("容量利用率", "已用容量/总容量"),
    "expected_objective": ("期望目标值", "随机优化期望最优值"),
    "objective_std": ("目标值标准差", "随机优化目标值波动"),
    "worst_case": ("最坏情况", "最坏场景目标值"),
    "robustness_ratio": ("鲁棒性比率", "最坏/期望，越接近1越鲁棒"),
    "n_scenarios": ("场景数", "蒙特卡洛模拟场景数"),
    "n_simulations": ("模拟次数", "蒙特卡洛模拟次数"),
    "simulation_seed": ("随机种子", "模拟随机种子"),
    "posterior_ratio_c": ("后验差比C", "GM(1,1)精度指标"),
    "small_error_probability": ("小误差概率P", "GM(1,1)精度指标"),
    "r_squared": ("R²", "线性回归决定系数"),
    "rmse": ("RMSE", "均方根误差"),
    "max_entropy": ("最大熵值", "熵权法指标"),
    "min_entropy": ("最小熵值", "熵权法指标"),
}


def zh_label_for_key(key: str) -> str | None:
    """把结果键名翻译为论文可用的中文标签。

    键名按 ``_`` 分词后逐词翻译；无法翻译的短记号（如数学符号
    ``d``、``x0``）原样保留，含其他未知词的键名整体丢弃，
    避免原始 snake_case 键名或领域英文词混入论文。
    """
    if not isinstance(key, str):
        return None
    normalized = key.strip()
    if not normalized:
        return None
    if normalized in _METRIC_LABELS:
        return _METRIC_LABELS[normalized][0]
    words = [w for w in normalized.split("_") if w]
    if not words:
        return None
    translated: list[str] = []
    for word in words:
        zh = _KEY_WORD_ZH.get(word.lower())
        if zh is not None:
            translated.append(zh)
        elif len(word) <= 2 and word.isalnum():
            translated.append(word)
        else:
            return None
    return "".join(translated) or None


def public_evidence_pairs(
    mapping: Any,
    max_items: int = 6,
) -> list[tuple[str, str]]:
    """从 results/metrics 映射中提取论文安全的 (中文标签, 数值) 对。

    过滤内部流程键、非标量值与非有限值；键名无法完整中文化的条目
    一律丢弃，避免原始键名或本地路径进入论文。
    """
    pairs: list[tuple[str, str]] = []
    if not isinstance(mapping, dict):
        return pairs
    for key, value in mapping.items():
        if key in _INTERNAL_EVIDENCE_KEYS:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float, np.number)):
            continue
        if isinstance(value, (float, np.floating)) and not np.isfinite(value):
            continue
        label = zh_label_for_key(str(key))
        if not label:
            continue
        pairs.append((label, _fmt_num(value)))
        if len(pairs) >= max_items:
            break
    return pairs


# ---------------------------------------------------------------------------
# 1. 优化结果表
# ---------------------------------------------------------------------------


def format_solution_table(
    computation: dict[str, Any],
    qid: str,
    feature_names: list[str] | None = None,
    max_rows: int = 15,
) -> str:
    """格式化优化求解结果为 Markdown 表格。

    当变量数超过 max_rows 时，仅展示非零变量，零变量汇总显示。

    Args:
        computation: 计算结果。
        qid: 小问 ID。
        feature_names: 变量名列表（可选）。
        max_rows: 最大展示行数（不含表头和汇总行）。

    Returns:
        Markdown 表格字符串。
    """
    results = computation.get("results", {})
    solution = results.get("optimal_solution", [])
    objective = results.get("optimal_objective", None)

    if not solution:
        return f"（问题 {qid} 无求解结果）"

    lines: list[str] = []
    lines.append("| 序号 | 决策变量 | 最优取值 | 是否非零 |")
    lines.append("|------|----------|----------|----------|")

    # 分离非零和零变量
    nonzero_items = []
    zero_items = []
    for i, val in enumerate(solution):
        name = feature_names[i] if feature_names and i < len(feature_names) else f"x_{{{i+1}}}"
        if abs(val) > 0.01:
            nonzero_items.append((i + 1, name, val, "是"))
        else:
            zero_items.append((i + 1, name, val, "否"))

    # 展示非零变量
    for seq, name, val, nz in nonzero_items[:max_rows]:
        lines.append(f"| {seq} | {name} | {_fmt_num(val)} | {nz} |")

    # 如果非零变量太多，截断
    if len(nonzero_items) > max_rows:
        lines.append(f"| ... | 另有 {len(nonzero_items) - max_rows} 个非零变量省略 | ... | ... |")

    # 汇总零变量
    if zero_items:
        lines.append(f"| - | （另有 {len(zero_items)} 个零变量） | 0.0000 | 否 |")

    # 汇总行
    total = sum(abs(v) for v in solution)
    n_nonzero = len(nonzero_items)
    lines.append(f"| **合计** | — | **{_fmt_num(total)}** | **{n_nonzero} 个非零** |")

    if objective is not None:
        lines.append("")
        lines.append(f"**最优目标值**：{_fmt_num(objective)}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 2. 指标表
# ---------------------------------------------------------------------------


def format_metrics_table(
    computation: dict[str, Any],
    qid: str,
) -> str:
    """格式化关键指标为 Markdown 表格。

    只展示能完整中文化的标量指标；内部流程键和无法翻译的原始
    snake_case 键名一律不进入论文。无可展示行时返回空字符串，
    由调用方决定是否跳过该表。
    """
    metrics = computation.get("metrics", {})

    if not metrics:
        return ""

    # 需要跳过的代码级指标（不应在报告中展示）
    _SKIP_METRIC_KEYS = {
        "solver_status", "solver", "method", "status", "message",
        "simulation_seed",  # 随机种子是代码实现细节
        "baseline_objective",  # 基线目标值是内部对比用，非报告指标
        "best_case_objective",  # 最优场景目标值是内部统计
        "scenario_objectives",  # 场景目标值列表是原始数据
        "n_features",  # 特征数是代码级信息
        "n_samples",  # 样本数是代码级信息
    }

    lines: list[str] = []
    lines.append("| 指标 | 数值 | 说明 |")
    lines.append("|----------|------|------|")

    rows = 0
    for key, value in metrics.items():
        if key in _SKIP_METRIC_KEYS or key in _INTERNAL_EVIDENCE_KEYS:
            continue
        if isinstance(value, (dict, list, tuple, np.ndarray)):
            continue
        # 跳过过长的字符串值（如求解器状态消息）
        if isinstance(value, str) and len(value) > 50:
            continue
        label = zh_label_for_key(key)
        if not label:
            continue
        desc = _METRIC_LABELS.get(key, ("", ""))[1]
        lines.append(f"| {label} | {_fmt_num(value)} | {desc} |")
        rows += 1

    if rows == 0:
        return ""
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 3. 验证报告表
# ---------------------------------------------------------------------------

# 框架检查项名 → 中文标签（检查项由 result_validator 固义生成，可完整映射）
_CHECK_LABELS = {
    "computation_recorded": "计算过程记录",
    "computation_status": "计算状态",
    "outputs_recorded": "计算输出记录",
    "model_recorded": "模型表述记录",
    "assumptions_recorded": "模型假设记录",
    "numeric_finiteness": "数值有限性核查",
}

_CATEGORY_LABELS = {
    "evidence": "证据完整性",
    "result": "结果质量",
    "model": "模型质量",
}


def format_validation_table(
    validation: dict[str, Any],
    qid: str,
) -> str:
    """格式化验证报告为 Markdown 表格。

    将嵌套的 checks 列表展开为逐条检查结果表。
    """
    if not validation:
        return f"（问题 {qid} 验证待完成）"

    lines: list[str] = []

    # 汇总信息
    summary = validation.get("summary", {})
    status = validation.get("status", "unknown")
    status_labels = {
        "passed": "通过 ✓",
        "warning": "警告 ⚠",
        "failed": "失败 ✗",
    }
    status_label = status_labels.get(status, status)

    lines.append(f"**验证状态**：{status_label}")
    if summary:
        lines.append(
            f"**检查统计**：共 {summary.get('total_checks', 0)} 项，"
            f"通过 {summary.get('passed', 0)} 项，"
            f"警告 {summary.get('warnings', 0)} 项，"
            f"错误 {summary.get('errors', 0)} 项"
        )
    lines.append("")

    # 检查明细表
    checks = validation.get("checks", [])
    if checks:
        lines.append("| 检查项 | 类别 | 结果 | 严重级别 | 说明 |")
        lines.append("|--------|------|------|----------|------|")

        for check in checks:
            name = check.get("name", "")
            category = check.get("category", "")
            passed = "✓" if check.get("passed") else "✗"
            severity = check.get("severity", "info")
            detail = str(check.get("detail", ""))

            name_label = _CHECK_LABELS.get(name, name)
            category_label = _CATEGORY_LABELS.get(category, category)

            # 严重级别中文标签
            sev_labels = {
                "info": "信息",
                "warning": "警告",
                "error": "错误",
            }
            sev_label = sev_labels.get(severity, severity)

            lines.append(
                f"| {name_label} | {category_label} | {passed} | {sev_label} | {detail} |"
            )

    # 风险列表
    risks = validation.get("risks", [])
    if risks:
        lines.append("")
        lines.append("**主要风险**：")
        for risk in risks:
            lines.append(f"- {risk}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 4. 数据摘要表
# ---------------------------------------------------------------------------


def format_data_summary_table(
    computation: dict[str, Any],
    qid: str,
) -> str:
    """格式化数据摘要统计表。

    ``data_summary`` / ``simulation`` 的结果键可能是 dict（键→统计值）、
    list（蒙特卡洛样本序列）或标量，统一防御处理：
      - dict：逐键渲染，嵌套 dict 拍平成 ``key.sub_key``；
      - list：输出描述统计（样本数/均值/中位数/标准差/最值），
        避免对样本序列调用 ``.items()`` 导致
        ``'list' object has no attribute 'items'`` 崩溃；
      - 标量：单行渲染。
    """
    results = computation.get("results", {})

    key = "data_summary" if "data_summary" in results else (
        "simulation" if "simulation" in results else None
    )
    if key is None:
        return f"（问题 {qid} 无数据摘要）"

    raw = results[key]
    lines: list[str] = []
    lines.append("| 统计量 | 值 |")
    lines.append("|--------|------|")

    if isinstance(raw, dict):
        for k, v in raw.items():
            if isinstance(v, dict):
                for sub_k, sub_v in v.items():
                    lines.append(f"| {k}.{sub_k} | {_fmt_num(sub_v)} |")
            else:
                lines.append(f"| {k} | {_fmt_num(v)} |")
    elif isinstance(raw, list):
        arr = np.asarray(
            [x for x in raw if isinstance(x, (int, float))],
            dtype=float,
        )
        if arr.size:
            lines.append(f"| 样本数 | {arr.size} |")
            lines.append(f"| 均值 | {_fmt_num(float(np.mean(arr)))} |")
            lines.append(f"| 中位数 | {_fmt_num(float(np.median(arr)))} |")
            lines.append(f"| 标准差 | {_fmt_num(float(np.std(arr)))} |")
            lines.append(f"| 最小值 | {_fmt_num(float(np.min(arr)))} |")
            lines.append(f"| 最大值 | {_fmt_num(float(np.max(arr)))} |")
        else:
            lines.append(f"| {key} | {len(raw)} 个非数值样本 |")
    else:
        lines.append(f"| {key} | {_fmt_num(raw)} |")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 5. 多问对比表
# ---------------------------------------------------------------------------


def format_comparison_table(
    question_results: dict[str, Any],
) -> str:
    """格式化多问横向对比表。"""
    qids = sorted(question_results.keys())
    if not qids:
        return "（无小问结果）"

    lines: list[str] = []
    lines.append("| 问题 | 题型 | 模型 | 求解结果 | 验证状态 |")
    lines.append("|------|------|------|----------|----------|")

    for qid in qids:
        result = question_results[qid]
        computation = result.computation if hasattr(result, "computation") else result.get("computation", {})
        interp = result.problem_interpretation if hasattr(result, "problem_interpretation") else result.get("problem_interpretation", None)
        findings = result.findings if hasattr(result, "findings") else result.get("findings", {})
        validation = result.validation if hasattr(result, "validation") else result.get("validation", {})

        task = interp.math_task if interp else "composite"
        task_labels = {
            "evaluation": "评价/排序",
            "prediction": "预测/回归",
            "optimization": "优化/规划",
            "stochastic_optimization": "随机优化",
            "classification": "分类",
            "clustering": "聚类",
            "simulation": "仿真/模拟",
            "mechanism": "机理建模",
            "composite": "综合任务",
        }
        task_label = task_labels.get(task, task)

        formulation = (
            result.formulation if hasattr(result, "formulation")
            else result.get("formulation", {})
        )
        # 方法列展示论文可读的模型名称，不暴露内部流程词汇
        model_label = str(formulation.get("description", "")).strip() if formulation else ""
        if (not model_label
                or "LLM" in model_label
                or "问题驱动建模" in model_label
                or "候选方法" in model_label):
            model_label = "本文所建模型"
        elif len(model_label) > 20:
            model_label = model_label[:20] + "…"

        # 求解结果：提取关键数值而非计算状态
        results = computation.get("results", {})
        metrics = computation.get("metrics", {})
        result_str = "—"
        if task in ("optimization", "stochastic_optimization"):
            obj = results.get("optimal_objective")
            if obj is not None:
                result_str = f"最优值 {obj:.4f}"
            elif metrics.get("expected_objective") is not None:
                result_str = f"期望值 {metrics['expected_objective']:.4f}"
        elif task == "simulation":
            n_sim = metrics.get("n_simulations")
            if n_sim is not None:
                result_str = f"{int(n_sim)} 次模拟"
        elif task == "prediction":
            r2 = metrics.get("r_squared")
            if r2 is not None:
                result_str = f"R² = {r2:.4f}"
        if result_str == "—":
            pairs = public_evidence_pairs(results, max_items=3) or \
                public_evidence_pairs(metrics, max_items=3)
            if pairs:
                result_str = "；".join(f"{label} {value}" for label, value in pairs)

        val_status = validation.get("status", "—") if validation else "—"
        val_labels = {
            "passed": "通过",
            "warning": "通过（含警告）",
            "failed": "未通过",
        }
        val_label = val_labels.get(val_status, val_status)

        lines.append(f"| {qid} | {task_label} | {model_label} | {result_str} | {val_label} |")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 6. 假设表
# ---------------------------------------------------------------------------


# 通用假设关键词 — 这些假设在多个问题中重复出现，仅在首次出现时保留
_GENERIC_ASSUMPTION_KEYWORDS = [
    "样本量有限",
    "无时间维度数据",
    "不可使用时间序列方法",
    "数值结果已由确定性代码生成",
    "可复现",
    "题型验证待",
]

# 继承前问假设的前缀 — 这些是递归引用，不含实质假设内容
_INHERIT_PREFIXES = ["继承前问假设"]


def _is_generic_assumption(text: str) -> bool:
    """判断是否为通用/递归假设（应在全局假设中只出现一次）。"""
    for kw in _GENERIC_ASSUMPTION_KEYWORDS:
        if kw in text:
            return True
    for prefix in _INHERIT_PREFIXES:
        if text.startswith(prefix):
            return True
    return False


def format_assumptions_table(
    question_results: dict[str, Any],
) -> str:
    """格式化各小问假设表（去重 + 过滤通用假设）。

    策略：
      - 通用假设（如"样本量有限"）仅在每个问题首次出现时保留
      - "继承前问假设"类递归引用不显示
      - 同一问题内的重复假设去重
    """
    qids = sorted(question_results.keys())

    lines: list[str] = []
    lines.append("| 问题 | 假设内容 |")
    lines.append("|------|----------|")

    # 全局已显示的通用假设
    global_generic_shown: set[str] = set()

    for qid in qids:
        result = question_results[qid]
        assumptions = result.assumptions if hasattr(result, "assumptions") else result.get("assumptions", [])
        seen_in_qid = set()
        for a in assumptions:
            # assumptions 可能是 dict 或 str
            if isinstance(a, dict):
                a_clean = (
                    a.get("content")
                    or a.get("description")
                    or a.get("text")
                    or str(a)
                )
            else:
                a_clean = str(a)
            a_clean = a_clean.strip()
            if not a_clean:
                continue
            # 跳过继承前问假设
            if any(a_clean.startswith(p) for p in _INHERIT_PREFIXES):
                continue
            # 跳过同问题内重复
            if a_clean in seen_in_qid:
                continue
            # 通用假设：每个只全局显示一次
            if _is_generic_assumption(a_clean):
                if a_clean in global_generic_shown:
                    continue
                global_generic_shown.add(a_clean)
            seen_in_qid.add(a_clean)
            lines.append(f"| {qid} | {a_clean} |")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 7. 符号表
# ---------------------------------------------------------------------------


def format_symbols_table(
    question_results: dict[str, Any],
) -> str:
    """格式化符号说明表（去重后）。"""
    qids = sorted(question_results.keys())

    lines: list[str] = []
    lines.append("| 符号 | 含义 | 所属问题 |")
    lines.append("|------|------|----------|")

    seen_symbols: set[str] = set()
    for qid in qids:
        result = question_results[qid]
        formulation = result.formulation if hasattr(result, "formulation") else result.get("formulation", {})
        if not formulation:
            continue

        decision_vars = formulation.get("decision_variables", [])
        params = formulation.get("parameters", {})

        for dv in decision_vars:
            # 兼容 list[str] 与 list[dict]（symbol/meaning）两种记录形式
            if isinstance(dv, dict):
                symbol = str(dv.get("symbol") or dv.get("name") or "").strip()
                meaning = str(dv.get("meaning") or dv.get("description") or "").strip()
                dv = f"{symbol}：{meaning}" if meaning else symbol
            else:
                dv = str(dv).strip()
            if not dv or dv in seen_symbols:
                continue
            seen_symbols.add(dv)
            symbol, _, meaning = dv.partition("：")
            if not meaning:
                symbol, _, meaning = dv.partition(":")
            if meaning:
                lines.append(f"| {symbol.strip()} | {meaning.strip()} | {qid} |")
            else:
                lines.append(f"| {dv} | 决策变量 | {qid} |")

        for sym, desc in params.items():
            if sym not in seen_symbols:
                seen_symbols.add(sym)
                lines.append(f"| {sym} | {desc} | {qid} |")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 8. LaTeX 公式生成
# ---------------------------------------------------------------------------


def _unicode_to_latex(text: str) -> str:
    """将 Unicode 数学符号转换为 LaTeX 命令。"""
    # 注意：多字符替换必须在前，单字符替换在后
    replacements = [
        # 多字符组合（优先替换）
        ("β₀", r"\beta_0"), ("β₁", r"\beta_1"), ("β_j", r"\beta_j"),
        ("x_ij", r"x_{ij}"), ("y_i", r"y_i"),
        # 上标/下标符号
        ("²", "^2"), ("³", "^3"), ("₀", "_0"), ("₁", "_1"), ("₂", "_2"),
        ("ₙ", "_n"), ("ₘ", "_m"), ("ₚ", "_p"),
        # 数学运算符
        ("·", r"\cdot "), ("×", r"\times "), ("÷", r"\div "), ("±", r"\pm "),
        ("≤", r"\leq"), ("≥", r"\geq"), ("≠", r"\neq"),
        ("→", r"\to"), ("←", r"\leftarrow"), ("↔", r"\leftrightarrow"),
        ("∈", r"\in"), ("∉", r"\notin"), ("∀", r"\forall"), ("∃", r"\exists"),
        ("∞", r"\infty"), ("∂", r"\partial"),
        # 大写希腊字母
        ("Σ", r"\sum"), ("∏", r"\prod"), ("∫", r"\int"),
        ("Γ", r"\Gamma"), ("Δ", r"\Delta"), ("Θ", r"\Theta"),
        ("Λ", r"\Lambda"), ("Φ", r"\Phi"), ("Ψ", r"\Psi"), ("Ω", r"\Omega"),
        # 小写希腊字母
        ("α", r"\alpha"), ("β", r"\beta"), ("γ", r"\gamma"), ("δ", r"\delta"),
        ("ε", r"\epsilon"), ("ζ", r"\zeta"), ("η", r"\eta"), ("θ", r"\theta"),
        ("λ", r"\lambda"), ("μ", r"\mu"), ("ν", r"\nu"), ("ξ", r"\xi"),
        ("π", r"\pi"), ("ρ", r"\rho"), ("σ", r"\sigma"), ("τ", r"\tau"),
        ("φ", r"\phi"), ("χ", r"\chi"), ("ψ", r"\psi"), ("ω", r"\omega"),
    ]
    result = text
    for uni, latex in replacements:
        result = result.replace(uni, latex)
    return result


def _is_mathematical_constraint(text: str) -> bool:
    """判断约束条件是否为数学表达式（而非纯文字描述）。

    纯文字约束如"误差独立同分布"不应作为公式展示。
    """
    # 包含数学运算符或关系符的视为数学约束
    math_indicators = ["≤", "≥", "=", "<", ">", "≤", "≥", "+", "-",
                       "\\leq", "\\geq", "\\sum", "\\max", "\\min"]
    has_math = any(ind in text for ind in math_indicators)
    # 纯中文（无数学符号）的视为非数学约束
    has_chinese = any("\u4e00" <= ch <= "\u9fff" for ch in text)
    return has_math or not has_chinese


def generate_latex_formula(
    formulation: dict[str, Any],
    qid: str,
    problem_context: dict | None = None,
) -> list[tuple[str, str]]:
    """从模型表述生成规范的 MathType 风格 LaTeX 公式列表。

    数据驱动生成：优先使用 formulation 中的实际目标函数、决策变量和
    约束条件生成公式；当 formulation 数据不完整时，回退到方法级别的
    通用数学公式（如线性规划标准形、回归模型等），不硬编码任何特定
    领域内容。

    Args:
        formulation: 模型表述字典，包含 math_task, objective_function,
            decision_variables, constraints 等。
        qid: 小问 ID。
        problem_context: 问题上下文字典（可选），可包含字段：
              - problem_description: 问题描述文本
              - math_task: 题型覆盖
              - alpha: 机会约束置信水平
              - n_features: 特征数（预测类）

    Returns:
        [(标签, 公式)] 列表，标签为中文描述，公式为纯 LaTeX
        （不含 $$ 或 \\[\\]）。
    """
    formulas: list[tuple[str, str]] = []
    math_task = formulation.get("math_task", "")
    # problem_context 可覆盖 math_task
    if problem_context and problem_context.get("math_task"):
        math_task = problem_context["math_task"]
    obj_func = formulation.get("objective_function", "")
    decision_vars = formulation.get("decision_variables", [])
    constraints = formulation.get("constraints", [])

    # 将目标函数中的 Unicode 符号转换为 LaTeX
    if obj_func:
        obj_func = _unicode_to_latex(obj_func)

    # 论文只展示模型构建阶段实际记录的公式。不得依据任务分类或方法名
    # 补写线性规划、回归、蒙特卡洛等通用公式，否则会掩盖模型信息缺失。
    if obj_func:
        formulas.append(("核心关系", obj_func))
    for constraint in constraints:
        if _is_mathematical_constraint(constraint):
            formulas.append(("约束条件", _unicode_to_latex(constraint)))

    return formulas


def format_formula_with_number(formula: str, number: int) -> str:
    """在公式右侧添加编号，生成 MathType 风格的带编号公式。

    使用 ``\\tag{n}`` 在公式右侧标注编号，适用于 LaTeX 行间公式
    （被 ``$$ ... $$`` 包裹后即可在 Word/MathType 中正确渲染）。

    Args:
        formula: 纯 LaTeX 公式字符串（不含 ``$$`` 或 ``\\[\\]``）。
        number: 公式编号（正整数）。

    Returns:
        带编号的纯 LaTeX 公式字符串（不含 ``$$``），形如
        ``formula \\tag{number}``。
    """
    formula = formula.strip()
    number = int(number)
    # 若已包含 \tag 则先移除避免重复编号
    if r"\tag{" in formula:
        import re

        formula = re.sub(r"\\tag\{\d+\}", "", formula).strip()
    return f"{formula} \\tag{{{number}}}"


# ---------------------------------------------------------------------------
# 9. 统一入口：生成所有表格
# ---------------------------------------------------------------------------


def generate_all_tables(
    question_results: dict[str, Any],
) -> dict[str, dict[str, str]]:
    """为所有小问生成格式化表格。

    Args:
        question_results: 所有小问结果。

    Returns:
        {qid: {table_type: markdown_table_string}} 的映射。
    """
    all_tables: dict[str, dict[str, str]] = {}

    for qid, result in question_results.items():
        computation = result.computation if hasattr(result, "computation") else result.get("computation", {})
        validation = result.validation if hasattr(result, "validation") else result.get("validation", {})
        data_prep = result.data_preparation if hasattr(result, "data_preparation") else result.get("data_preparation", {})

        tables: dict[str, str] = {}
        tables["solution"] = format_solution_table(
            computation, qid,
            data_prep.get("feature_names") if data_prep else None,
        )
        tables["metrics"] = format_metrics_table(computation, qid)
        tables["validation"] = format_validation_table(validation, qid)
        tables["data_summary"] = format_data_summary_table(computation, qid)

        all_tables[qid] = tables

    # 全局对比表
    all_tables["_global"] = {
        "comparison": format_comparison_table(question_results),
        "assumptions": format_assumptions_table(question_results),
        "symbols": format_symbols_table(question_results),
    }

    return all_tables
