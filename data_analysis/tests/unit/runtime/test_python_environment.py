from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from data_analysis.runtime import PythonEnvironmentError, resolve_python_environment
from data_analysis.tools import execute_python_code


def _venv_python(environment_root: Path) -> Path:
    if os.name == "nt":
        return environment_root / "Scripts" / "python.exe"
    return environment_root / "bin" / "python"


def test_resolver_prefers_project_dot_venv(tmp_path: Path) -> None:
    executable = _venv_python(tmp_path / ".venv")
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"placeholder")

    environment = resolve_python_environment(tmp_path)

    assert environment.executable == executable.resolve()
    assert environment.source == "project"
    assert environment.name == ".venv"


def test_resolver_discovers_named_project_environment(tmp_path: Path) -> None:
    environment_root = tmp_path / "analysis-runtime"
    executable = _venv_python(environment_root)
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"placeholder")
    (environment_root / "pyvenv.cfg").write_text("home = python", encoding="utf-8")

    environment = resolve_python_environment(tmp_path)

    assert environment.executable == executable.resolve()
    assert environment.name == "analysis-runtime"


def test_resolver_accepts_explicit_interpreter(tmp_path: Path) -> None:
    environment = resolve_python_environment(tmp_path, sys.executable)

    assert environment.executable == Path(sys.executable).resolve()
    assert environment.source == "custom"


def test_resolver_creates_environment_at_missing_configured_path(tmp_path: Path) -> None:
    # 用户指定路径不存在时，视为虚拟环境创建位置
    target = tmp_path / "user-venv"

    environment = resolve_python_environment(tmp_path, target)

    assert environment.source == "created"
    assert environment.name == "user-venv"
    assert environment.executable == _venv_python(target).resolve()


def test_resolver_rejects_non_python_executable(tmp_path: Path) -> None:
    executable = tmp_path / ("other.exe" if os.name == "nt" else "other")
    executable.write_bytes(b"placeholder")

    with pytest.raises(PythonEnvironmentError, match="Python|python.exe"):
        resolve_python_environment(tmp_path, executable)


def test_resolver_never_falls_back_to_host() -> None:
    # 没有项目根目录且未指定环境时必须报错，禁止回退到宿主解释器
    with pytest.raises(PythonEnvironmentError, match="项目根目录"):
        resolve_python_environment(None)


def test_resolver_auto_creates_project_venv(tmp_path: Path) -> None:
    # 项目内没有环境时，自动在项目根目录创建 .venv
    environment = resolve_python_environment(tmp_path)

    assert environment.source == "created"
    assert environment.name == ".venv"
    assert environment.executable.is_file()


def test_agents_md_declaration_has_highest_priority(tmp_path: Path) -> None:
    # AGENTS.md 声明的环境优先于用户指定路径和项目内扫描结果
    environment_root = tmp_path / "md-env"
    executable = _venv_python(environment_root)
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"placeholder")
    (environment_root / "pyvenv.cfg").write_text("home = python", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text(
        f"python_environment: {environment_root}\n",
        encoding="utf-8",
    )
    other_root = tmp_path / ".venv"
    other_executable = _venv_python(other_root)
    other_executable.parent.mkdir(parents=True)
    other_executable.write_bytes(b"placeholder")

    environment = resolve_python_environment(tmp_path, other_root)

    assert environment.source == "declared"
    assert environment.name == "md-env"
    assert environment.executable == executable.resolve()


def test_agents_md_declaration_accepts_relative_path(tmp_path: Path) -> None:
    environment_root = tmp_path / "custom-runtime"
    executable = _venv_python(environment_root)
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"placeholder")
    (tmp_path / "AGENTS.md").write_text(
        "python_environment=custom-runtime\n",
        encoding="utf-8",
    )

    environment = resolve_python_environment(tmp_path)

    assert environment.source == "declared"
    assert environment.executable == executable.resolve()


def test_agents_md_invalid_declaration_raises(tmp_path: Path) -> None:
    # AGENTS.md 是最高约束，声明路径无效时必须报错而不是静默回退
    (tmp_path / "AGENTS.md").write_text(
        "python_environment: D:/not/exist/venv\n",
        encoding="utf-8",
    )

    with pytest.raises(PythonEnvironmentError, match="不存在"):
        resolve_python_environment(tmp_path)


def test_execute_python_uses_selected_virtual_environment(tmp_path: Path) -> None:
    environment_root = tmp_path / "project-env"
    subprocess.run(
        [sys.executable, "-m", "venv", str(environment_root)],
        check=True,
        capture_output=True,
        text=True,
    )
    executable = _venv_python(environment_root)

    observation = execute_python_code(
        "import sys\nresult = {'prefix': sys.prefix, 'executable': sys.executable}",
        data_paths=[],
        output_dir=tmp_path / "output",
        python_executable=executable,
        environment_source="custom",
        environment_name="project-env",
    )

    assert Path(observation["result"]["prefix"]).resolve() == environment_root.resolve()
    assert Path(observation["runtime"]["python_executable"]).resolve() == executable.resolve()
    assert observation["runtime"]["source"] == "custom"
    assert observation["runtime"]["name"] == "project-env"
