"""sandbox_backends 单元测试：策略矩阵、路由、配置生成、sidecar 与产物工具。

对齐《MXC 集成设计方案 V2.2》§9 的单测要求；全部使用 mock 或临时目录，
不依赖 MXC 真机资产、不依赖网络。
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

from data_analysis.runtime.sandbox_backends import (
    BackendProbe,
    PythonRunRequest,
    SandboxPolicy,
    SandboxPolicyError,
    SandboxService,
    SandboxSettings,
    SandboxUnavailableError,
    load_settings,
    resolve_policy,
)
from data_analysis.runtime.sandbox_backends.artifacts import (
    normalize_relative_path,
    changed_artifacts,
    describe_artifacts,
    snapshot_artifacts,
)
from data_analysis.runtime.sandbox_backends.errors import SidecarProtocolError
from data_analysis.runtime.sandbox_backends.mxc_backend import (
    MxcBackend,
    _load_sidecar,
    build_mxc_config,
    parse_probe_output,
    resolve_runtime_dirs,
)

PROJECT_ROOT = Path(__file__).resolve().parents[4]


# ---------------------------------------------------------------------------
# 策略解析
# ---------------------------------------------------------------------------


def test_load_settings_defaults_and_invalid_values() -> None:
    """缺省配置为 local/development；非法值 fail-fast。"""
    settings = load_settings({})
    assert settings.backend == "local"
    assert settings.profile == "development"
    assert settings.allow_local_fallback is False
    with pytest.raises(ValueError):
        load_settings({"DAAGENT_SANDBOX_BACKEND": "trust-me"})
    with pytest.raises(ValueError):
        load_settings({"DAAGENT_SANDBOX_PROFILE": "staging"})
    with pytest.raises(ValueError):
        load_settings({"DAAGENT_SANDBOX_ALLOW_UNSAFE_FALLBACK": "maybe"})


def test_resolve_policy_matrix() -> None:
    """隔离要求与降级许可的推导矩阵。"""
    local = resolve_policy(SandboxSettings(backend="local"))
    assert local.required_isolation == "legacy_allowed"
    assert local.allow_local_fallback is False

    dev_mxc = resolve_policy(SandboxSettings(backend="mxc", profile="development"))
    assert dev_mxc.required_isolation == "mxc_required"

    prod_mxc = resolve_policy(SandboxSettings(backend="mxc", profile="production"))
    assert prod_mxc.required_isolation == "supported_mxc_required"

    # 降级许可只在 development + 显式开关下成立
    dev_fallback = resolve_policy(
        SandboxSettings(backend="auto", profile="development", allow_local_fallback=True)
    )
    assert dev_fallback.allow_local_fallback is True
    prod_fallback = resolve_policy(
        SandboxSettings(backend="auto", profile="production", allow_local_fallback=True)
    )
    assert prod_fallback.allow_local_fallback is False


# ---------------------------------------------------------------------------
# 路由矩阵（mock 后端）
# ---------------------------------------------------------------------------


class _FakeBackend:
    """用于路由测试的桩后端。"""

    name = "fake"

    def __init__(self, probe: BackendProbe) -> None:
        self._probe = probe
        self.calls: list[PythonRunRequest] = []

    def probe(self) -> BackendProbe:
        return self._probe

    def run_python(self, request, *, policy, cancel_check=None):  # noqa: ANN001
        self.calls.append(request)
        return {"stdout": "", "stderr": "", "result": None, "artifacts": [],
                "changed_artifacts": [], "artifact_details": [], "runtime": {}}

    def run_command(self, request, *, policy, cancel_check=None):  # noqa: ANN001
        raise NotImplementedError


def _make_service(
    *,
    backend: str,
    profile: str = "development",
    fallback: bool = False,
    mxc_available: bool = True,
) -> tuple[SandboxService, _FakeBackend, _FakeBackend]:
    probe = (
        BackendProbe(available=True, backend="mxc", tier="processcontainer")
        if mxc_available
        else BackendProbe(available=False, backend="mxc", reason="executor missing")
    )
    fake_local = _FakeBackend(BackendProbe(available=True, backend="local"))
    fake_mxc = _FakeBackend(probe)
    service = SandboxService(
        SandboxSettings(
            backend=backend,
            profile=profile,
            allow_local_fallback=fallback,
            mxc_executable=Path("missing/wxc-exec.exe"),
        ),
        backends={"local": fake_local, "mxc": fake_mxc},
    )
    return service, fake_local, fake_mxc


def _request() -> PythonRunRequest:
    return PythonRunRequest(
        run_id="test-run",
        code="result = 1",
        data_paths=(),
        output_dir=Path("."),
        workspace_root=None,
        python_executable=Path(sys.executable),
    )


def test_routing_local_and_strict_reject() -> None:
    """local + legacy_allowed 走 local；要求隔离时拒绝。"""
    service, fake_local, _ = _make_service(backend="local")
    service.run_python(_request())
    assert len(fake_local.calls) == 1

    strict = SandboxService(
        SandboxSettings(backend="mxc", profile="production"),
        backends={"local": _FakeBackend(BackendProbe(available=True, backend="local")),
                  "mxc": _FakeBackend(BackendProbe(available=True, backend="mxc"))},
    )
    with pytest.raises(SandboxPolicyError):
        strict.run_python(_request(), backend="local")


def test_routing_mxc_fail_closed_and_auto_fallback() -> None:
    """mxc 探测失败必须报错；auto + 显式许可 + development 才能降级。"""
    failing, fake_local, _ = _make_service(backend="mxc", mxc_available=False)
    with pytest.raises(SandboxUnavailableError):
        failing.run_python(_request())

    auto_ok, fake_local_2, _ = _make_service(
        backend="auto", fallback=True, mxc_available=False
    )
    auto_ok.run_python(_request())
    assert len(fake_local_2.calls) == 1  # 已降级到 local

    auto_denied, _, _ = _make_service(backend="auto", fallback=False, mxc_available=False)
    with pytest.raises(SandboxUnavailableError):
        auto_denied.run_python(_request())

    # production 要求 supported_mxc_required：即使配置了许可也不降级
    prod, fake_local_3, _ = _make_service(
        backend="auto", profile="production", fallback=True, mxc_available=False
    )
    with pytest.raises(SandboxUnavailableError):
        prod.run_python(_request())


def test_routing_rejects_unknown_backend_name() -> None:
    """未知后端标识直接拒绝。"""
    service, _, _ = _make_service(backend="local")
    with pytest.raises(SandboxPolicyError):
        service.run_python(_request(), backend="shell-plus")


# ---------------------------------------------------------------------------
# MXC 配置生成与探测解析
# ---------------------------------------------------------------------------


def test_parse_probe_output() -> None:
    """probe 输出解析：正常、缺字段与非法 JSON 均按约定处理。"""
    probe = parse_probe_output(
        json.dumps({"tier": "appcontainer-dacl", "warnings": ["w1"]})
    )
    assert probe.available is True
    assert probe.tier == "appcontainer-dacl"
    assert probe.warnings == ("w1",)
    with pytest.raises(ValueError):
        parse_probe_output("not json")
    with pytest.raises(ValueError):
        parse_probe_output(json.dumps({"warnings": []}))


def test_resolve_runtime_dirs_for_project_venv() -> None:
    """项目 .venv 应解析出 venv 根与 base 解释器目录。"""
    venv_python = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
    if not venv_python.is_file():
        pytest.skip("当前环境没有项目 .venv")
    venv_root, base_dir = resolve_runtime_dirs(venv_python)
    assert venv_root == (PROJECT_ROOT / ".venv").resolve()
    assert base_dir is not None
    assert (base_dir / "python.exe").is_file() or (base_dir / "Lib").is_dir()


def test_build_mxc_config_mapping(tmp_path: Path) -> None:
    """配置字段映射：路径集合、环境变量、超时与网络策略。"""
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    data_file = tmp_path / "data.csv"
    data_file.write_text("a,b\n1,2\n", encoding="utf-8")
    control_dir = tmp_path / "control"
    control_dir.mkdir()
    sidecar = output_dir / ".runtime" / "result.json"
    venv_python = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
    if not venv_python.is_file():
        pytest.skip("当前环境没有项目 .venv")
    request = PythonRunRequest(
        run_id="cfg-test",
        code="print(1)",
        data_paths=(data_file,),
        output_dir=output_dir,
        workspace_root=None,
        python_executable=venv_python,
        timeout_seconds=123.0,
    )
    config = build_mxc_config(
        request,
        policy=SandboxPolicy(),
        control_dir=control_dir,
        sidecar_path=sidecar,
        executable=venv_python.resolve(),
        output_dir=output_dir.resolve(),
    )
    assert config["version"] == "1.0.0"
    assert config["containment"] == "process"
    assert config["lifecycle"] == {"destroyOnExit": True, "preservePolicy": False}
    process = config["process"]
    assert process["commandLine"].startswith(f'"{venv_python.resolve()}" -I -B ')
    assert process["timeout"] == 123_000
    assert process["inheritDefaultEnv"] is False
    env_text = "\n".join(process["env"])
    assert "LOCALAPPDATA=" in env_text  # presence-only 必需项
    assert "SYSTEMROOT=" in env_text
    assert "MPLCONFIGDIR=" in env_text
    assert "TEMP=" not in env_text and "TMP=" not in env_text  # 由平台管理
    filesystem = config["filesystem"]
    assert filesystem["readwritePaths"] == [str(output_dir.resolve())]
    readonly = filesystem["readonlyPaths"]
    assert str(data_file.resolve()) in readonly
    assert str(control_dir) in readonly
    assert str((PROJECT_ROOT / ".venv").resolve()) in readonly
    assert config["network"]["egress"]["default"] == "deny"
    assert config["ui"]["disable"] is False


def test_mxc_probe_reports_missing_executor() -> None:
    """执行器缺失时 probe 返回不可用（不抛异常）。"""
    backend = MxcBackend(Path("definitely/missing/wxc-exec.exe"))
    probe = backend.probe()
    assert probe.available is False
    assert "不存在" in probe.reason


# ---------------------------------------------------------------------------
# sidecar 校验（不可信数据）
# ---------------------------------------------------------------------------


def _write_sidecar(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_load_sidecar_validation(tmp_path: Path) -> None:
    """sidecar 的通过/拒绝路径：合法、run_id 不符、超限、缺失。"""
    limits = SandboxPolicy().limits
    sidecar = tmp_path / "result.json"
    valid = {
        "schema_version": 1,
        "run_id": "run-1",
        "ok": True,
        "result": {"value": 1},
        "error_type": None,
        "error": None,
    }
    _write_sidecar(sidecar, valid)
    loaded = _load_sidecar(sidecar, run_id="run-1", limits=limits)
    assert loaded["ok"] is True

    _write_sidecar(sidecar, {**valid, "run_id": "other"})
    with pytest.raises(SidecarProtocolError):
        _load_sidecar(sidecar, run_id="run-1", limits=limits)

    _write_sidecar(sidecar, {**valid, "error": None})
    sidecar.write_text("x" * (limits.max_result_bytes + 1), encoding="utf-8")
    with pytest.raises(SidecarProtocolError):
        _load_sidecar(sidecar, run_id="run-1", limits=limits)

    missing = tmp_path / "nope.json"
    with pytest.raises(SidecarProtocolError):
        _load_sidecar(missing, run_id="run-1", limits=limits)


# ---------------------------------------------------------------------------
# 产物审计工具
# ---------------------------------------------------------------------------


def test_normalize_relative_path_rejects_unsafe() -> None:
    """相对路径规范化：合法通过；绝对、含 `..`、触及 .runtime 拒绝。"""
    assert normalize_relative_path("reports/a.csv") == "reports/a.csv"
    for bad in ("", "   ", "../a.csv", str(Path.home() / "a.csv"), "a/.runtime/x"):
        with pytest.raises(ValueError):
            normalize_relative_path(bad)


def test_snapshot_changed_and_describe(tmp_path: Path) -> None:
    """快照/变化检测/SHA-256 描述与 .runtime 排除。"""
    out = tmp_path / "out"
    out.mkdir()
    (out / "runtime-inside").mkdir()
    (out / ".runtime").mkdir()
    (out / ".runtime" / "hidden.txt").write_text("hidden", encoding="utf-8")
    before = snapshot_artifacts(out)
    assert before == {}  # 只有 .runtime 内容，全部排除

    payload = b"hello,artifact\n"
    (out / "a.csv").write_bytes(payload)
    after = snapshot_artifacts(out)
    changed = changed_artifacts(before, after)
    assert changed == ["a.csv"]

    details = describe_artifacts(out, changed)
    assert details[0]["size_bytes"] == len(payload)
    assert details[0]["media_type"] == "text/csv"
    assert details[0]["sha256"] == hashlib.sha256(payload).hexdigest()


# ---------------------------------------------------------------------------
# local 后端防递归（真实执行一次）
# ---------------------------------------------------------------------------


def test_execute_python_code_local_no_recursion(tmp_path: Path) -> None:
    """经 SandboxService 路由的 local 执行必须成功（回归递归/循环导入陷阱）。"""
    from data_analysis.tools.code_tools import execute_python_code

    out = tmp_path / "out"
    out.mkdir()
    result = execute_python_code(
        "result = 6 * 7",
        data_paths=[],
        output_dir=out,
        sandbox_backend="local",
    )
    assert result["result"] == 42
    assert result["runtime"]["source"] == "host"
