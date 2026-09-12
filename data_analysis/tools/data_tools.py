"""面向 Agent 的确定性数据读取与增强画像工具。"""

from __future__ import annotations

import math
import os
import re
from pathlib import Path
from typing import Any, Sequence

import pandas as pd
from pydantic import JsonValue

__all__ = [
    "MAX_INSPECT_DATA_ROWS",
    "UnauthorizedDataPathError",
    "UnsupportedDataFileError",
    "inspect_data_rows",
    "profile_data",
    "supports_data_profile",
]

_CSV_SUFFIXES = frozenset({".csv"})
_EXCEL_SUFFIXES = frozenset({".xlsx", ".xls"})
_JSON_SUFFIXES = frozenset({".json", ".jsonl", ".ndjson"})
_SUPPORTED_SUFFIXES = _CSV_SUFFIXES | _EXCEL_SUFFIXES | _JSON_SUFFIXES
_TIME_KEYWORDS = frozenset(
    {"date", "datetime", "time", "timestamp", "year", "month", "day", "hour"}
)
_TIME_KEYWORDS_ZH = ("日期", "时间", "年份", "年月", "年度", "月份", "小时", "时刻")
_NON_TIME_KEYWORDS = frozenset({"id", "code", "index", "number", "no"})
_NON_TIME_KEYWORDS_ZH = ("编号", "序号", "代码", "标识", "索引", "学号", "工号", "卡号", "牌号", "批号", "型号")
_SPATIAL_KEYWORDS = frozenset({"lat", "lon", "lng", "latitude", "longitude"})
_SPATIAL_KEYWORDS_ZH = ("经度", "纬度", "东经", "北纬", "坐标x", "坐标y", "x坐标", "y坐标")
_UNIT_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"[\(\[（].*?(kg/m2|kg/㎡|密度|人均).*?[\)\]）]", "强度/密度"),
    (r"[\(\[（].*?(kg|g|吨|克|千克).*?[\)\]）]", "重量"),
    (r"[\(\[（].*?(km|m|cm|mm|米|千米|厘米|毫米).*?[\)\]）]", "长度"),
    (r"[\(\[（].*?(min|h|hour|sec|s|分|时|秒).*?[\)\]）]", "时长"),
    (r"[\(\[（].*?(%|百分|percent|percentage).*?[\)\]）]", "百分比"),
    (r"[\(\[（].*?(元|￥|¥|\$|dollar|rmb|yuan).*?[\)\]）]", "货币"),
    (r"[\(\[（].*?(℃|°c|度|温度|temp).*?[\)\]）]", "温度"),
    (r"[\(\[（].*?(hz|khz|mhz|ghz).*?[\)\]）]", "频率"),
    (r"[\(\[（].*?(w|kw|mw|瓦|千瓦).*?[\)\]）]", "功率"),
    (r"[\(\[（].*?(亿元|万元|百万|十亿|万).*?[\)\]）]", "金额量级"),
)
_CORRELATION_THRESHOLD = 0.8
_MAX_CORRELATION_COLUMNS = 40
_MAX_SAMPLE_VALUES = 5
_MAX_TOP_VALUES = 10
MAX_INSPECT_DATA_ROWS = 200


class UnauthorizedDataPathError(PermissionError):
    """请求的数据文件不在本次运行的授权范围内。"""


class UnsupportedDataFileError(ValueError):
    """数据文件格式不受当前画像工具支持。"""


def supports_data_profile(path: str | Path) -> bool:
    """判断文件是否能由确定性数据画像工具读取。"""
    return Path(path).suffix.lower() in _SUPPORTED_SUFFIXES


def profile_data(
    data_paths: Sequence[str | Path],
    *,
    authorized_paths: Sequence[str | Path],
) -> dict[str, JsonValue]:
    """读取已授权数据文件并生成增强画像。

    CSV 和 JSON 各生成一张表画像；Excel 会展开全部工作表。返回值只包含
    JSON 可序列化数据，可直接写入 ``Observation.result``。

    Args:
        data_paths: 本次需要画像的数据文件路径。
        authorized_paths: 当前 Agent 状态中已授权的数据文件路径。

    Returns:
        包含总体摘要、文件信息、表信息和字段统计的画像字典。

    Raises:
        ValueError: 未提供数据文件。
        UnauthorizedDataPathError: 请求路径不在授权范围内。
        FileNotFoundError: 已授权文件不存在或不是普通文件。
        UnsupportedDataFileError: 文件格式不受支持。
        OSError: 文件无法读取。
    """
    requested_paths = _validate_requested_paths(data_paths, authorized_paths)
    file_profiles: list[dict[str, Any]] = []

    for path in requested_paths:
        tables = _read_tables(path)
        table_profiles = [
            _profile_table(path.name, sheet_name, frame)
            for sheet_name, frame in tables.items()
        ]
        file_profiles.append(
            {
                "file_name": path.name,
                "file_type": _file_type(path.suffix.lower()),
                "size_bytes": path.stat().st_size,
                "tables": table_profiles,
            }
        )

    return {
        "summary": _summarize_profiles(file_profiles),
        "files": file_profiles,
    }


def inspect_data_rows(
    data_paths: Sequence[str | Path],
    *,
    authorized_paths: Sequence[str | Path],
    row_count: int,
    start_row: int = 1,
    file_name: str | None = None,
    sheet_name: str | None = None,
) -> dict[str, JsonValue]:
    """按指定范围读取一张表中的真实数据记录。

    行号从 1 开始且不包含表头。为控制工具结果大小，单次最多返回
    ``MAX_INSPECT_DATA_ROWS`` 行。存在多个文件或工作表时必须明确指定目标，
    避免 Agent 在含糊条件下读取错误的数据。

    Args:
        data_paths: 本次运行绑定的数据文件路径。
        authorized_paths: 当前 Agent 状态中已授权的数据文件路径。
        row_count: 希望返回的真实数据行数。
        start_row: 起始数据行号，从 1 开始。
        file_name: 可选的目标文件名，不包含目录。
        sheet_name: 可选的 Excel 工作表名称。

    Returns:
        包含字段名、实际行号、真实单元格值和分页信息的字典。

    Raises:
        ValueError: 行范围无效，或文件、工作表选择不明确或不存在。
        UnauthorizedDataPathError: 请求路径不在授权范围内。
        FileNotFoundError: 已授权文件不存在或不是普通文件。
        UnsupportedDataFileError: 文件格式不受支持。
    """
    if isinstance(row_count, bool) or not isinstance(row_count, int):
        raise ValueError("row_count 必须是整数")
    if not 1 <= row_count <= MAX_INSPECT_DATA_ROWS:
        raise ValueError(
            f"row_count 必须在 1 到 {MAX_INSPECT_DATA_ROWS} 之间"
        )
    if isinstance(start_row, bool) or not isinstance(start_row, int):
        raise ValueError("start_row 必须是整数")
    if start_row < 1:
        raise ValueError("start_row 必须大于等于 1")

    requested_paths = _validate_requested_paths(data_paths, authorized_paths)
    selected_path = _select_data_file(requested_paths, file_name)
    tables = _read_tables(selected_path)
    selected_sheet, frame = _select_data_table(tables, sheet_name)
    start_index = start_row - 1
    selected = frame.iloc[start_index : start_index + row_count]
    rows = [
        {
            "row_number": start_index + offset + 1,
            "values": [_cell_to_json(value) for value in row],
        }
        for offset, row in enumerate(selected.itertuples(index=False, name=None))
    ]
    returned_row_count = len(rows)
    end_row = start_row + returned_row_count - 1 if rows else None

    return {
        "file_name": selected_path.name,
        "sheet_name": selected_sheet,
        "columns": [str(column) for column in frame.columns],
        "total_rows": int(len(frame)),
        "start_row": start_row,
        "end_row": end_row,
        "requested_row_count": row_count,
        "returned_row_count": returned_row_count,
        "has_more_rows": bool(end_row is not None and end_row < len(frame)),
        "rows": rows,
    }


def _select_data_file(paths: list[Path], file_name: str | None) -> Path:
    if file_name is None:
        if len(paths) != 1:
            names = ", ".join(path.name for path in paths)
            raise ValueError(f"存在多个数据文件，请指定 file_name: {names}")
        return paths[0]
    normalized_name = file_name.strip()
    if not normalized_name:
        raise ValueError("file_name 不能为空")
    matches = [path for path in paths if path.name.casefold() == normalized_name.casefold()]
    if not matches:
        raise ValueError(f"未找到数据文件: {normalized_name}")
    if len(matches) > 1:
        raise ValueError(f"存在多个同名数据文件，无法唯一选择: {normalized_name}")
    return matches[0]


def _select_data_table(
    tables: dict[str, pd.DataFrame],
    sheet_name: str | None,
) -> tuple[str, pd.DataFrame]:
    if sheet_name is None:
        if len(tables) != 1:
            names = ", ".join(tables)
            raise ValueError(f"文件包含多个工作表，请指定 sheet_name: {names}")
        return next(iter(tables.items()))
    normalized_name = sheet_name.strip()
    if not normalized_name:
        raise ValueError("sheet_name 不能为空")
    matches = [name for name in tables if name.casefold() == normalized_name.casefold()]
    if not matches:
        raise ValueError(f"未找到工作表: {normalized_name}")
    selected_name = matches[0]
    return selected_name, tables[selected_name]


def _validate_requested_paths(
    data_paths: Sequence[str | Path],
    authorized_paths: Sequence[str | Path],
) -> list[Path]:
    if not data_paths:
        raise ValueError("至少需要提供一个数据文件")

    authorized_keys = {_path_key(path) for path in authorized_paths}
    validated: list[Path] = []
    seen: set[str] = set()
    for raw_path in data_paths:
        path = Path(raw_path).expanduser().resolve(strict=False)
        key = _path_key(path)
        if key not in authorized_keys:
            raise UnauthorizedDataPathError(f"数据文件未获授权: {path.name}")
        if key in seen:
            continue
        if not path.is_file():
            raise FileNotFoundError(f"数据文件不存在或不是普通文件: {path}")
        suffix = path.suffix.lower()
        if suffix not in _SUPPORTED_SUFFIXES:
            supported = ", ".join(sorted(_SUPPORTED_SUFFIXES))
            raise UnsupportedDataFileError(
                f"不支持的数据文件格式 '{suffix}'，当前支持: {supported}"
            )
        validated.append(path)
        seen.add(key)
    return validated


def _path_key(path: str | Path) -> str:
    resolved = Path(path).expanduser().resolve(strict=False)
    return os.path.normcase(str(resolved))


def _read_tables(path: Path) -> dict[str, pd.DataFrame]:
    suffix = path.suffix.lower()
    if suffix in _CSV_SUFFIXES:
        return {"": _read_csv(path)}
    if suffix in _EXCEL_SUFFIXES:
        sheets = pd.read_excel(path, sheet_name=None)
        return {str(sheet_name): frame for sheet_name, frame in sheets.items()}
    if suffix in _JSON_SUFFIXES:
        return {"": _read_json(path)}
    raise UnsupportedDataFileError(f"不支持的数据文件格式: {suffix}")


def _read_csv(path: Path) -> pd.DataFrame:
    try:
        return pd.read_csv(path, encoding="utf-8")
    except UnicodeDecodeError:
        return pd.read_csv(path, encoding="gb18030")


def _read_json(path: Path) -> pd.DataFrame:
    lines = path.suffix.lower() in {".jsonl", ".ndjson"}
    try:
        return pd.read_json(path, lines=lines)
    except ValueError:
        if lines:
            raise
        return pd.read_json(path, lines=True)


def _profile_table(
    source_file: str,
    sheet_name: str,
    frame: pd.DataFrame,
) -> dict[str, JsonValue]:
    row_count, column_count = frame.shape
    duplicate_rows = _duplicate_count(frame)
    missing_cells = int(frame.isna().sum().sum())
    total_cells = row_count * column_count
    columns = [
        _profile_column(str(column), frame.iloc[:, index], row_count)
        for index, column in enumerate(frame.columns)
    ]
    time_columns = [column["name"] for column in columns if column["is_time_column"]]
    spatial_columns = [column["name"] for column in columns if column["is_spatial"]]
    candidate_keys = [column["name"] for column in columns if column["is_candidate_key"]]
    correlated_pairs = _correlated_pairs(frame, columns)
    return {
        "source_file": source_file,
        "sheet_name": sheet_name,
        "row_count": int(row_count),
        "column_count": int(column_count),
        "missing_cells": missing_cells,
        "missing_rate": missing_cells / total_cells if total_cells else 0.0,
        "duplicate_rows": duplicate_rows,
        "duplicate_rate": duplicate_rows / row_count if row_count else 0.0,
        "candidate_keys": candidate_keys,
        "time_columns": time_columns,
        "time_coverage": [
            _time_coverage(name, frame.iloc[:, index])
            for index, name in enumerate(map(str, frame.columns))
            if name in time_columns
        ],
        "spatial_columns": spatial_columns,
        "correlated_pairs": correlated_pairs,
        "quality_flags": _quality_flags(columns),
        "columns": columns,
    }


def _profile_column(
    name: str,
    series: pd.Series,
    row_count: int,
) -> dict[str, JsonValue]:
    missing_count = int(series.isna().sum())
    non_null = series.dropna()
    dtype = _normalized_dtype(series)
    is_time_column = _detect_time_column(series, name)
    is_spatial = _detect_spatial_column(name)
    profile: dict[str, Any] = {
        "name": name,
        "dtype": dtype,
        "missing_count": missing_count,
        "missing_rate": missing_count / row_count if row_count else 0.0,
        "unique_count": _unique_count(series),
        "sample_values": [_cell_to_text(value) for value in non_null.head(_MAX_SAMPLE_VALUES)],
        "unit_hint": _detect_unit_hint(name),
        "is_time_column": is_time_column,
        "is_spatial": is_spatial,
        "is_candidate_key": bool(
            row_count > 0
            and missing_count == 0
            and _unique_count(series) == row_count
        ),
    }
    if dtype in {"integer", "float"}:
        numeric = pd.to_numeric(series, errors="coerce").dropna()
        profile["numeric_summary"] = _numeric_summary(numeric)
    elif dtype in {"string", "category", "boolean"}:
        profile["categorical_summary"] = _categorical_summary(series)
        parsed = pd.to_numeric(non_null, errors="coerce")
        profile["numeric_parseable_rate"] = (
            float(parsed.notna().mean()) if not non_null.empty else None
        )
    return profile


def _normalized_dtype(series: pd.Series) -> str:
    dtype = series.dtype
    if pd.api.types.is_bool_dtype(dtype):
        return "boolean"
    if pd.api.types.is_integer_dtype(dtype):
        return "integer"
    if pd.api.types.is_float_dtype(dtype):
        return "float"
    if pd.api.types.is_datetime64_any_dtype(dtype):
        return "datetime"
    if isinstance(dtype, pd.CategoricalDtype):
        return "category"
    if pd.api.types.is_object_dtype(dtype) or pd.api.types.is_string_dtype(dtype):
        non_null = series.dropna()
        if not non_null.empty:
            numeric = pd.to_numeric(non_null, errors="coerce")
            if numeric.notna().all():
                return "integer" if bool((numeric % 1 == 0).all()) else "float"
    return "string"


def _numeric_summary(numeric: pd.Series) -> dict[str, Any]:
    """计算数值列的集中、离散、形态和 IQR 离群统计。"""
    if numeric.empty:
        return {
            "mean": None,
            "std": None,
            "min": None,
            "max": None,
            "median": None,
            "q1": None,
            "q3": None,
            "iqr": None,
            "skewness": None,
            "kurtosis": None,
            "outlier_count": 0,
            "outlier_rate": 0.0,
        }
    q1 = float(numeric.quantile(0.25))
    q3 = float(numeric.quantile(0.75))
    iqr = q3 - q1
    outliers = numeric[(numeric < q1 - 1.5 * iqr) | (numeric > q3 + 1.5 * iqr)]
    return {
        "mean": _finite_float(numeric.mean()),
        "std": _finite_float(numeric.std()),
        "min": _finite_float(numeric.min()),
        "max": _finite_float(numeric.max()),
        "median": _finite_float(numeric.median()),
        "q1": _finite_float(q1),
        "q3": _finite_float(q3),
        "iqr": _finite_float(iqr),
        "skewness": _finite_float(numeric.skew()) if len(numeric) >= 3 else None,
        "kurtosis": _finite_float(numeric.kurt()) if len(numeric) >= 4 else None,
        "outlier_count": int(len(outliers)),
        "outlier_rate": len(outliers) / len(numeric),
    }


def _categorical_summary(series: pd.Series) -> dict[str, Any]:
    """计算分类列的高频值与最大类别占比。"""
    values = series.dropna().map(_cell_to_text)
    counts = values.value_counts().head(_MAX_TOP_VALUES)
    total = len(values)
    return {
        "top_values": [
            {
                "value": str(value),
                "count": int(count),
                "share": int(count) / total if total else 0.0,
            }
            for value, count in counts.items()
        ],
        "max_category_share": int(counts.iloc[0]) / total
        if total and not counts.empty
        else None,
    }


def _name_tokens(name: str) -> set[str]:
    """提取字段名中的英文词元，避免用子串误判 ID 或时间。"""
    return set(re.findall(r"[a-z]+", name.lower()))


def _detect_time_column(series: pd.Series, name: str) -> bool:
    """根据字段名、原始类型和可解析率识别时间字段。"""
    lower_name = name.lower().replace(" ", "")
    tokens = _name_tokens(name)
    if tokens & _NON_TIME_KEYWORDS or any(
        keyword in lower_name for keyword in _NON_TIME_KEYWORDS_ZH
    ):
        return False
    if (
        tokens & _TIME_KEYWORDS
        or lower_name in {"年", "月", "日", "时"}
        or any(keyword in lower_name for keyword in _TIME_KEYWORDS_ZH)
    ):
        return True
    if pd.api.types.is_datetime64_any_dtype(series.dtype):
        return True
    sample = series.dropna().head(20)
    if sample.empty or pd.to_numeric(sample, errors="coerce").notna().all():
        return False
    parsed = pd.to_datetime(sample, errors="coerce", format="mixed")
    return bool(parsed.notna().mean() >= 0.8 and parsed.nunique() > 1)


def _detect_spatial_column(name: str) -> bool:
    """根据字段名识别经纬度或显式坐标字段。"""
    normalized_name = name.lower().replace(" ", "")
    return bool(
        _name_tokens(name) & _SPATIAL_KEYWORDS
        or any(keyword in normalized_name for keyword in _SPATIAL_KEYWORDS_ZH)
    )


def _detect_unit_hint(name: str) -> str | None:
    """从字段名括号内容中提取常见单位类别。"""
    for pattern, unit_type in _UNIT_PATTERNS:
        if re.search(pattern, name, re.IGNORECASE):
            return unit_type
    return None


def _parse_time_values(series: pd.Series) -> pd.Series:
    """解析时间值，并正确处理四位年份数值。"""
    values = series.dropna()
    numeric = pd.to_numeric(values, errors="coerce")
    if (
        not values.empty
        and numeric.notna().all()
        and bool(numeric.between(1000, 3000).all())
        and bool((numeric % 1 == 0).all())
    ):
        return pd.to_datetime(numeric.astype(int).astype(str), format="%Y", errors="coerce")
    return pd.to_datetime(values, errors="coerce", format="mixed")


def _time_coverage(name: str, series: pd.Series) -> dict[str, Any]:
    """返回单个时间字段的有效范围和时间点数量。"""
    parsed = _parse_time_values(series).dropna()
    return {
        "column": name,
        "min": _format_time_value(parsed.min()) if not parsed.empty else None,
        "max": _format_time_value(parsed.max()) if not parsed.empty else None,
        "unique_count": int(parsed.nunique()),
        "valid_count": int(len(parsed)),
    }


def _format_time_value(value: Any) -> str:
    """将零点时间压缩为日期字符串。"""
    text = str(value)
    return text[:-9] if text.endswith(" 00:00:00") else text


def _correlated_pairs(
    frame: pd.DataFrame,
    columns: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """返回绝对相关系数达到阈值的数值列对。"""
    numeric_series: list[pd.Series] = []
    names: list[str] = []
    for index, column in enumerate(columns):
        if column["dtype"] not in {"integer", "float"}:
            continue
        numeric_series.append(pd.to_numeric(frame.iloc[:, index], errors="coerce"))
        names.append(str(column["name"]))
        if len(numeric_series) >= _MAX_CORRELATION_COLUMNS:
            break
    if len(numeric_series) < 2 or len(frame) < 3:
        return []
    numeric_frame = pd.concat(numeric_series, axis=1)
    correlation = numeric_frame.corr()
    pairs: list[dict[str, Any]] = []
    for left in range(len(names)):
        for right in range(left + 1, len(names)):
            value = _finite_float(correlation.iloc[left, right])
            if value is not None and abs(value) >= _CORRELATION_THRESHOLD:
                pairs.append(
                    {
                        "column_a": names[left],
                        "column_b": names[right],
                        "correlation": value,
                    }
                )
    return sorted(pairs, key=lambda pair: abs(pair["correlation"]), reverse=True)


def _quality_flags(columns: list[dict[str, Any]]) -> dict[str, Any]:
    """汇总高缺失、离群、类别失衡、混合编码和常量字段。"""
    return {
        "high_missing_columns": [
            {"name": column["name"], "missing_rate": column["missing_rate"]}
            for column in columns
            if column["missing_rate"] > 0.5
        ],
        "high_outlier_columns": [
            {
                "name": column["name"],
                "outlier_rate": column["numeric_summary"]["outlier_rate"],
            }
            for column in columns
            if not column["is_time_column"]
            and not column["is_spatial"]
            and column.get("numeric_summary", {}).get("outlier_rate", 0) > 0.05
        ],
        "imbalanced_categorical_columns": [
            {
                "name": column["name"],
                "max_category_share": column["categorical_summary"]["max_category_share"],
            }
            for column in columns
            if column.get("categorical_summary", {}).get("max_category_share") is not None
            and column["categorical_summary"]["max_category_share"] >= 0.8
        ],
        "mixed_numeric_columns": [
            {
                "name": column["name"],
                "numeric_parseable_rate": column["numeric_parseable_rate"],
            }
            for column in columns
            if column.get("numeric_parseable_rate") is not None
            and 0 < column["numeric_parseable_rate"] < 1
        ],
        "constant_columns": [
            column["name"]
            for column in columns
            if column["unique_count"] <= 1 and column["missing_count"] == 0
        ],
    }


def _cell_to_text(value: Any) -> str:
    """将样例和分类值安全转换为字符串。"""
    if isinstance(value, (list, dict)):
        return str(value)
    try:
        return "" if pd.isna(value) else str(value)
    except (TypeError, ValueError):
        return str(value)


def _cell_to_json(value: Any) -> JsonValue:
    """将真实单元格值转换为保留语义的 JSON 值。"""
    if value is None:
        return None
    if isinstance(value, dict):
        return {str(key): _cell_to_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_cell_to_json(item) for item in value]
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    try:
        missing = pd.isna(value)
        if isinstance(missing, bool) and missing:
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(value, "item"):
        try:
            return _cell_to_json(value.item())
        except (TypeError, ValueError):
            pass
    if hasattr(value, "isoformat"):
        try:
            return str(value.isoformat())
        except (TypeError, ValueError):
            pass
    return str(value)


def _duplicate_count(frame: pd.DataFrame) -> int:
    try:
        return int(frame.duplicated().sum())
    except TypeError:
        return int(frame.astype(str).duplicated().sum())


def _unique_count(series: pd.Series) -> int:
    try:
        return int(series.nunique(dropna=True))
    except TypeError:
        return int(series.dropna().astype(str).nunique())


def _finite_float(value: Any) -> float | None:
    result = float(value)
    return result if math.isfinite(result) else None


def _file_type(suffix: str) -> str:
    if suffix in _CSV_SUFFIXES:
        return "csv"
    if suffix in _EXCEL_SUFFIXES:
        return "excel"
    return "json"


def _summarize_profiles(
    file_profiles: list[dict[str, Any]],
) -> dict[str, JsonValue]:
    tables = [
        table
        for file_profile in file_profiles
        for table in file_profile["tables"]
    ]
    row_count = sum(int(table["row_count"]) for table in tables)
    column_count = sum(int(table["column_count"]) for table in tables)
    missing_cells = sum(int(table["missing_cells"]) for table in tables)
    duplicate_rows = sum(int(table["duplicate_rows"]) for table in tables)
    total_cells = sum(
        int(table["row_count"]) * int(table["column_count"])
        for table in tables
    )
    columns = [column for table in tables for column in table["columns"]]
    quality_flag_count = sum(
        len(items)
        for table in tables
        for items in table["quality_flags"].values()
    )
    return {
        "file_count": len(file_profiles),
        "table_count": len(tables),
        "row_count": row_count,
        "column_count": column_count,
        "missing_cells": missing_cells,
        "missing_rate": missing_cells / total_cells if total_cells else 0.0,
        "duplicate_rows": duplicate_rows,
        "numeric_column_count": sum(
            column["dtype"] in {"integer", "float"} for column in columns
        ),
        "categorical_column_count": sum(
            column["dtype"] in {"string", "category", "boolean"}
            for column in columns
        ),
        "time_column_count": sum(column["is_time_column"] for column in columns),
        "spatial_column_count": sum(column["is_spatial"] for column in columns),
        "candidate_key_count": sum(column["is_candidate_key"] for column in columns),
        "high_correlation_pair_count": sum(
            len(table["correlated_pairs"]) for table in tables
        ),
        "quality_flag_count": quality_flag_count,
    }
