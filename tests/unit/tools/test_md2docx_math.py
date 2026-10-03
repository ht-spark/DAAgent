from docx import Document

from data_analysis.tools.docx_writer import markdown_to_docx


def test_markdown_writer_preserves_formula_text(tmp_path):
    """轻量 DOCX 写出器必须保留尚未转换的公式原文。"""
    output_path = tmp_path / "math.docx"
    markdown_to_docx(
        "# 模型\n\n模型满足 $x_i^2 + \\frac{a}{b}$。",
        str(output_path),
        base_dir=str(tmp_path),
    )

    document = Document(output_path)
    text = "\n".join(paragraph.text for paragraph in document.paragraphs)
    assert "模型" in text
    assert "$x_i^2 + \\frac{a}{b}$" in text
