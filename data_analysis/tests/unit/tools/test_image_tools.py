from __future__ import annotations

from data_analysis.agent import Action
from data_analysis.runtime import Harness
from data_analysis.tools import create_agent_tool_registry


class VisionLlm:
    """返回固定视觉分析结果的测试模型。"""

    def invoke(self, messages):
        """校验多模态消息并返回测试响应。"""
        assert messages[0][1][1]["type"] == "image_url"
        return type("Response", (), {"content": "图中最大值为 8"})()


class TextOnlyLlm:
    """拒绝图片输入的测试模型。"""

    def invoke(self, messages):
        """模拟纯文本模型拒绝视觉消息。"""
        raise ValueError("image_url is not supported")


def test_agent_registry_exposes_image_inspection_with_current_model(tmp_path) -> None:
    """图片附件应通过当前模型按需识别。"""
    image_path = tmp_path / "chart.png"
    image_path.write_bytes(b"image-content")
    registry = create_agent_tool_registry(
        [image_path],
        tmp_path / "output",
        vision_llm=VisionLlm(),
    )

    assert "inspect_image" in [spec.name for spec in registry.specs()]
    result = registry.resolve("inspect_image").invoke(
        {"file_name": "chart.png", "question": "最大值是多少？"}
    )

    assert result == {"file_name": "chart.png", "analysis": "图中最大值为 8"}


def test_image_inspection_returns_clear_error_for_non_vision_model(tmp_path) -> None:
    """当前模型拒绝图片时应返回统一且可读的错误。"""
    image_path = tmp_path / "chart.png"
    image_path.write_bytes(b"image-content")
    registry = create_agent_tool_registry(
        [image_path],
        tmp_path / "output",
        vision_llm=TextOnlyLlm(),
    )
    observation = Harness(registry).execute(
        Action(
            tool_name="inspect_image",
            arguments={"file_name": "chart.png", "question": "读取图片"},
        )
    )

    assert observation.success is False
    assert observation.error == "当前模型不支持识图"


def test_image_inspection_rejects_unbound_file_name(tmp_path) -> None:
    """识图工具不得读取本轮未授权的同类文件。"""
    image_path = tmp_path / "allowed.png"
    image_path.write_bytes(b"image-content")
    registry = create_agent_tool_registry(
        [image_path],
        tmp_path / "output",
        vision_llm=VisionLlm(),
    )
    observation = Harness(registry).execute(
        Action(
            tool_name="inspect_image",
            arguments={"file_name": "other.png", "question": "读取图片"},
        )
    )

    assert observation.success is False
    assert observation.error == "图片未获授权或不存在: other.png"
