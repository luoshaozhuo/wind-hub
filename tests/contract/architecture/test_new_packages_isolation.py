"""新 Collector/Commander/Core 架构隔离回归测试。

静态源码扫描 + git 基线比对，作为 import-linter 之外的独立防线：

- 新包（core/collector/commander）不得 import 任何 wind_hub_* 旧包；
- collector 与 commander 不得互相 import；
- core 保持配置纯净（不引入 yaml/grpc/配置仓储）；
- 旧 wind_hub_* 生产代码相对任务基线零改动；
- 进程内 proto 副本与旧共享 proto 逐字节一致（wire contract 兼容）。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SRC = REPO_ROOT / "src"
NEW_PACKAGES = (SRC / "core", SRC / "collector", SRC / "commander")
LEGACY_PACKAGES = (
    "src/wind_hub_core",
    "src/wind_hub_collector",
    "src/wind_hub_commander",
)
BASELINE = "5d08aea75ad60c4d0924a353804c2e0572d970ed"

_IMPORT_RE = re.compile(r"^\s*(?:from|import)\s+([a-zA-Z0-9_\.]+)")


def _python_files(package: Path) -> list[Path]:
    return sorted(package.rglob("*.py"))


def _imported_roots(path: Path) -> set[str]:
    roots: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        match = _IMPORT_RE.match(line)
        if match:
            roots.add(match.group(1).split(".")[0])
    return roots


@pytest.mark.parametrize("package", NEW_PACKAGES, ids=lambda p: p.name)
def test_new_packages_do_not_import_legacy(package: Path) -> None:
    offenders: list[str] = []
    for path in _python_files(package):
        roots = _imported_roots(path)
        legacy = roots & {"wind_hub_core", "wind_hub_collector", "wind_hub_commander"}
        if legacy:
            offenders.append(f"{path.relative_to(REPO_ROOT)}: {sorted(legacy)}")
    assert not offenders, "新包 import 了旧 wind_hub_* 包：\n" + "\n".join(offenders)


def test_collector_commander_no_cross_import() -> None:
    pairs = ((SRC / "collector", "commander"), (SRC / "commander", "collector"))
    offenders: list[str] = []
    for package, forbidden in pairs:
        for path in _python_files(package):
            if forbidden in _imported_roots(path):
                offenders.append(str(path.relative_to(REPO_ROOT)))
    assert not offenders, "进程间交叉 import：\n" + "\n".join(offenders)


def test_core_config_purity() -> None:
    """core 不得引入 YAML/gRPC/指纹等配置基础设施（留在进程包内）。"""
    offenders: list[str] = []
    for path in _python_files(SRC / "core"):
        roots = _imported_roots(path)
        banned = roots & {"yaml", "grpc", "grpc_tools"}
        if banned:
            offenders.append(f"{path.relative_to(REPO_ROOT)}: {sorted(banned)}")
    assert not offenders, "core 引入了配置/传输基础设施：\n" + "\n".join(offenders)


def test_legacy_packages_unmodified_since_baseline() -> None:
    """旧 wind_hub_* 生产代码相对任务基线零改动（含未提交改动）。"""
    diff = subprocess.run(
        ["git", "diff", "--name-only", BASELINE, "--", *LEGACY_PACKAGES],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert not diff, f"旧包相对基线 {BASELINE[:8]} 被修改：\n{diff}"


def test_proto_copies_byte_identical_to_legacy() -> None:
    pairs = (
        (
            SRC / "wind_hub_core/rpc/collector.proto",
            SRC / "collector/infrastructure/grpc/collector.proto",
        ),
        (
            SRC / "wind_hub_core/rpc/commander.proto",
            SRC / "commander/infrastructure/grpc/commander.proto",
        ),
    )
    for legacy, copied in pairs:
        assert (
            copied.read_bytes() == legacy.read_bytes()
        ), f"{copied.relative_to(REPO_ROOT)} 与旧 proto 不一致，wire contract 可能漂移"
