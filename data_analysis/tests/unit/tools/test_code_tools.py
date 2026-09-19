from __future__ import annotations

import pandas as pd
import pytest
from pypdf import PdfWriter

from data_analysis.tools import CodeExecutionError, execute_python_code


def test_execute_python_code_reads_data_and_creates_artifact(tmp_path) -> None:
    data_path = tmp_path / "input.csv"
    output_dir = tmp_path / "output"
    pd.DataFrame({"销量": [10, 20, 30]}).to_csv(data_path, index=False)
    code = (
        "from pathlib import Path\n"
        "import pandas as pd\n"
        "frame = pd.read_csv(DATA_FILE_MAP['input.csv'])\n"
        "mean_value = float(frame['销量'].mean())\n"
        "Path(OUTPUT_DIR, 'summary.txt').write_text(str(mean_value), encoding='utf-8')\n"
        "print(f'均值={mean_value}')\n"
        "result = {'mean': mean_value, 'rows': len(frame)}\n"
    )

    observation = execute_python_code(
        code,
        data_paths=[data_path],
        output_dir=output_dir,
    )

    assert observation["stdout"] == "均值=20.0\n"
    assert observation["stderr"] == ""
    assert observation["result"] == {"mean": 20.0, "rows": 3}
    assert observation["artifacts"] == ["summary.txt"]
    assert (output_dir / "summary.txt").read_text(encoding="utf-8") == "20.0"


def test_execute_python_code_returns_dataframe_as_records(tmp_path) -> None:
    observation = execute_python_code(
        "import pandas as pd\nresult = pd.DataFrame({'值': [1, 2]})",
        data_paths=[],
        output_dir=tmp_path / "output",
    )

    assert observation["result"] == [{"值": 1}, {"值": 2}]


def test_execute_python_code_handles_surrogate_in_result(tmp_path) -> None:
    observation = execute_python_code(
        "value = chr(0xDCAE)\nprint(value)\nresult = {'text': value}",
        data_paths=[],
        output_dir=tmp_path / "output",
    )

    assert observation["stdout"] == "\udcae\n"
    assert observation["result"] == {"text": "\udcae"}


def test_execute_python_code_raises_actionable_error(tmp_path) -> None:
    with pytest.raises(CodeExecutionError, match="ZeroDivisionError"):
        execute_python_code(
            "print('开始计算')\nresult = 1 / 0",
            data_paths=[],
            output_dir=tmp_path / "output",
        )


def test_execute_python_code_can_read_uploaded_pdf(tmp_path) -> None:
    pdf_path = tmp_path / "report.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    with pdf_path.open("wb") as stream:
        writer.write(stream)

    observation = execute_python_code(
        (
            "from pypdf import PdfReader\n"
            "reader = PdfReader(DATA_FILE_MAP['report.pdf'])\n"
            "result = {'page_count': len(reader.pages)}"
        ),
        data_paths=[pdf_path],
        output_dir=tmp_path / "output",
    )

    assert observation["result"] == {"page_count": 1}


def test_execute_python_code_exposes_safe_artifact_workspace(tmp_path) -> None:
    output_dir = tmp_path / "output"

    observation = execute_python_code(
        (
            "target = ARTIFACTS.path('tables/result.xlsx')\n"
            "import pandas as pd\n"
            "pd.DataFrame({'值': [1, 2]}).to_excel(target, index=False)\n"
            "result = {'path': 'tables/result.xlsx'}"
        ),
        data_paths=[],
        output_dir=output_dir,
    )

    assert observation["artifacts"] == ["tables/result.xlsx"]
    assert pd.read_excel(output_dir / "tables" / "result.xlsx")["值"].tolist() == [
        1,
        2,
    ]


def test_execute_python_code_rejects_write_outside_run_directory(tmp_path) -> None:
    outside = tmp_path / "outside.txt"

    with pytest.raises(CodeExecutionError, match="禁止写入当前运行目录之外"):
        execute_python_code(
            f"from pathlib import Path\nPath({str(outside)!r}).write_text('越界')",
            data_paths=[],
            output_dir=tmp_path / "output",
        )

    assert not outside.exists()


def test_execute_python_code_requires_promised_artifact(tmp_path) -> None:
    with pytest.raises(CodeExecutionError, match="没有在当前运行目录生成承诺产物"):
        execute_python_code(
            "result = {'status': 'done'}",
            data_paths=[],
            output_dir=tmp_path / "output",
            expected_artifacts=["report.md"],
        )


def test_execute_python_code_returns_structured_artifact_metadata(tmp_path) -> None:
    observation = execute_python_code(
        "from pathlib import Path\nPath(ARTIFACTS.path('report.md')).write_text('# 结果', encoding='utf-8')",
        data_paths=[],
        output_dir=tmp_path / "output",
        expected_artifacts=["report.md"],
    )

    assert observation["changed_artifacts"] == ["report.md"]
    assert observation["artifact_details"] == [{
        "path": "report.md",
        "media_type": "text/markdown",
        "size_bytes": len("# 结果".encode("utf-8")),
    }]