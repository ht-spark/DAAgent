from __future__ import annotations

import json

import pandas as pd
import pytest

from data_analysis.agent.state import Observation
from data_analysis.tools import (
    UnauthorizedDataPathError,
    UnsupportedDataFileError,
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
