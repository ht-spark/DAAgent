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


def test_resolver_rejects_invalid_explicit_path(tmp_path: Path) -> None:
    with pytest.raises(PythonEnvironmentError, match="不存在"):
        resolve_python_environment(tmp_path, tmp_path / "missing-venv")


def test_resolver_rejects_non_python_executable(tmp_path: Path) -> None:
    executable = tmp_path / ("other.exe" if os.name == "nt" else "other")
    executable.write_bytes(b"placeholder")

    with pytest.raises(PythonEnvironmentError, match="Python|python.exe"):
        resolve_python_environment(tmp_path, executable)


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
