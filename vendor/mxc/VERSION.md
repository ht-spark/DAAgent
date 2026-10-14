# MXC 资产版本记录

| 项 | 值 |
|---|---|
| 上游包 | `@microsoft/mxc-sdk@1.0.0`（npm，registry.npmjs.org） |
| 上游仓库 | https://github.com/microsoft/mxc |
| 提取路径 | 包内 `bin/x64/` 全量（23 个文件，含 `wxc-exec.exe`、`mxc_ffi.dll`、`plm.exe`、`_manifest/`、`dotnet/`） |
| 部署日期 | 2026-10-14 |
| 部署方式 | 从 npm 包复制；`scripts/fetch_mxc.py` 可锁版本重新获取 |
| 平台 | Windows x64（`bin/x64`）；ARM64 使用同包 `bin/arm64` |

## 关键文件 SHA-256（2026-10-14 部署时核对）

```
8a6d1db4dd1846981127d9014741be18b094d07be967765dff6754bda1d40259  wxc-exec.exe
d49383365e7e2399603c51d6650946bf83ff9df4093ac6597f0b973af754947e  mxc_ffi.dll
07fbc793b29f068627743f496290464e815cb9df9b5500ac957ae867b52e9152  plm.exe
c07c94acf62844c5c20590a2d3e7944d0beaaf424df934afc8c83d531bf72825  wxc-host-prep.exe
```

## 说明

- `vendor/mxc/bin/` 已加入 `.gitignore`（二进制不入库）；本文件与 `scripts/fetch_mxc.py` 入库。
- `wxc-exec.exe --probe` 可查询本机可用隔离 tier（开发机 Windows 10 19045 实测为 `appcontainer-dacl`，非官方支持矩阵，定位开发实验；官方支持矩阵见 MXC 仓库 `docs/backends/process-container/os-version-support.md`）。
- 部署环境校验：CI 或新机器上可用 `scripts/fetch_mxc.py --verify` 核对 SHA-256。
- 切勿运行 `wxc-exec.exe --audit` 处理不可信代码（审计模式会放宽沙箱保护）。
