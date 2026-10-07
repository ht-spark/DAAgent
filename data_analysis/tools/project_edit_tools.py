"""限定项目目录的文本创建与精确编辑。"""
from __future__ import annotations

from contextlib import contextmanager
import difflib
import hashlib
import json
import os
import shutil
import tempfile
import time
import unicodedata
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any, BinaryIO, Iterator, TypedDict

from .workspace_tools import (
    WorkspacePathError,
    resolve_workspace_member_path,
)

MAX_TEXT_BYTES = 500_000
MAX_COMMAND_OUTPUT_CHARS = 20_000
MAX_FILE_EDITS = 100
# 同一文件的写操作最长等待 30 秒；超时后把冲突明确返回给模型，避免拖死整轮运行。
FILE_LOCK_TIMEOUT_SECONDS = 30.0
BLOCKED_PARTS = {
    ".git",
    ".runtime",
    ".venv",
    "node_modules",
    "__pycache__",
    "outputs",
}


class FileEdit(TypedDict):
    """一项基于原始文件内容的精确文本替换。"""

    old_text: str
    new_text: str


_FUZZY_CHARACTER_TRANSLATION = str.maketrans({
    "\u2018": "'",
    "\u2019": "'",
    "\u201a": "'",
    "\u201b": "'",
    "\u201c": '"',
    "\u201d": '"',
    "\u201e": '"',
    "\u201f": '"',
    "\u2010": "-",
    "\u2011": "-",
    "\u2012": "-",
    "\u2013": "-",
    "\u2014": "-",
    "\u2015": "-",
    "\u2212": "-",
    "\u00a0": " ",
    "\u2002": " ",
    "\u2003": " ",
    "\u2004": " ",
    "\u2005": " ",
    "\u2006": " ",
    "\u2007": " ",
    "\u2008": " ",
    "\u2009": " ",
    "\u200a": " ",
    "\u202f": " ",
    "\u205f": " ",
    "\u3000": " ",
})


def normalize_for_fuzzy_match(text: str) -> str:
    """规范尾随空白、智能标点、破折号与特殊空格。"""
    normalized = unicodedata.normalize("NFKC", text)
    normalized = "\n".join(line.rstrip() for line in normalized.split("\n"))
    return normalized.translate(_FUZZY_CHARACTER_TRANSLATION)


def _line_spans(content: str) -> list[tuple[int, int]]:
    """返回保留换行符的逐行字符区间。"""
    spans: list[tuple[int, int]] = []
    offset = 0
    for line in content.splitlines(keepends=True):
        spans.append((offset, offset + len(line)))
        offset += len(line)
    if content and not spans:
        spans.append((0, len(content)))
    return spans


def _replacement_line_range(
    spans: list[tuple[int, int]],
    start: int,
    end: int,
) -> tuple[int, int]:
    """把字符替换区间扩展为它实际触及的行区间。"""
    start_line = next(
        (index for index, (line_start, line_end) in enumerate(spans)
         if line_start <= start < line_end),
        -1,
    )
    if start_line < 0:
        raise ValueError("修改范围超出原文件内容")
    end_line = start_line
    while end_line < len(spans) and spans[end_line][1] < end:
        end_line += 1
    if end_line >= len(spans):
        raise ValueError("修改范围超出原文件内容")
    return start_line, end_line + 1


def _apply_replacements(
    content: str,
    replacements: list[tuple[int, int, str, int]],
    *,
    offset: int = 0,
) -> str:
    """按逆序应用已排序的替换，保持所有原始偏移稳定。"""
    result = content
    for start, end, new_text, _index in reversed(replacements):
        relative_start = start - offset
        relative_end = end - offset
        result = f"{result[:relative_start]}{new_text}{result[relative_end:]}"
    return result


def _apply_fuzzy_replacements_preserving_lines(
    original: str,
    normalized: str,
    replacements: list[tuple[int, int, str, int]],
) -> str:
    """只重写模糊匹配实际触及的行，其余行保留原始字节对应文本。"""
    original_lines = original.splitlines(keepends=True)
    normalized_spans = _line_spans(normalized)
    if len(original_lines) != len(normalized_spans):
        raise ValueError("规范化文本行数变化，无法安全保留未修改内容")

    groups: list[tuple[int, int, list[tuple[int, int, str, int]]]] = []
    for replacement in replacements:
        start_line, end_line = _replacement_line_range(
            normalized_spans,
            replacement[0],
            replacement[1],
        )
        if groups and start_line < groups[-1][1]:
            previous_start, previous_end, previous_replacements = groups[-1]
            groups[-1] = (
                previous_start,
                max(previous_end, end_line),
                [*previous_replacements, replacement],
            )
        else:
            groups.append((start_line, end_line, [replacement]))

    original_line = 0
    result: list[str] = []
    for start_line, end_line, group_replacements in groups:
        result.extend(original_lines[original_line:start_line])
        group_start = normalized_spans[start_line][0]
        group_end = normalized_spans[end_line - 1][1]
        result.append(_apply_replacements(
            normalized[group_start:group_end],
            group_replacements,
            offset=group_start,
        ))
        original_line = end_line
    result.extend(original_lines[original_line:])
    return "".join(result)


def prepare_exact_text_edits(
    raw_content: bytes,
    edits: list[FileEdit],
    path: str = "<file>",
) -> tuple[bytes, str, str]:
    """校验并计算一组 Pi 风格的精确或轻量模糊文本替换。

    Args:
        raw_content: 原始 UTF-8 文件字节。
        edits: 基于原始文件内容的一组精确替换。
        path: 用于错误信息的文件路径。

    Returns:
        写回字节、规范化后的原文和规范化后的修改文本。
    """
    if not edits or len(edits) > MAX_FILE_EDITS:
        raise ValueError(f"edits 必须包含 1 到 {MAX_FILE_EDITS} 项修改")
    has_bom = raw_content.startswith(b"\xef\xbb\xbf")
    original_text = raw_content.decode("utf-8-sig")
    crlf_index = original_text.find("\r\n")
    lf_index = original_text.find("\n")
    newline = (
        "\r\n"
        if crlf_index >= 0 and (lf_index < 0 or crlf_index < lf_index)
        else "\n"
    )
    original = original_text.replace("\r\n", "\n").replace("\r", "\n")
    normalized_edits = [
        {
            "old_text": edit["old_text"].replace("\r\n", "\n").replace("\r", "\n"),
            "new_text": edit["new_text"].replace("\r\n", "\n").replace("\r", "\n"),
        }
        for edit in edits
    ]
    for index, edit in enumerate(normalized_edits, start=1):
        if not edit["old_text"]:
            raise ValueError(f"第 {index} 项 edit 的 old_text 不能为空")

    fuzzy_content = normalize_for_fuzzy_match(original)
    uses_fuzzy_match = any(
        edit["old_text"] not in original
        and normalize_for_fuzzy_match(edit["old_text"]) in fuzzy_content
        for edit in normalized_edits
    )
    replacement_base = fuzzy_content if uses_fuzzy_match else original
    replacements: list[tuple[int, int, str, int]] = []
    for index, edit in enumerate(normalized_edits, start=1):
        old_text = (
            normalize_for_fuzzy_match(edit["old_text"])
            if uses_fuzzy_match
            else edit["old_text"]
        )
        new_text = edit["new_text"]
        start = replacement_base.find(old_text)
        if start < 0:
            raise ValueError(
                f"第 {index} 项 edit 无法在 {path} 中找到 old_text；"
                "必须匹配原文件中的空白和换行"
            )
        occurrences = replacement_base.count(old_text)
        if occurrences > 1:
            raise ValueError(
                f"第 {index} 项 edit 的 old_text 匹配 {occurrences} 次，需要唯一匹配"
            )
        replacements.append((start, start + len(old_text), new_text, index))

    replacements.sort(key=lambda item: item[0])
    for previous, current in zip(replacements, replacements[1:]):
        if current[0] < previous[1]:
            raise ValueError(
                f"第 {previous[3]} 项和第 {current[3]} 项 edit 的修改范围重叠"
            )

    updated = (
        _apply_fuzzy_replacements_preserving_lines(
            original,
            replacement_base,
            replacements,
        )
        if uses_fuzzy_match
        else _apply_replacements(replacement_base, replacements)
    )
    if updated == original:
        raise ValueError(f"未修改 {path}：替换后的内容与原内容完全相同")
    restored = updated.replace("\n", newline)
    encoded = restored.encode("utf-8")
    if has_bom:
        encoded = b"\xef\xbb\xbf" + encoded
    return encoded, original, updated


def generate_edit_diff(
    old_content: str,
    new_content: str,
    *,
    context_lines: int = 4,
) -> tuple[str, int | None]:
    """生成带行号的紧凑展示 diff，并返回修改后文件的首个变更行。"""
    old_lines = old_content.splitlines()
    new_lines = new_content.splitlines()
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines)
    groups = list(matcher.get_grouped_opcodes(context_lines))
    width = len(str(max(len(old_lines), len(new_lines), 1)))
    output: list[str] = []
    first_changed_line: int | None = None
    for group_index, group in enumerate(groups):
        if group_index:
            output.append(f" {'':>{width}} ...")
        for tag, old_start, old_end, new_start, new_end in group:
            if tag == "equal":
                for offset, line in enumerate(old_lines[old_start:old_end]):
                    output.append(f" {old_start + offset + 1:>{width}} {line}")
                continue
            if first_changed_line is None:
                first_changed_line = new_start + 1
            if tag in {"delete", "replace"}:
                for offset, line in enumerate(old_lines[old_start:old_end]):
                    output.append(f"-{old_start + offset + 1:>{width}} {line}")
            if tag in {"insert", "replace"}:
                for offset, line in enumerate(new_lines[new_start:new_end]):
                    output.append(f"+{new_start + offset + 1:>{width}} {line}")
    return "\n".join(output), first_changed_line


def generate_unified_patch(path: str, old_content: str, new_content: str) -> str:
    """生成标准 unified patch，默认保留四行上下文。"""
    return "".join(difflib.unified_diff(
        old_content.splitlines(keepends=True),
        new_content.splitlines(keepends=True),
        fromfile=path,
        tofile=path,
        n=4,
    ))


def coerce_file_edits(value: Any) -> list[FileEdit]:
    """兼容模型把 edits 输出成 JSON 字符串、单个对象或 camelCase 字段。"""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("edits JSON 字符串无效") from exc
    if isinstance(value, Mapping):
        value = [value]
    if not isinstance(value, list) or not value or len(value) > MAX_FILE_EDITS:
        raise ValueError("edits 参数无效")

    edits: list[FileEdit] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise ValueError("edits 参数无效")
        keys = set(item)
        if keys == {"old_text", "new_text"}:
            old_text = item["old_text"]
            new_text = item["new_text"]
        elif keys == {"oldText", "newText"}:
            old_text = item["oldText"]
            new_text = item["newText"]
        else:
            raise ValueError("edits 参数无效")
        if not isinstance(old_text, str) or not isinstance(new_text, str):
            raise ValueError("edits 参数无效")
        edits.append({"old_text": old_text, "new_text": new_text})
    return edits


def _editable_path(workspace_root: str | Path, relative_path: str) -> Path:
    """解析项目内可编辑的普通文本文件路径。"""
    root = Path(workspace_root).resolve(strict=True)
    target = resolve_workspace_member_path(root, relative_path)
    parts = target.relative_to(root).parts
    if not parts or any(part.casefold() in BLOCKED_PARTS for part in parts):
        raise WorkspacePathError("不允许修改该项目路径")
    if target.name.casefold().startswith(".env"):
        raise WorkspacePathError("不允许修改环境密钥文件")
    if target.is_symlink():
        raise WorkspacePathError("不允许修改符号链接文件")
    return target


def _try_lock_file(stream: BinaryIO) -> None:
    """以非阻塞方式锁住锁文件的第一个字节。"""
    stream.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        return
    import fcntl

    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file(stream: BinaryIO) -> None:
    """释放当前进程持有的文件锁。"""
    stream.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


@contextmanager
def _exclusive_target_lock(target: Path) -> Iterator[None]:
    """按目标绝对路径建立跨进程互斥锁，串行化同一文件的读改写。"""
    normalized = os.path.normcase(str(target.resolve(strict=False)))
    lock_name = hashlib.sha256(normalized.encode("utf-8")).hexdigest() + ".lock"
    lock_root = Path(tempfile.gettempdir()) / "daagent-file-locks"
    lock_root.mkdir(parents=True, exist_ok=True)
    lock_path = lock_root / lock_name
    deadline = time.monotonic() + FILE_LOCK_TIMEOUT_SECONDS
    with lock_path.open("a+b") as stream:
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        while True:
            try:
                _try_lock_file(stream)
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"等待文件编辑锁超时: {target}") from exc
                time.sleep(0.05)
        try:
            yield
        finally:
            _unlock_file(stream)


def _atomic_replace_bytes(target: Path, content: bytes, *, preserve_mode: bool) -> None:
    """先完整落盘临时文件，再用同目录原子替换提交内容。"""
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if preserve_mode:
            shutil.copymode(target, temporary)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def write_workspace_file(
    workspace_root: str | Path,
    relative_path: str,
    content: str,
) -> dict[str, Any]:
    """创建或完整覆盖一个 UTF-8 项目文件，并自动创建父目录。"""
    target = _editable_path(workspace_root, relative_path)
    encoded = content.encode("utf-8")
    if len(encoded) > MAX_TEXT_BYTES:
        raise ValueError("文件内容超过大小限制")
    with _exclusive_target_lock(target):
        if target.exists() and not target.is_file():
            raise ValueError("目标路径不是普通文件")
        if target.is_symlink():
            raise WorkspacePathError("不允许修改符号链接文件")
        existed = target.exists()
        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic_replace_bytes(target, encoded, preserve_mode=existed)
    return {
        "path": relative_path,
        "created": not existed,
        "overwritten": existed,
        "size_bytes": len(encoded),
    }


def edit_workspace_file(
    workspace_root: str | Path,
    relative_path: str,
    edits: list[FileEdit],
    *,
    expected_content: bytes | None = None,
) -> dict[str, Any]:
    """一次性校验并应用一组不重叠的精确文本替换，返回实际 diff。"""
    target = _editable_path(workspace_root, relative_path)
    with _exclusive_target_lock(target):
        if (
            not target.is_file()
            or target.is_symlink()
            or target.stat().st_size > MAX_TEXT_BYTES
        ):
            raise ValueError("目标文件不存在、不是普通文件或超过大小限制")
        raw_content = target.read_bytes()
        if expected_content is not None and raw_content != expected_content:
            raise ValueError(
                "项目目标文件与本轮上传附件内容不一致，已拒绝原位修改以避免改错文件"
            )
        encoded, original, updated = prepare_exact_text_edits(
            raw_content,
            edits,
            relative_path,
        )
        if len(encoded) > MAX_TEXT_BYTES:
            raise ValueError("修改后的文件超过大小限制")
        diff, first_changed_line = generate_edit_diff(original, updated)
        patch = generate_unified_patch(relative_path, original, updated)
        _atomic_replace_bytes(target, encoded, preserve_mode=True)
    return {
        "path": relative_path,
        "edit_count": len(edits),
        "diff": diff[:MAX_COMMAND_OUTPUT_CHARS],
        "patch": patch[:MAX_COMMAND_OUTPUT_CHARS],
        "first_changed_line": first_changed_line,
    }


def run_write_workspace_file(
    arguments: dict[str, Any],
    context: dict[str, Any],
) -> dict[str, Any]:
    """校验工具参数后创建或完整覆盖项目文件。"""
    if set(arguments) != {"relative_path", "content"} or not all(
        isinstance(arguments[key], str) for key in arguments
    ):
        raise ValueError("write_workspace_file 参数无效")
    return write_workspace_file(
        context["workspace_root"],
        arguments["relative_path"],
        arguments["content"],
    )


def run_edit_workspace_file(arguments: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """校验工具参数后精确修改项目文件。"""
    allowed = {"relative_path", "edits", "old_text", "new_text", "oldText", "newText"}
    if set(arguments) - allowed or not isinstance(arguments.get("relative_path"), str):
        raise ValueError("edit_workspace_file 参数无效")
    raw_edits: Any = arguments.get("edits")
    legacy: dict[str, Any] | None = None
    if "old_text" in arguments or "new_text" in arguments:
        legacy = {
            "old_text": arguments.get("old_text"),
            "new_text": arguments.get("new_text"),
        }
    elif "oldText" in arguments or "newText" in arguments:
        legacy = {
            "oldText": arguments.get("oldText"),
            "newText": arguments.get("newText"),
        }
    if raw_edits is None:
        raw_edits = legacy
    elif legacy is not None:
        parsed = coerce_file_edits(raw_edits)
        parsed.extend(coerce_file_edits(legacy))
        raw_edits = parsed
    try:
        edits = coerce_file_edits(raw_edits)
    except ValueError as exc:
        raise ValueError("edit_workspace_file 参数无效") from exc
    if len(edits) > MAX_FILE_EDITS:
        raise ValueError("edit_workspace_file 参数无效")
    return edit_workspace_file(
        context["workspace_root"],
        arguments["relative_path"],
        edits,
    )
