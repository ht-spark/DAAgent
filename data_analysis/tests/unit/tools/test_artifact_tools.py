from __future__ import annotations

import json

import pandas as pd
import pytest
from docx import Document

from data_analysis.tools import (
    ArtifactExistsError,
    ArtifactPathError,
    UnsupportedArtifactFormatError,
    write_artifact,
)


def test_write_artifact_writes_nested_code_and_markdown(tmp_path) -> None:
    output_dir = tmp_path / "output"

    code_result = write_artifact(
        "scripts/analysis.py",
        "print('ok')\n",
        output_dir=output_dir,
    )
    markdown_result = write_artifact(
        "reports/summary.md",
        "# 分析结果\n",
        output_dir=output_dir,
    )

    assert code_result["path"] == "scripts/analysis.py"
    assert markdown_result["format"] == "md"
    assert (output_dir / "scripts" / "analysis.py").read_text(
        encoding="utf-8"
    ) == "print('ok')\n"


@pytest.mark.parametrize(
    "relative_path",
    [
        "../outside.md",
        "input/source.md",
        ".runtime/cache.md",
        "reports/.runtime/cache.md",
        "D:/outside.md",
        "/outside.md",
        "",
    ],
)
def test_write_artifact_rejects_unsafe_paths(tmp_path, relative_path: str) -> None:
    with pytest.raises(ArtifactPathError):
        write_artifact(
            relative_path,
            "内容",
            output_dir=tmp_path / "output",
        )


def test_write_artifact_rejects_session_input_directory(tmp_path) -> None:
    with pytest.raises(ArtifactPathError, match="输入附件目录"):
        write_artifact(
            ".runs/run-1/input/source.csv",
            [{"值": 1}],
            output_dir=tmp_path / "output",
        )


def test_write_artifact_requires_explicit_overwrite(tmp_path) -> None:
    output_dir = tmp_path / "output"
    write_artifact("result.txt", "第一版", output_dir=output_dir)

    with pytest.raises(ArtifactExistsError):
        write_artifact("result.txt", "第二版", output_dir=output_dir)

    result = write_artifact(
        "result.txt",
        "第二版",
        output_dir=output_dir,
        overwrite=True,
    )

    assert result["overwritten"] is True
    assert (output_dir / "result.txt").read_text(encoding="utf-8") == "第二版"


def test_write_artifact_writes_valid_json_and_csv(tmp_path) -> None:
    output_dir = tmp_path / "output"
    write_artifact(
        "data/result.json",
        {"结论": "通过", "数量": 2},
        output_dir=output_dir,
    )
    write_artifact(
        "data/result.csv",
        [{"名称": "甲", "值": 1}, {"名称": "乙", "值": 2}],
        output_dir=output_dir,
    )

    assert json.loads(
        (output_dir / "data" / "result.json").read_text(encoding="utf-8")
    ) == {"结论": "通过", "数量": 2}
    assert pd.read_csv(output_dir / "data" / "result.csv")["值"].tolist() == [
        1,
        2,
    ]


def test_write_artifact_writes_multiple_excel_sheets(tmp_path) -> None:
    output_dir = tmp_path / "output"

    result = write_artifact(
        "tables/result.xlsx",
        {
            "sheets": {
                "汇总": [{"指标": "均值", "值": 2.5}],
                "明细": [{"编号": 1}, {"编号": 2}],
            }
        },
        output_dir=output_dir,
    )

    workbook = pd.read_excel(output_dir / "tables" / "result.xlsx", sheet_name=None)
    assert result["format"] == "xlsx"
    assert list(workbook) == ["汇总", "明细"]
    assert workbook["明细"]["编号"].tolist() == [1, 2]


def test_write_artifact_converts_markdown_to_docx(tmp_path) -> None:
    output_dir = tmp_path / "output"

    result = write_artifact(
        "reports/result.docx",
        "# 分析报告\n\n结论正文。",
        output_dir=output_dir,
    )

    document = Document(output_dir / "reports" / "result.docx")
    text = "\n".join(paragraph.text for paragraph in document.paragraphs)
    assert result["format"] == "docx"
    assert "分析报告" in text
    assert "结论正文" in text


def test_write_artifact_rejects_unsupported_format(tmp_path) -> None:
    with pytest.raises(UnsupportedArtifactFormatError, match="不支持产物格式"):
        write_artifact(
            "program.exe",
            "binary",
            output_dir=tmp_path / "output",
        )
