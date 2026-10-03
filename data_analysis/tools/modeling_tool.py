"""智能体可调用的数学建模探索工具。

为统一数据分析 Agent 提供可复现的确定性探索能力，聚焦「数据间的规律和数学模型」：

  1. 选表与概况 — 样本量、数值/分类列、缺失
  2. 两两规律发现 — 在常见函数族中拟合 y = f(x)，按 R² 排序
  3. 目标建模（可选）— 给定目标列时拟合多元线性模型与标准化系数
  4. 结构结论 — 相关、趋势、共线性、样本约束

不依赖 LLM，可复现、可单测；智能体拿到结果后自行组织结论。
"""
from __future__ import annotations

import math
from typing import Any, Sequence

import numpy as np
import pandas as pd
from pydantic import JsonValue

from .data_tools import (
    _read_tables,
    _select_data_file,
    _select_data_table,
    _validate_requested_paths,
    supports_data_profile,
)

__all__ = [
    "MAX_RELATIONSHIP_LAWS",
    "explore_math_models",
    "supports_modeling_explore",
]

#: 一次探索最多返回的两两规律条数
MAX_RELATIONSHIP_LAWS = 12

#: 单条规律最少有效样本点
_MIN_PAIR_POINTS = 8

#: 多元回归最少样本量（每个特征）
_MIN_POINTS_PER_FEATURE = 8

#: 多元回归最多纳入的特征数
_MAX_FEATURES = 8

#: 强相关阈值
_STRONG_CORR = 0.7

#: 共线性阈值
_COLLINEAR_CORR = 0.95


def supports_modeling_explore(path: str) -> bool:
    """判断文件是否可进入建模探索（与数据画像共用读表能力）。"""
    return supports_data_profile(path)


# ---------------------------------------------------------------------------
# 公开入口
# ---------------------------------------------------------------------------


def explore_math_models(
    data_paths: Sequence[str],
    *,
    authorized_paths: Sequence[str],
    focus: str = "",
    target_column: str | None = None,
    file_name: str | None = None,
    sheet_name: str | None = None,
    max_relationships: int = 8,
) -> dict[str, JsonValue]:
    """对已授权表格数据做数学建模探索。

    Args:
        data_paths: 本次要探索的数据文件路径（须已在 authorized_paths 中）。
        authorized_paths: 当前 Agent 状态已授权的数据文件路径。
        focus: 自然语言探索重点，仅原样回显，便于智能体串联上下文。
        target_column: 可选目标列；提供时额外拟合多元线性模型。
        file_name: 多文件时指定文件名。
        sheet_name: Excel 多工作表时指定表名。
        max_relationships: 返回的两两规律上限（1–12）。

    Returns:
        JSON 可序列化的探索结果，含 relationship_laws / target_model /
        structure_findings / modeling_constraints。

    Raises:
        ValueError: 参数非法或无法唯一定位数据表。
        PermissionError: 路径未授权。
        FileNotFoundError: 文件不存在。
    """
    if not 1 <= max_relationships <= MAX_RELATIONSHIP_LAWS:
        raise ValueError(
            f"max_relationships 必须在 1 到 {MAX_RELATIONSHIP_LAWS} 之间"
        )

    validated = _validate_requested_paths(data_paths, authorized_paths)
    selected_path = _select_data_file(validated, file_name)
    tables = _read_tables(selected_path)
    sheet_label, frame = _select_data_table(tables, sheet_name)

    numeric_cols = _numeric_columns(frame)
    categorical_cols = [
        str(col) for col in frame.columns if str(col) not in numeric_cols
    ]

    data_summary: dict[str, Any] = {
        "file_name": selected_path.name,
        "sheet_name": sheet_label,
        "n_rows": int(len(frame)),
        "n_cols": int(frame.shape[1]),
        "numeric_columns": numeric_cols,
        "categorical_columns": categorical_cols,
        "missing_rate": _missing_rate(frame),
    }

    laws = _discover_pairwise_laws(frame, numeric_cols, limit=max_relationships)

    target_name = _resolve_target(frame, target_column)
    target_model = (
        _fit_target_model(frame, numeric_cols, target_name)
        if target_name is not None
        else None
    )

    findings = _structure_findings(frame, numeric_cols, laws, target_model)
    constraints = _modeling_constraints(frame, numeric_cols, categorical_cols)

    return {
        "status": "ok",
        "focus": (focus or "").strip(),
        "data": data_summary,
        "relationship_laws": laws,
        "target_model": target_model,
        "structure_findings": findings,
        "modeling_constraints": constraints,
    }


# ---------------------------------------------------------------------------
# 两两规律：函数族拟合
# ---------------------------------------------------------------------------


def _discover_pairwise_laws(
    frame: pd.DataFrame,
    numeric_cols: list[str],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    """在数值列两两之间拟合常见函数族，返回按 R² 排序的规律。"""
    if len(numeric_cols) < 2:
        return []

    candidates: list[dict[str, Any]] = []
    for i, y_name in enumerate(numeric_cols):
        for x_name in numeric_cols[i + 1 :]:
            for x_col, y_col in ((x_name, y_name), (y_name, x_name)):
                law = _best_fit_pair(frame, x_col, y_col)
                if law is not None:
                    candidates.append(law)

    candidates.sort(key=lambda item: (-item["r_squared"], item["x"], item["y"]))
    # 同一无序列对只保留 R² 更高的一条（避免 x↔y 双向刷屏）
    seen_pairs: set[frozenset[str]] = set()
    ranked: list[dict[str, Any]] = []
    for law in candidates:
        pair_key = frozenset({law["x"], law["y"]})
        if pair_key in seen_pairs:
            continue
        seen_pairs.add(pair_key)
        ranked.append(law)
        if len(ranked) >= limit:
            break
    return ranked


def _best_fit_pair(
    frame: pd.DataFrame,
    x_name: str,
    y_name: str,
) -> dict[str, Any] | None:
    """对 (x, y) 尝试全部函数族，返回最优一条规律；样本不足时返回 None。"""
    clean = frame[[x_name, y_name]].apply(pd.to_numeric, errors="coerce").dropna()
    if len(clean) < _MIN_PAIR_POINTS:
        return None

    x = clean[x_name].to_numpy(dtype=float)
    y = clean[y_name].to_numpy(dtype=float)
    if _near_constant(x) or _near_constant(y):
        return None

    best: dict[str, Any] | None = None
    for fitter in (
        _fit_linear,
        _fit_poly2,
        _fit_poly3,
        _fit_logarithmic,
        _fit_exponential,
        _fit_power,
    ):
        law = fitter(x, y, x_name, y_name)
        if law is None:
            continue
        if best is None or law["r_squared"] > best["r_squared"]:
            best = law

    if best is None:
        return None

    pearson = float(np.corrcoef(x, y)[0, 1]) if len(x) >= 2 else 0.0
    if not math.isfinite(pearson):
        pearson = 0.0
    best["pearson_r"] = round(pearson, 4)
    best["direction"] = (
        "positive" if pearson > 0.05 else "negative" if pearson < -0.05 else "weak"
    )
    best["n_points"] = int(len(clean))
    return best


def _fit_linear(
    x: np.ndarray, y: np.ndarray, x_name: str, y_name: str
) -> dict[str, Any] | None:
    """拟合 y = a·x + b。"""
    design = np.column_stack([x, np.ones_like(x)])
    coef, r2, rmse = _ols(x, y, design)
    if coef is None:
        return None
    a, b = float(coef[0]), float(coef[1])
    formula = f"{y_name} = " + _join_terms(
        [_signed_term(a, x_name), _signed_term(b)]
    )
    return {
        "x": x_name,
        "y": y_name,
        "family": "linear",
        "formula": formula,
        "coefficients": {"a": a, "b": b},
        "r_squared": r2,
        "rmse": rmse,
    }


def _fit_poly2(
    x: np.ndarray, y: np.ndarray, x_name: str, y_name: str
) -> dict[str, Any] | None:
    """拟合 y = a·x² + b·x + c。"""
    design = np.column_stack([x**2, x, np.ones_like(x)])
    coef, r2, rmse = _ols(x, y, design)
    if coef is None:
        return None
    a, b, c = (float(coef[0]), float(coef[1]), float(coef[2]))
    formula = f"{y_name} = " + _join_terms(
        [
            _signed_term(a, f"{x_name}^2"),
            _signed_term(b, x_name),
            _signed_term(c),
        ]
    )
    return {
        "x": x_name,
        "y": y_name,
        "family": "polynomial_2",
        "formula": formula,
        "coefficients": {"a": a, "b": b, "c": c},
        "r_squared": r2,
        "rmse": rmse,
    }


def _fit_poly3(
    x: np.ndarray, y: np.ndarray, x_name: str, y_name: str
) -> dict[str, Any] | None:
    """拟合 y = a·x³ + b·x² + c·x + d。"""
    design = np.column_stack([x**3, x**2, x, np.ones_like(x)])
    coef, r2, rmse = _ols(x, y, design)
    if coef is None:
        return None
    a, b, c, d = (float(v) for v in coef)
    formula = f"{y_name} = " + _join_terms(
        [
            _signed_term(a, f"{x_name}^3"),
            _signed_term(b, f"{x_name}^2"),
            _signed_term(c, x_name),
            _signed_term(d),
        ]
    )
    return {
        "x": x_name,
        "y": y_name,
        "family": "polynomial_3",
        "formula": formula,
        "coefficients": {"a": a, "b": b, "c": c, "d": d},
        "r_squared": r2,
        "rmse": rmse,
    }


def _fit_logarithmic(
    x: np.ndarray, y: np.ndarray, x_name: str, y_name: str
) -> dict[str, Any] | None:
    """拟合 y = a·ln(x) + b（要求 x > 0）。"""
    mask = x > 0
    if int(mask.sum()) < _MIN_PAIR_POINTS:
        return None
    log_x = np.log(x[mask])
    design = np.column_stack([log_x, np.ones_like(log_x)])
    coef, r2, rmse = _ols(log_x, y[mask], design)
    if coef is None:
        return None
    a, b = float(coef[0]), float(coef[1])
    formula = f"{y_name} = " + _join_terms(
        [_signed_term(a, f"ln({x_name})"), _signed_term(b)]
    )
    return {
        "x": x_name,
        "y": y_name,
        "family": "logarithmic",
        "formula": formula,
        "coefficients": {"a": a, "b": b},
        "r_squared": r2,
        "rmse": rmse,
    }


def _fit_exponential(
    x: np.ndarray, y: np.ndarray, x_name: str, y_name: str
) -> dict[str, Any] | None:
    """拟合 y = a·e^{b·x}（要求 y > 0，半对数线性化）。"""
    mask = y > 0
    if int(mask.sum()) < _MIN_PAIR_POINTS:
        return None
    log_y = np.log(y[mask])
    design = np.column_stack([x[mask], np.ones_like(log_y)])
    coef, r2_log, _ = _ols(x[mask], log_y, design)
    if coef is None:
        return None
    b, log_a = float(coef[0]), float(coef[1])
    a = float(math.exp(log_a))
    y_hat = a * np.exp(b * x[mask])
    r2, rmse = _fit_quality(y[mask], y_hat, n_params=2)
    return {
        "x": x_name,
        "y": y_name,
        "family": "exponential",
        "formula": f"{y_name} = {_num(a)}*exp({_num(b)}*{x_name})",
        "coefficients": {"a": a, "b": b},
        "r_squared": r2,
        "rmse": rmse,
        "log_space_r_squared": r2_log,
    }


def _fit_power(
    x: np.ndarray, y: np.ndarray, x_name: str, y_name: str
) -> dict[str, Any] | None:
    """拟合 y = a·x^b（要求 x > 0 且 y > 0，双对数线性化）。"""
    mask = (x > 0) & (y > 0)
    if int(mask.sum()) < _MIN_PAIR_POINTS:
        return None
    log_x = np.log(x[mask])
    log_y = np.log(y[mask])
    design = np.column_stack([log_x, np.ones_like(log_x)])
    coef, r2_log, _ = _ols(log_x, log_y, design)
    if coef is None:
        return None
    b, log_a = float(coef[0]), float(coef[1])
    a = float(math.exp(log_a))
    y_hat = a * np.power(x[mask], b)
    r2, rmse = _fit_quality(y[mask], y_hat, n_params=2)
    return {
        "x": x_name,
        "y": y_name,
        "family": "power",
        "formula": f"{y_name} = {_num(a)}*{x_name}^{_num(b)}",
        "coefficients": {"a": a, "b": b},
        "r_squared": r2,
        "rmse": rmse,
        "log_space_r_squared": r2_log,
    }


# ---------------------------------------------------------------------------
# 目标建模
# ---------------------------------------------------------------------------


def _resolve_target(frame: pd.DataFrame, target_column: str | None) -> str | None:
    """校验目标列必须存在且可数值化。"""
    if target_column is None:
        return None
    name = target_column.strip()
    if not name:
        raise ValueError("target_column 不能为空")
    columns = {str(col): str(col) for col in frame.columns}
    matched = columns.get(name) or next(
        (col for col in columns if col.casefold() == name.casefold()), None
    )
    if matched is None:
        raise ValueError(f"未找到目标列: {name}")
    series = pd.to_numeric(frame[matched], errors="coerce")
    if series.notna().sum() < _MIN_PAIR_POINTS:
        raise ValueError(f"目标列 {matched} 的有效数值样本不足")
    return matched


def _fit_target_model(
    frame: pd.DataFrame,
    numeric_cols: list[str],
    target_name: str,
) -> dict[str, Any] | None:
    """以目标列为因变量拟合多元线性模型，并给出标准化系数重要性。"""
    feature_cols = [col for col in numeric_cols if col != target_name]
    if not feature_cols:
        return None

    clean = frame[[target_name, *feature_cols]].apply(
        pd.to_numeric, errors="coerce"
    ).dropna()
    n_rows = len(clean)
    if n_rows < _MIN_PAIR_POINTS:
        return None

    # 按与目标的 |Pearson r| 挑选特征，剔除共线性
    scored: list[tuple[float, str]] = []
    y = clean[target_name].to_numpy(dtype=float)
    if _near_constant(y):
        return None
    for col in feature_cols:
        x = clean[col].to_numpy(dtype=float)
        if _near_constant(x):
            continue
        r = float(np.corrcoef(x, y)[0, 1])
        if not math.isfinite(r):
            continue
        scored.append((abs(r), col))
    scored.sort(key=lambda item: (-item[0], item[1]))

    selected: list[str] = []
    for _, col in scored:
        if len(selected) >= _MAX_FEATURES:
            break
        if n_rows < len(selected + [col]) * _MIN_POINTS_PER_FEATURE:
            break
        if _is_collinear_with(clean, col, selected):
            continue
        selected.append(col)

    if not selected:
        return None

    design_cols = [*selected, "__intercept__"]
    clean = clean.copy()
    clean["__intercept__"] = 1.0
    x_mat = clean[design_cols].to_numpy(dtype=float)
    coef, r2, rmse = _ols(clean[target_name].to_numpy(dtype=float), y, x_mat)
    if coef is None:
        return None

    n_params = len(selected) + 1
    adj_r2 = _adjusted_r2(r2, n_rows, n_params)
    coefficients = {
        selected[i]: float(coef[i]) for i in range(len(selected))
    }
    coefficients["intercept"] = float(coef[-1])

    # 标准化系数：系数 × 特征标准差 / 目标标准差，用于相对重要性
    y_std = float(np.std(y, ddof=0))
    importance: list[dict[str, Any]] = []
    for i, col in enumerate(selected):
        x_std = float(np.std(clean[col].to_numpy(dtype=float), ddof=0))
        std_coef = (
            float(coef[i]) * x_std / y_std if y_std > 0 else 0.0
        )
        importance.append(
            {
                "feature": col,
                "coefficient": float(coef[i]),
                "standardized_coefficient": round(std_coef, 4),
            }
        )
    importance.sort(
        key=lambda item: -abs(item["standardized_coefficient"])
    )

    terms = [
        _signed_term(coefficients[col], col) for col in selected
    ]
    terms.append(_signed_term(coefficients["intercept"]))
    formula = f"{target_name} = " + _join_terms(terms)

    return {
        "target": target_name,
        "family": "linear_ols",
        "formula": formula,
        "features": selected,
        "coefficients": coefficients,
        "feature_importance": importance,
        "r_squared": r2,
        "adj_r_squared": adj_r2,
        "rmse": rmse,
        "n_points": int(n_rows),
    }


def _is_collinear_with(
    frame: pd.DataFrame, candidate: str, selected: list[str]
) -> bool:
    """候选特征与已选特征高度线性相关时视为共线，跳过。"""
    for existing in selected:
        pair = frame[[candidate, existing]].dropna()
        if len(pair) < _MIN_PAIR_POINTS:
            continue
        r = float(np.corrcoef(pair[candidate], pair[existing])[0, 1])
        if math.isfinite(r) and abs(r) >= _COLLINEAR_CORR:
            return True
    return False


# ---------------------------------------------------------------------------
# 数值工具
# ---------------------------------------------------------------------------


def _ols(
    x_row_or_unused: np.ndarray,
    y: np.ndarray,
    design: np.ndarray,
) -> tuple[np.ndarray | None, float, float]:
    """普通最小二乘：返回 (系数, R², RMSE)；奇异或退化时返回 Nones。"""
    try:
        coef, *_ = np.linalg.lstsq(design, y, rcond=None)
    except np.linalg.LinAlgError:
        return None, 0.0, 0.0
    y_hat = design @ coef
    r2, rmse = _fit_quality(y, y_hat, n_params=design.shape[1])
    return coef, r2, rmse


def _fit_quality(
    y: np.ndarray, y_hat: np.ndarray, *, n_params: int
) -> tuple[float, float]:
    """计算 R² 与 RMSE。"""
    ss_res = float(np.sum((y - y_hat) ** 2))
    ss_tot = float(np.sum((y - np.mean(y)) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    r2 = max(0.0, min(1.0, r2))
    rmse = float(math.sqrt(ss_res / max(len(y), 1)))
    return round(r2, 4), round(rmse, 6)


def _adjusted_r2(r2: float, n_samples: int, n_params: int) -> float:
    """调整 R²，惩罚无效参数。"""
    if n_samples <= n_params:
        return 0.0
    value = 1.0 - (1.0 - r2) * (n_samples - 1) / (n_samples - n_params)
    return round(max(0.0, min(1.0, value)), 4)


def _numeric_columns(frame: pd.DataFrame) -> list[str]:
    """取出可整体数值化的列名。"""
    cols: list[str] = []
    for col in frame.columns:
        name = str(col)
        series = pd.to_numeric(frame[col], errors="coerce")
        valid = series.notna().sum()
        if valid >= _MIN_PAIR_POINTS and valid >= 0.5 * max(len(frame), 1):
            cols.append(name)
    return cols


def _missing_rate(frame: pd.DataFrame) -> float:
    """全表缺失率。"""
    total = frame.shape[0] * frame.shape[1]
    if total <= 0:
        return 0.0
    return round(float(frame.isna().sum().sum()) / total, 4)


def _near_constant(values: np.ndarray, eps: float = 1e-12) -> bool:
    """判断序列是否近似常量（无法拟合斜率类模型）。"""
    return float(np.std(values)) < eps


def _num(value: float) -> str:
    """把系数格式化为简洁可读的数字字符串。"""
    if not math.isfinite(value):
        return "0"
    if abs(value - round(value)) < 1e-9 and abs(value) < 1e6:
        return str(int(round(value)))
    return f"{value:.4g}"


def _join_terms(terms: list[str]) -> str:
    """把带符号的项拼成 `a - b + c` 形式，避免出现 `+ -`。"""
    if not terms:
        return "0"
    parts: list[str] = []
    for index, term in enumerate(terms):
        if index == 0:
            parts.append(term)
            continue
        if term.startswith("-"):
            parts.append(f"- {term[1:]}")
        else:
            parts.append(f"+ {term}")
    return " ".join(parts)


def _signed_term(coef: float, expression: str | None = None) -> str:
    """生成单项：`2.5*x` 或 `-3`；expression 为空表示常数项。"""
    magnitude = abs(coef)
    body = _num(magnitude) if expression is None else f"{_num(magnitude)}*{expression}"
    return f"-{body}" if coef < 0 else body


# ---------------------------------------------------------------------------
# 结构结论
# ---------------------------------------------------------------------------


def _structure_findings(
    frame: pd.DataFrame,
    numeric_cols: list[str],
    laws: list[dict[str, Any]],
    target_model: dict[str, Any] | None,
) -> list[str]:
    """汇总可直接写进分析结论的规律描述。"""
    findings: list[str] = []
    n_rows = len(frame)
    findings.append(
        f"样本 {n_rows} 行 × {frame.shape[1]} 列，其中数值列 {len(numeric_cols)} 个"
    )

    if laws:
        top = laws[0]
        findings.append(
            f"最强规律：{top['formula']}（{top['family']}，R²={top['r_squared']}）"
        )
        strong = [law for law in laws if law["r_squared"] >= 0.6]
        if len(strong) > 1:
            findings.append(
                f"共发现 {len(strong)} 条 R²≥0.6 的函数规律，详见 relationship_laws"
            )

    corr_pairs = _top_correlations(frame, numeric_cols, limit=3)
    for left, right, r in corr_pairs:
        if abs(r) >= _STRONG_CORR:
            direction = "正" if r > 0 else "负"
            findings.append(
                f"{left} 与 {right} 呈强{direction}线性相关（r={r:.3f}）"
            )

    if target_model is not None:
        findings.append(
            f"目标 {target_model['target']} 的多元线性模型 "
            f"R²={target_model['r_squared']}，"
            f"主要驱动：{_format_importance(target_model['feature_importance'])}"
        )

    # 时间列上的粗粒度趋势
    time_law = _time_trend_law(laws)
    if time_law is not None:
        findings.append(time_law)

    return findings


def _top_correlations(
    frame: pd.DataFrame, numeric_cols: list[str], *, limit: int
) -> list[tuple[str, str, float]]:
    """返回 |r| 最大的若干数值列对。"""
    if len(numeric_cols) < 2:
        return []
    subset = frame[numeric_cols].apply(pd.to_numeric, errors="coerce")
    corr = subset.corr(method="pearson").to_numpy()
    pairs: list[tuple[str, str, float]] = []
    names = list(numeric_cols)
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            r = corr[i, j]
            if isinstance(r, float) and math.isfinite(r):
                pairs.append((names[i], names[j], float(r)))
    pairs.sort(key=lambda item: -abs(item[2]))
    return pairs[:limit]


def _format_importance(items: list[dict[str, Any]]) -> str:
    """把特征重要性格式化为一行摘要。"""
    if not items:
        return "无"
    top = items[:3]
    return "、".join(
        f"{item['feature']}(β={item['standardized_coefficient']:+.3f})"
        for item in top
    )


def _time_trend_law(laws: list[dict[str, Any]]) -> str | None:
    """若规律中包含时间/序号类变量，给出趋势描述。"""
    time_keywords = ("时间", "日期", "年", "月", "日", "time", "date", "year", "month", "day", "index", "序号")
    for law in laws:
        x_name = law["x"].lower()
        if any(key in x_name for key in time_keywords) and law["family"] == "linear":
            a = law["coefficients"].get("a", 0.0)
            trend = "上升" if a > 0 else "下降" if a < 0 else "平坦"
            return f"{law['y']} 随 {law['x']} 呈线性{trend}趋势（斜率={a:.4g}）"
    return None


def _modeling_constraints(
    frame: pd.DataFrame,
    numeric_cols: list[str],
    categorical_cols: list[str],
) -> list[str]:
    """基于数据事实给出模型可用性硬约束。"""
    constraints: list[str] = []
    n_rows = len(frame)
    n_num = len(numeric_cols)

    if n_rows < 30:
        constraints.append(f"样本量偏小（{n_rows} 行），避免高复杂度模型与大量交互项")
    if n_num < 2:
        constraints.append("数值特征不足 2 个，难以做多变量回归，优先单变量函数拟合")

    corr_pairs = _top_correlations(frame, numeric_cols, limit=8)
    collinear = [
        (a, b, r) for a, b, r in corr_pairs if abs(r) >= _COLLINEAR_CORR
    ]
    if collinear:
        names = "、".join(f"{a}~{b}" for a, b, _ in collinear[:3])
        constraints.append(f"存在高度共线列（{names}），回归系数可能不稳定")

    missing = _missing_rate(frame)
    if missing > 0.2:
        constraints.append(f"全表缺失率 {missing:.0%}，建模前需明确缺失处理策略")

    if n_rows < n_num * _MIN_POINTS_PER_FEATURE:
        constraints.append(
            f"样本/特征比不足（{n_rows} 行 vs {n_num} 个数值列），应降维或先筛选特征"
        )

    if not constraints:
        constraints.append("数据基本面可支撑常规回归与函数拟合")
    return constraints
