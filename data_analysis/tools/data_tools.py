"""面向 Agent 的确定性数据读取与基础画像工具。"""

from __future__ import annotations

import math
import os
from pathlib import Path
from typing import Any, Sequence

import pandas as pd
from pydantic import JsonValue

__all__ = [
    "UnauthorizedDataPathError",
    "UnsupportedDataFileError",
    "profile_data",
]

_CSV_SUFFIXES = frozenset({".csv"})
_EXCEL_SUFFIXES = frozenset({".xlsx", ".xls"})
_JSON_SUFFIXES = frozenset({".json", ".jsonl", ".ndjson"})
_SUPPORTED_SUFFIXES = _CSV_SUFFIXES | _EXCEL_SUFFIXES | _JSON_SUFFIXES


class UnauthorizedDataPathError(PermissionError):
    """请求的数据文件不在本次运行的授权范围内。"""


class UnsupportedDataFileError(ValueError):
    """数据文件格式不受当前画像工具支持。"""


def profile_data(
    data_paths: Sequence[str | Path],
    *,
    authorized_paths: Sequence[str | Path],
) -> dict[str, JsonValue]:
    """读取已授权数据文件并生成基础画像。

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
    return {
        "source_file": source_file,
        "sheet_name": sheet_name,
        "row_count": int(row_count),
        "column_count": int(column_count),
        "missing_cells": missing_cells,
        "missing_rate": missing_cells / total_cells if total_cells else 0.0,
        "duplicate_rows": duplicate_rows,
        "duplicate_rate": duplicate_rows / row_count if row_count else 0.0,
        "columns": columns,
    }


def _profile_column(
    name: str,
    series: pd.Series,
    row_count: int,
) -> dict[str, JsonValue]:
    missing_count = int(series.isna().sum())
    profile: dict[str, Any] = {
        "name": name,
        "dtype": _normalized_dtype(series),
        "missing_count": missing_count,
        "missing_rate": missing_count / row_count if row_count else 0.0,
        "unique_count": _unique_count(series),
    }
    if pd.api.types.is_numeric_dtype(series.dtype) and not pd.api.types.is_bool_dtype(
        series.dtype
    ):
        numeric = pd.to_numeric(series, errors="coerce").dropna()
        profile["numeric_summary"] = {
            "mean": _finite_float(numeric.mean()) if not numeric.empty else None,
            "std": _finite_float(numeric.std()) if not numeric.empty else None,
            "min": _finite_float(numeric.min()) if not numeric.empty else None,
            "max": _finite_float(numeric.max()) if not numeric.empty else None,
        }
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
    return "string"


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
    return {
        "file_count": len(file_profiles),
        "table_count": len(tables),
        "row_count": row_count,
        "column_count": column_count,
        "missing_cells": missing_cells,
        "missing_rate": missing_cells / total_cells if total_cells else 0.0,
        "duplicate_rows": duplicate_rows,
    }
