"""锁定版本获取 MXC 原生资产（wxc-exec.exe 等）到 vendor/mxc/。

设计说明（对齐《MXC 集成设计方案 V2.2》§8.1）：
- 只使用标准库，从 npm registry 下载固定版本的 @microsoft/mxc-sdk 包并提取
  `bin/<arch>/` 下的原生资产；不运行 Node SDK、不执行包内任何脚本。
- 版本为常量锁定；新版本升级必须显式修改 DEFAULT_VERSION 并更新
  vendor/mxc/VERSION.md 中的 SHA-256 记录。
- `--verify` 模式不联网，仅核对已部署资产的完整性（存在性 + 输出关键文件
  SHA-256 供与 VERSION.md 人工/CI 比对），供部署与新机器校验使用。

用法：
    python scripts/fetch_mxc.py                 # 获取并部署（默认 x64）
    python scripts/fetch_mxc.py --arch arm64    # 指定架构
    python scripts/fetch_mxc.py --verify        # 核对已部署资产
"""

from __future__ import annotations

import argparse
import hashlib
import io
import sys
import tarfile
import urllib.request
from pathlib import Path

# 锁定的上游版本；升级时同步更新 vendor/mxc/VERSION.md
DEFAULT_VERSION = "1.0.0"
PACKAGE_NAME = "@microsoft/mxc-sdk"
REGISTRY_BASE = "https://registry.npmjs.org"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
VENDOR_ROOT = PROJECT_ROOT / "vendor" / "mxc"

# 运行 wxc-exec.exe 所需的关键文件（verify 模式重点核对）
KEY_FILES = ("wxc-exec.exe", "mxc_ffi.dll", "plm.exe", "wxc-host-prep.exe")

# 单次下载上限（该包 x64 资产约 55 MB，限制 500 MB 防御异常响应）
MAX_DOWNLOAD_BYTES = 500 * 1024 * 1024


def _package_tarball_url(version: str) -> str:
    """返回 npm registry 的包 tarball 地址。"""
    short_name = PACKAGE_NAME.split("/")[-1]
    return f"{REGISTRY_BASE}/{PACKAGE_NAME}/-/{short_name}-{version}.tgz"


def _download(url: str) -> bytes:
    """下载 URL 内容并限制最大体积。"""
    request = urllib.request.Request(url, headers={"User-Agent": "daagent-fetch-mxc/1.0"})
    with urllib.request.urlopen(request, timeout=120) as response:  # noqa: S310（固定 registry 域名）
        declared = response.headers.get("Content-Length")
        if declared is not None and int(declared) > MAX_DOWNLOAD_BYTES:
            raise RuntimeError(f"包体积超出上限: {declared} 字节")
        data = response.read(MAX_DOWNLOAD_BYTES + 1)
    if len(data) > MAX_DOWNLOAD_BYTES:
        raise RuntimeError("下载内容超出上限，已中止")
    return data


def _sha256_of(path: Path) -> str:
    """计算文件 SHA-256。"""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fetch(version: str, arch: str) -> Path:
    """下载并部署指定版本的 MXC 资产，返回部署目录。"""
    url = _package_tarball_url(version)
    print(f"下载 {url}")
    payload = _download(url)
    target_root = VENDOR_ROOT / "bin" / arch
    prefix = f"package/bin/{arch}/"
    written = 0
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        for member in archive.getmembers():
            if not member.isfile() or not member.name.startswith(prefix):
                continue
            relative = member.name[len(prefix):]
            destination = target_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                continue
            destination.write_bytes(source.read())
            written += 1
    if written == 0:
        raise RuntimeError(f"包内未找到 {prefix} 资产，请核对版本与架构")
    print(f"已部署 {written} 个文件到 {target_root}")
    for name in KEY_FILES:
        candidate = target_root / name
        if candidate.is_file():
            print(f"  {name}: sha256={_sha256_of(candidate)}")
    return target_root


def verify(arch: str) -> int:
    """核对已部署资产完整性，返回进程退出码。"""
    target_root = VENDOR_ROOT / "bin" / arch
    if not target_root.is_dir():
        print(f"[失败] 资产目录不存在: {target_root}（先运行不带参数的 fetch_mxc.py）")
        return 1
    missing = [name for name in KEY_FILES if not (target_root / name).is_file()]
    if missing:
        print(f"[失败] 缺少关键文件: {', '.join(missing)}")
        return 1
    print(f"[通过] 关键文件齐全: {target_root}")
    for name in KEY_FILES:
        print(f"  {name}: sha256={_sha256_of(target_root / name)}")
    print("请与 vendor/mxc/VERSION.md 中的记录比对，或在 CI 中断言一致。")
    return 0


def main() -> int:
    """命令行入口。"""
    parser = argparse.ArgumentParser(description="锁定版本获取 MXC 原生资产")
    parser.add_argument("--arch", default="x64", choices=("x64", "arm64"), help="目标架构")
    parser.add_argument("--version", default=DEFAULT_VERSION, help="npm 包版本（默认锁定值）")
    parser.add_argument("--verify", action="store_true", help="仅核对已部署资产，不联网")
    args = parser.parse_args()
    if args.verify:
        return verify(args.arch)
    fetch(args.version, args.arch)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
