"""把常用 Markdown 结构写入 DOCX。"""

from __future__ import annotations

import re
from pathlib import Path

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT


def markdown_to_docx(markdown: str, output_path: str, *, base_dir: str) -> None:
    """将 Markdown 文本转换为可编辑 DOCX。

    Args:
        markdown: Markdown 正文。
        output_path: 输出 DOCX 路径。
        base_dir: 解析相对图片路径时允许使用的根目录。
    """
    document = Document()
    lines = markdown.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index].rstrip()
        if not line.strip():
            index += 1
            continue
        heading = re.match(r"^(#{1,4})\s+(.+)$", line)
        if heading:
            document.add_heading(heading.group(2).strip(), level=len(heading.group(1)))
            index += 1
            continue
        image = re.fullmatch(r"!\[([^]]*)\]\(([^)]+)\)", line.strip())
        if image:
            _add_image(document, image.group(2), image.group(1), Path(base_dir))
            index += 1
            continue
        if line.lstrip().startswith("|"):
            table_lines: list[str] = []
            while index < len(lines) and lines[index].lstrip().startswith("|"):
                table_lines.append(lines[index].strip())
                index += 1
            _add_table(document, table_lines)
            continue
        list_item = re.match(r"^\s*([-*]|\d+\.)\s+(.+)$", line)
        if list_item:
            style = "List Bullet" if list_item.group(1) in {"-", "*"} else "List Number"
            document.add_paragraph(list_item.group(2).strip(), style=style)
            index += 1
            continue
        document.add_paragraph(line)
        index += 1
    document.save(output_path)


def _add_image(document: Document, raw_path: str, caption: str, base_dir: Path) -> None:
    """在根目录范围内插入本地图片。"""
    root = base_dir.resolve(strict=False)
    image_path = (root / raw_path).resolve(strict=False)
    if root != image_path and root not in image_path.parents:
        document.add_paragraph(f"[图片路径越界：{raw_path}]")
        return
    if image_path.is_file():
        document.add_picture(str(image_path))
        if caption.strip():
            document.add_paragraph(caption.strip())
    else:
        document.add_paragraph(f"[图片不存在：{raw_path}]")


def _add_table(document: Document, lines: list[str]) -> None:
    """把标准 Markdown 管道表格写入 Word 表格。"""
    rows = [[cell.strip() for cell in line.strip("|").split("|")] for line in lines]
    if len(rows) >= 2 and all(re.fullmatch(r":?-{3,}:?", cell) for cell in rows[1]):
        rows.pop(1)
    if not rows:
        return
    width = max(len(row) for row in rows)
    table = document.add_table(rows=len(rows), cols=width)
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.style = "Table Grid"
    for row_index, row in enumerate(rows):
        for column_index, value in enumerate(row):
            table.cell(row_index, column_index).text = value
