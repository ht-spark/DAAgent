"""
文档转换工具（基于 MarkItDown）。

确定性工具，不依赖 LLM，可单测、可复现。

功能：
  - 把 PDF / Word / PowerPoint / Excel / HTML / EPUB / Notebook 等文档
    统一转换为 Markdown 文本
  - 支持两种入口：磁盘路径（流式读取）与内存字节流（上传场景，无需落临时文件）

设计约束：
  1. 懒加载：MarkItDown 属选装依赖，未安装时抛出可读异常而非在导入期崩溃。
  2. 扩展名白名单：只受理明确支持的格式，是否解析不看内容嗅探结果。
  3. 体积上限：转换前校验大小，避免超大文件长时间占用进程。
  4. 空结果检测：扫描版 PDF 会「转换成功」但内容为空，这里显式判为失败。
  5. 文件头校验：二进制格式先比对魔数，避免「扩展名与实际内容不符」的文件
     被 MarkItDown 退回纯文本解析后记为「转换成功」。
  6. 不启用 LLM 图片描述：保持输出确定性，不产生额外 token 开销。
"""
from __future__ import annotations

import functools
import io
import logging
from pathlib import Path
from typing import Any

__all__ = [
    "MAX_DOCUMENT_BYTES",
    "SUPPORTED_DOCUMENT_EXTENSIONS",
    "DocumentConversionError",
    "is_supported_document",
    "supported_extensions",
    "to_markdown",
    "to_markdown_from_bytes",
]

logger = logging.getLogger(__name__)

# 受支持的扩展名：MarkItDown 内置转换器，外加 pdf/docx/pptx/xlsx/xls 选装依赖
SUPPORTED_DOCUMENT_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".pdf",
        ".docx",
        ".pptx",
        ".xlsx",
        ".xls",
        ".html",
        ".htm",
        ".epub",
        ".ipynb",
        ".csv",
        ".txt",
        ".md",
        ".markdown",
        ".json",
        ".xml",
    }
)

# 单文件体积上限 50 MB：超过后解析耗时不可控
MAX_DOCUMENT_BYTES = 50 * 1024 * 1024

# 二进制格式的文件头魔数：用于在解析前识别「扩展名与实际内容不符」的文件
_BINARY_SIGNATURES: dict[str, tuple[bytes, ...]] = {
    ".pdf": (b"%PDF-",),
    ".docx": (b"PK\x03\x04",),
    ".pptx": (b"PK\x03\x04",),
    ".xlsx": (b"PK\x03\x04",),
    ".epub": (b"PK\x03\x04",),
    ".xls": (b"\xd0\xcf\x11\xe0",),
}

# PDF 允许文件头前存在少量前导字节，按规范在 1024 字节内查找
_HEADER_SEARCH_BYTES = 1024

# 缺少选装依赖时的修复提示
_INSTALL_HINT = 'uv add "markitdown[pdf,docx,pptx,xlsx,xls]"'


class DocumentConversionError(RuntimeError):
    """文档转换失败（格式损坏、缺少选装依赖、内容为空等）。"""


def supported_extensions() -> tuple[str, ...]:
    """返回受支持扩展名的排序元组，供接口与界面展示。

    Returns:
        形如 ``('.csv', '.docx', ...)`` 的扩展名元组。
    """
    return tuple(sorted(SUPPORTED_DOCUMENT_EXTENSIONS))


def is_supported_document(path: str | Path) -> bool:
    """判断路径是否为受支持的文档格式。

    只看扩展名，不读取文件内容，因此对不存在的路径同样可用。

    Args:
        path: 待判断的文件路径或文件名。

    Returns:
        True 表示该扩展名可交给 MarkItDown 转换。
    """
    return Path(path).suffix.lower() in SUPPORTED_DOCUMENT_EXTENSIONS


def _validate(name: str, size: int, max_bytes: int | None) -> str:
    """校验文件名与体积，返回小写扩展名。

    Args:
        name: 文件名或路径（用于取扩展名与报错）。
        size: 内容字节数。
        max_bytes: 体积上限，None 表示不限制。

    Returns:
        小写扩展名，例如 ``'.pdf'``。

    Raises:
        ValueError: 扩展名不在白名单内。
        DocumentConversionError: 内容为空或超过体积上限。
    """
    suffix = Path(name).suffix.lower()
    if suffix not in SUPPORTED_DOCUMENT_EXTENSIONS:
        raise ValueError(
            f"Unsupported document type '{suffix}'. "
            f"Supported: {', '.join(supported_extensions())}"
        )
    if size == 0:
        raise DocumentConversionError(f"文档内容为空：{name}")
    if max_bytes is not None and size > max_bytes:
        raise DocumentConversionError(
            f"文档体积 {size / 1024 / 1024:.1f} MB 超过上限 "
            f"{max_bytes / 1024 / 1024:.0f} MB，请拆分后再转换"
        )
    return suffix


def _check_signature(suffix: str, head: bytes, name: str) -> None:
    """校验二进制格式的文件头魔数。

    MarkItDown 对无法识别的二进制内容会退回按纯文本解析，结果是「成功」但内容全无意义；
    这里提前拦截，给出明确的失败原因，避免转换状态失真。

    拦截范围说明：只覆盖「扩展名与实际内容完全不匹配」（如把文本改名成 .pdf）。
    文件头合法但内部损坏的文件仍会进入解析阶段，属于 MarkItDown 自身的行为。

    Args:
        suffix: 小写扩展名。
        head: 文件开头若干字节。
        name: 文件名，仅用于报错。

    Raises:
        DocumentConversionError: 扩展名要求二进制格式，但内容缺少对应文件头。
    """
    signatures = _BINARY_SIGNATURES.get(suffix)
    if signatures is None:
        return
    window = head[:_HEADER_SEARCH_BYTES]
    if any(signature in window for signature in signatures):
        return
    raise DocumentConversionError(
        f"文件内容与扩展名 {suffix} 不符（缺少该格式的文件头），"
        f"可能已损坏或并非该格式：{name}"
    )


@functools.lru_cache(maxsize=1)
def _get_converter() -> Any:
    """构造并缓存 MarkItDown 实例。

    实例化会加载 magika 类型识别模型（onnxruntime），开销在百毫秒量级，
    因此全局复用同一实例；同时把导入推迟到首次调用，未安装依赖时只报错不崩溃。

    Returns:
        可复用的 MarkItDown 实例。

    Raises:
        DocumentConversionError: 未安装 MarkItDown。
    """
    try:
        from markitdown import MarkItDown
    except ImportError as exc:  # pragma: no cover - 取决于本地环境
        raise DocumentConversionError(
            f"未安装 MarkItDown，无法转换文档。请执行: {_INSTALL_HINT}"
        ) from exc

    # enable_plugins=False：禁用第三方插件，保证输出只由内置转换器决定
    return MarkItDown(enable_plugins=False)


def _extract_markdown(result: Any, name: str) -> str:
    """从转换结果中取出 Markdown 文本并校验非空。

    Args:
        result: MarkItDown 的 DocumentConverterResult。
        name: 文件名，仅用于报错。

    Returns:
        去除首尾空白后的 Markdown 文本。

    Raises:
        DocumentConversionError: 未提取到任何文本内容。
    """
    markdown = (getattr(result, "text_content", "") or "").strip()
    if not markdown:
        raise DocumentConversionError(
            "未提取到任何文本内容，可能是扫描版或纯图片文档，需要 OCR 支持"
        )
    return markdown


def _convert(call: Any, name: str) -> str:
    """执行一次转换调用，把 MarkItDown 的异常统一收敛为 DocumentConversionError。

    Args:
        call: 无参可调用对象，内部发起真正的转换。
        name: 文件名，仅用于报错。

    Returns:
        Markdown 文本。

    Raises:
        DocumentConversionError: 缺少依赖、格式无法识别或转换失败。
    """
    try:
        result = call()
    except Exception as exc:  # MarkItDown 各转换器抛出的异常类型不统一
        kind = type(exc).__name__
        if kind == "MissingDependencyException":
            raise DocumentConversionError(
                f"{Path(name).suffix.lower()} 解析依赖缺失，请执行: {_INSTALL_HINT}"
            ) from exc
        if kind == "UnsupportedFormatException":
            raise DocumentConversionError(f"MarkItDown 无法识别该文件内容：{name}") from exc
        logger.warning("MarkItDown 转换失败: %s (%s)", name, kind)
        raise DocumentConversionError(f"文档转换失败：{exc}") from exc
    return _extract_markdown(result, name)


def to_markdown(
    path: str | Path,
    *,
    max_bytes: int | None = MAX_DOCUMENT_BYTES,
) -> str:
    """把磁盘上的单个文档转换为 Markdown 文本。

    以流式方式读取，不会把整个文件读进内存。

    Args:
        path: 待转换的文件路径。
        max_bytes: 体积上限（字节）。None 表示不限制。

    Returns:
        去除首尾空白后的 Markdown 文本。

    Raises:
        FileNotFoundError: 文件不存在。
        ValueError: 路径不是文件，或扩展名不在白名单内。
        DocumentConversionError: 文件为空、超过体积上限、缺少依赖或转换失败。
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Document not found: {p}")
    if not p.is_file():
        raise ValueError(f"Not a file: {p}")

    suffix = _validate(p.name, p.stat().st_size, max_bytes)
    with open(p, "rb") as handle:
        _check_signature(suffix, handle.read(_HEADER_SEARCH_BYTES), p.name)

    converter = _get_converter()
    return _convert(lambda: converter.convert_local(str(p)), p.name)


def to_markdown_from_bytes(
    content: bytes,
    filename: str,
    *,
    max_bytes: int | None = MAX_DOCUMENT_BYTES,
) -> str:
    """把内存中的文档字节流转换为 Markdown 文本。

    用于上传场景：内容已在内存里，无需再落临时文件；扩展名由 filename 提供，
    避免 MarkItDown 依赖内容嗅探误判格式。

    Args:
        content: 文档二进制内容。
        filename: 原始文件名，用于取扩展名与报错。
        max_bytes: 体积上限（字节）。None 表示不限制。

    Returns:
        去除首尾空白后的 Markdown 文本。

    Raises:
        ValueError: 扩展名不在白名单内。
        DocumentConversionError: 内容为空、超过体积上限、缺少依赖或转换失败。
    """
    from markitdown import StreamInfo

    suffix = _validate(filename, len(content), max_bytes)
    _check_signature(suffix, content, filename)

    converter = _get_converter()
    stream_info = StreamInfo(
        extension=suffix,
        filename=Path(filename).name,
    )
    return _convert(
        lambda: converter.convert_stream(io.BytesIO(content), stream_info=stream_info),
        filename,
    )
