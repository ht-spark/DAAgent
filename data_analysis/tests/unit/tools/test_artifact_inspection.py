from __future__ import annotations

import pytest

from data_analysis.agent import Action
from data_analysis.runtime import Harness
from data_analysis.tools import ToolArgumentsError, create_agent_tool_registry


class VisionLlm:
    """返回固定图片质量检查结果的测试模型。"""

    def invoke(self, messages):
        """确认收到图片并返回检查结果。"""
        assert messages[0][1][1]["type"] == "image_url"
        return type("Response", (), {"content": "坐标轴和图例清晰，无明显遮挡"})()


def test_inspect_artifact_returns_generated_code_content(tmp_path) -> None:
    """代码产物应能以真实文件内容返回给下一轮。"""
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    (output_dir / "solution.py").write_text(
        "result = sum(range(6))\n",
        encoding="utf-8",
    )
    registry = create_agent_tool_registry([], output_dir)

    result = registry.resolve("inspect_artifact").invoke(
        {"relative_path": "solution.py"}
    )

    assert result["content"] == "result = sum(range(6))\n"
    assert result["truncated"] is False


def test_inspect_artifact_rejects_path_traversal(tmp_path) -> None:
    """产物读取不得越过当前会话工作区。"""
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    registry = create_agent_tool_registry([], output_dir)

    with pytest.raises(ToolArgumentsError, match="相对路径"):
        registry.resolve("inspect_artifact").invoke(
            {"relative_path": "../secret.py"}
        )


def test_inspect_artifact_rejects_runtime_internal_file(tmp_path) -> None:
    output_dir = tmp_path / "output"
    runtime = output_dir / ".runtime"
    runtime.mkdir(parents=True)
    (runtime / "cache.py").write_text("secret = True", encoding="utf-8")
    registry = create_agent_tool_registry([], output_dir)

    with pytest.raises(ToolArgumentsError, match="运行时内部目录"):
        registry.resolve("inspect_artifact").invoke(
            {"relative_path": "nested/.runtime/cache.py"}
        )


def test_inspect_generated_image_returns_visual_observation(tmp_path) -> None:
    """生成图片应能重新交给当前模型检查。"""
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    (output_dir / "chart.png").write_bytes(b"generated-image")
    registry = create_agent_tool_registry(
        [],
        output_dir,
        vision_llm=VisionLlm(),
    )

    observation = Harness(registry).execute(
        Action(
            tool_name="inspect_generated_image",
            arguments={
                "relative_path": "chart.png",
                "question": "检查图表是否清晰",
            },
        )
    )

    assert observation.success is True
    assert observation.result["path"] == "chart.png"
    assert "无明显遮挡" in observation.result["analysis"]
