"""数学建模探索工具的单元测试。"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from data_analysis.tools import (
    UnauthorizedDataPathError,
    create_agent_tool_registry,
    explore_math_models,
    supports_modeling_explore,
)


def _write_csv(path: Path, frame: pd.DataFrame) -> Path:
    frame.to_csv(path, index=False)
    return path


def test_supports_modeling_explore_matches_tabular_suffixes() -> None:
    assert supports_modeling_explore("a.csv")
    assert supports_modeling_explore("b.xlsx")
    assert not supports_modeling_explore("c.md")


def test_discovers_linear_relationship(tmp_path: Path) -> None:
    x = np.linspace(0, 10, 40)
    y = 2.5 * x + 1.0
    path = _write_csv(
        tmp_path / "linear.csv",
        pd.DataFrame({"广告投入": x, "销量": y}),
    )

    result = explore_math_models([str(path)], authorized_paths=[str(path)])

    assert result["status"] == "ok"
    laws = result["relationship_laws"]
    assert laws, "应至少发现一条规律"
    top = laws[0]
    assert top["family"] in {"linear", "polynomial_2", "polynomial_3"}
    assert top["r_squared"] > 0.99
    assert "销量" in top["formula"]
    assert result["structure_findings"]


def test_discovers_power_relationship(tmp_path: Path) -> None:
    x = np.linspace(1, 10, 40)
    y = 3.0 * x**1.5
    path = _write_csv(
        tmp_path / "power.csv",
        pd.DataFrame({"规模": x, "成本": y}),
    )

    result = explore_math_models(
        [str(path)],
        authorized_paths=[str(path)],
        max_relationships=5,
    )
    families = {law["family"] for law in result["relationship_laws"]}
    assert "power" in families or "polynomial_2" in families
    power_laws = [
        law for law in result["relationship_laws"] if law["family"] == "power"
    ]
    if power_laws:
        assert power_laws[0]["r_squared"] > 0.99


def test_target_model_reports_features_and_importance(tmp_path: Path) -> None:
    rng = np.random.default_rng(7)
    n = 60
    x1 = rng.normal(0, 1, n)
    x2 = rng.normal(0, 1, n)
    y = 3.0 * x1 - 1.5 * x2 + 0.5 + rng.normal(0, 0.05, n)
    path = _write_csv(
        tmp_path / "target.csv",
        pd.DataFrame({"特征A": x1, "特征B": x2, "目标": y}),
    )

    result = explore_math_models(
        [str(path)],
        authorized_paths=[str(path)],
        target_column="目标",
        focus="目标由哪些特征驱动",
    )

    model = result["target_model"]
    assert model is not None
    assert model["target"] == "目标"
    assert set(model["features"]) == {"特征A", "特征B"}
    assert model["r_squared"] > 0.9
    assert model["coefficients"]["intercept"] == pytest.approx(0.5, abs=0.2)
    importance = model["feature_importance"]
    assert importance[0]["feature"] == "特征A"
    assert result["focus"] == "目标由哪些特征驱动"


def test_rejects_unauthorized_path(tmp_path: Path) -> None:
    allowed = _write_csv(
        tmp_path / "ok.csv", pd.DataFrame({"a": [1, 2, 3, 4, 5, 6, 7, 8]})
    )
    denied = tmp_path / "denied.csv"
    pd.DataFrame({"a": [1]}).to_csv(denied, index=False)

    with pytest.raises(UnauthorizedDataPathError):
        explore_math_models([str(denied)], authorized_paths=[str(allowed)])


def test_rejects_unknown_target_column(tmp_path: Path) -> None:
    path = _write_csv(
        tmp_path / "data.csv",
        pd.DataFrame({"x": np.arange(20, dtype=float), "y": np.arange(20.0)}),
    )
    with pytest.raises(ValueError, match="未找到目标列"):
        explore_math_models(
            [str(path)],
            authorized_paths=[str(path)],
            target_column="不存在的列",
        )


def test_registered_in_agent_tool_registry(tmp_path: Path) -> None:
    path = _write_csv(
        tmp_path / "sales.csv",
        pd.DataFrame({"x": np.arange(20, dtype=float), "y": np.arange(20.0)}),
    )
    registry = create_agent_tool_registry([path], output_dir=tmp_path / "out")
    assert "explore_math_models" in registry
    tool = registry.resolve("explore_math_models")
    result = tool.invoke({"max_relationships": 3})
    assert result["status"] == "ok"
    assert len(result["relationship_laws"]) <= 3


def test_registry_without_data_does_not_expose_modeling_tool(tmp_path: Path) -> None:
    registry = create_agent_tool_registry([], output_dir=tmp_path / "out")
    assert "explore_math_models" not in registry


def test_handler_rejects_unknown_arguments(tmp_path: Path) -> None:
    path = _write_csv(
        tmp_path / "a.csv",
        pd.DataFrame({"x": np.arange(20, dtype=float), "y": np.arange(20.0)}),
    )
    registry = create_agent_tool_registry([path], output_dir=tmp_path / "out")
    tool = registry.resolve("explore_math_models")
    with pytest.raises(Exception, match="未声明参数"):
        tool.invoke({"nope": 1})
