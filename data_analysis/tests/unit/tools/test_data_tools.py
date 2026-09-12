from __future__ import annotations

import json

import pandas as pd
import pytest

from data_analysis.agent.state import Observation
from data_analysis.tools import (
    UnauthorizedDataPathError,
    UnsupportedDataFileError,
    inspect_data_rows,
    profile_data,
)


def test_profile_csv_returns_basic_statistics(tmp_path) -> None:
    path = tmp_path / "sales.csv"
    pd.DataFrame(
        {
            "销量": [1.0, 2.0, 2.0, None],
            "地区": ["华东", "华南", "华南", None],
        }
    ).to_csv(path, index=False)

    result = profile_data([path], authorized_paths=[path])

    assert result["summary"] == {
        "file_count": 1,
        "table_count": 1,
        "row_count": 4,
        "column_count": 2,
        "missing_cells": 2,
        "missing_rate": 0.25,
        "duplicate_rows": 1,
        "numeric_column_count": 1,
        "categorical_column_count": 1,
        "time_column_count": 0,
        "spatial_column_count": 0,
        "candidate_key_count": 0,
        "high_correlation_pair_count": 0,
        "quality_flag_count": 0,
    }
    table = result["files"][0]["tables"][0]
    assert table["duplicate_rate"] == 0.25
    sales = table["columns"][0]
    assert sales["dtype"] == "float"
    assert sales["missing_rate"] == 0.25
    assert sales["numeric_summary"]["mean"] == pytest.approx(5 / 3)
    assert sales["numeric_summary"]["std"] == pytest.approx(1 / 3**0.5)
    assert sales["numeric_summary"]["min"] == 1.0
    assert sales["numeric_summary"]["max"] == 2.0


def test_profile_returns_distribution_semantics_and_quality_flags(tmp_path) -> None:
    path = tmp_path / "quality.csv"
    pd.DataFrame(
        {
            "记录编号": ["A1", "A2", "A3", "A4", "A5", "A6"],
            "价格(元)": [1, 2, 2, 3, 3, 100],
            "类别": ["甲", "甲", "甲", "甲", "甲", "乙"],
            "混合编码": ["1", "A", "2", "B", "3", "C"],
            "常量": [7, 7, 7, 7, 7, 7],
        }
    ).to_csv(path, index=False)

    table = profile_data([path], authorized_paths=[path])["files"][0]["tables"][0]
    columns = {column["name"]: column for column in table["columns"]}

    assert "记录编号" in table["candidate_keys"]
    assert columns["价格(元)"]["unit_hint"] == "货币"
    assert columns["价格(元)"]["numeric_summary"]["median"] == 2.5
    assert columns["价格(元)"]["numeric_summary"]["outlier_count"] == 1
    assert columns["类别"]["categorical_summary"]["top_values"][0] == {
        "value": "甲",
        "count": 5,
        "share": pytest.approx(5 / 6),
    }
    assert columns["混合编码"]["numeric_parseable_rate"] == 0.5
    assert table["quality_flags"]["constant_columns"] == ["常量"]
    assert table["quality_flags"]["imbalanced_categorical_columns"][0]["name"] == "类别"
    assert table["quality_flags"]["mixed_numeric_columns"][0]["name"] == "混合编码"


def test_profile_detects_time_space_and_high_correlation(tmp_path) -> None:
    path = tmp_path / "spatiotemporal.csv"
    pd.DataFrame(
        {
            "日期": ["2025-01-01", "2025-01-02", "2025-01-03", "2025-01-04"],
            "经度": [116.1, 116.2, 116.3, 116.4],
            "纬度": [39.1, 39.2, 39.3, 39.4],
            "指标A": [1, 2, 3, 4],
            "指标B": [2, 4, 6, 8],
        }
    ).to_csv(path, index=False)

    result = profile_data([path], authorized_paths=[path])
    table = result["files"][0]["tables"][0]

    assert table["time_columns"] == ["日期"]
    assert table["time_coverage"] == [
        {
            "column": "日期",
            "min": "2025-01-01",
            "max": "2025-01-04",
            "unique_count": 4,
            "valid_count": 4,
        }
    ]
    assert table["spatial_columns"] == ["经度", "纬度"]
    assert any(
        pair["column_a"] == "指标A"
        and pair["column_b"] == "指标B"
        and pair["correlation"] == pytest.approx(1.0)
        for pair in table["correlated_pairs"]
    )
    assert result["summary"]["time_column_count"] == 1
    assert result["summary"]["spatial_column_count"] == 2


def test_profile_excel_expands_all_sheets(tmp_path) -> None:
    path = tmp_path / "workbook.xlsx"
    with pd.ExcelWriter(path) as writer:
        pd.DataFrame({"编号": [1, 2]}).to_excel(
            writer,
            sheet_name="订单",
            index=False,
        )
        pd.DataFrame({"名称": ["甲", "乙", "丙"]}).to_excel(
            writer,
            sheet_name="产品",
            index=False,
        )

    result = profile_data([path], authorized_paths=[path])

    assert result["summary"]["table_count"] == 2
    assert result["summary"]["row_count"] == 5
    assert [table["sheet_name"] for table in result["files"][0]["tables"]] == [
        "订单",
        "产品",
    ]


def test_profile_json_and_json_lines(tmp_path) -> None:
    json_path = tmp_path / "records.json"
    json_path.write_text(
        json.dumps([{"a": 1}, {"a": 2}], ensure_ascii=False),
        encoding="utf-8",
    )
    jsonl_path = tmp_path / "records.jsonl"
    jsonl_path.write_text('{"b": 1}\n{"b": 2}\n', encoding="utf-8")

    result = profile_data(
        [json_path, jsonl_path],
        authorized_paths=[json_path, jsonl_path],
    )

    assert result["summary"]["file_count"] == 2
    assert result["summary"]["row_count"] == 4
    assert result["files"][1]["file_type"] == "json"


def test_profile_rejects_unauthorized_path(tmp_path) -> None:
    allowed = tmp_path / "allowed.csv"
    denied = tmp_path / "denied.csv"
    pd.DataFrame({"a": [1]}).to_csv(allowed, index=False)
    pd.DataFrame({"a": [2]}).to_csv(denied, index=False)

    with pytest.raises(UnauthorizedDataPathError, match="denied.csv"):
        profile_data([denied], authorized_paths=[allowed])


def test_profile_rejects_missing_authorized_file(tmp_path) -> None:
    missing = tmp_path / "missing.csv"

    with pytest.raises(FileNotFoundError, match="missing.csv"):
        profile_data([missing], authorized_paths=[missing])


def test_profile_rejects_unsupported_file(tmp_path) -> None:
    path = tmp_path / "notes.txt"
    path.write_text("不是表格", encoding="utf-8")

    with pytest.raises(UnsupportedDataFileError, match=".txt"):
        profile_data([path], authorized_paths=[path])


def test_profile_deduplicates_equivalent_paths(tmp_path) -> None:
    path = tmp_path / "data.csv"
    pd.DataFrame({"a": [1]}).to_csv(path, index=False)

    result = profile_data(
        [path, path.resolve()],
        authorized_paths=[path],
    )

    assert result["summary"]["file_count"] == 1


def test_profile_result_is_valid_observation_payload(tmp_path) -> None:
    path = tmp_path / "data.csv"
    pd.DataFrame({"a": [1, 2]}).to_csv(path, index=False)

    result = profile_data([path], authorized_paths=[path])
    observation = Observation(
        tool_name="profile_data",
        success=True,
        result=result,
    )

    assert observation.result == result


def test_inspect_data_rows_returns_requested_real_rows(tmp_path) -> None:
    path = tmp_path / "sales.csv"
    pd.DataFrame(
        {
            "销量": [10, 20, 30, 40],
            "地区": ["华东", "华南", None, "华北"],
        }
    ).to_csv(path, index=False)

    result = inspect_data_rows(
        [path],
        authorized_paths=[path],
        start_row=2,
        row_count=2,
    )

    assert result == {
        "file_name": "sales.csv",
        "sheet_name": "",
        "columns": ["销量", "地区"],
        "total_rows": 4,
        "start_row": 2,
        "end_row": 3,
        "requested_row_count": 2,
        "returned_row_count": 2,
        "has_more_rows": True,
        "rows": [
            {"row_number": 2, "values": [20, "华南"]},
            {"row_number": 3, "values": [30, None]},
        ],
    }


def test_inspect_data_rows_selects_excel_sheet(tmp_path) -> None:
    path = tmp_path / "workbook.xlsx"
    with pd.ExcelWriter(path) as writer:
        pd.DataFrame({"编号": [1]}).to_excel(writer, sheet_name="订单", index=False)
        pd.DataFrame({"名称": ["甲", "乙"]}).to_excel(
            writer,
            sheet_name="产品",
            index=False,
        )

    result = inspect_data_rows(
        [path],
        authorized_paths=[path],
        row_count=1,
        sheet_name="产品",
    )

    assert result["sheet_name"] == "产品"
    assert result["rows"] == [{"row_number": 1, "values": ["甲"]}]


def test_inspect_data_rows_requires_unambiguous_file_and_sheet(tmp_path) -> None:
    first = tmp_path / "first.csv"
    second = tmp_path / "second.csv"
    pd.DataFrame({"值": [1]}).to_csv(first, index=False)
    pd.DataFrame({"值": [2]}).to_csv(second, index=False)

    with pytest.raises(ValueError, match="指定 file_name"):
        inspect_data_rows(
            [first, second],
            authorized_paths=[first, second],
            row_count=1,
        )


@pytest.mark.parametrize(
    ("start_row", "row_count", "message"),
    [(0, 1, "start_row"), (1, 0, "row_count"), (1, 201, "row_count")],
)
def test_inspect_data_rows_rejects_invalid_range(
    tmp_path,
    start_row: int,
    row_count: int,
    message: str,
) -> None:
    path = tmp_path / "data.csv"
    pd.DataFrame({"值": [1]}).to_csv(path, index=False)

    with pytest.raises(ValueError, match=message):
        inspect_data_rows(
            [path],
            authorized_paths=[path],
            start_row=start_row,
            row_count=row_count,
        )
