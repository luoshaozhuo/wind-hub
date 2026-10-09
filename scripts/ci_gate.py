"""wind-hub 本地与 GitHub CI 共用的质量门禁执行器。"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEV_TOOL = REPO_ROOT / "scripts" / "dev.py"
FRONTEND_DIR = REPO_ROOT / "src" / "wind-hub-admin"

PR_TARGETS: dict[str, tuple[str, ...]] = {
    "integration-protocol": ("tests/integration/protocols",),
    "integration-rpc": ("tests/integration/rpc",),
    "integration-sinks": ("tests/integration/sinks",),
    "system-acquisition": ("tests/system/acquisition",),
    "system-command": ("tests/system/command",),
    "system-diagnostics": ("tests/system/diagnostics",),
    "system-reload": ("tests/system/reload",),
    "system-startup": ("tests/system/startup",),
    "system-task-control": ("tests/system/task_control",),
    "system-e2e": ("tests/system/e2e",),
}


def _run(label: str, command: Sequence[str], *, cwd: Path = REPO_ROOT) -> bool:
    """执行一个门禁命令并保留原始输出。"""
    print(f"\n==> {label}")
    print("$ " + " ".join(command), flush=True)
    try:
        completed = subprocess.run(list(command), cwd=cwd, check=False)
    except OSError as exc:
        print(f"[FAIL] {label}: {exc}", file=sys.stderr)
        return False

    if completed.returncode == 0:
        print(f"[PASS] {label}")
        return True

    print(f"[FAIL] {label}: exit={completed.returncode}", file=sys.stderr)
    return False


def _python(*args: str) -> list[str]:
    """使用当前解释器生成 Python 命令。"""
    return [sys.executable, *args]


def _python_module(module: str, *args: str) -> list[str]:
    """使用当前解释器执行 Python module。"""
    return [sys.executable, "-m", module, *args]


def _tool(name: str, *args: str) -> list[str]:
    """解析与当前 Python 环境同源的 console script。"""
    sibling = Path(sys.executable).with_name(name)
    if sibling.is_file():
        return [str(sibling), *args]
    found = shutil.which(name)
    if found:
        return [found, *args]
    return [name, *args]


def _npm(*args: str) -> list[str]:
    """通过统一开发环境入口执行 npm。"""
    return [sys.executable, str(DEV_TOOL), "npm", "--", *args]


def _run_many(commands: Sequence[tuple[str, Sequence[str]]]) -> bool:
    """运行一组检查并汇总结果，不因单项失败隐藏后续问题。"""
    passed = True
    for label, command in commands:
        passed = _run(label, command) and passed
    return passed


def _isolated_tests(root_name: str) -> bool:
    """各子包独立收集，隔离并行迁移期同名 Protobuf 描述符。

    不跳过任何测试：目录与顶层测试文件均逐个启动新的 pytest 进程。
    迁移期新旧两包生成自同名 package 的 *_pb2（如
    ``wind_hub_core.rpc.collector_pb2`` 与
    ``collector.infrastructure.grpc.collector_pb2``）在同一进程的默认
    descriptor pool 中互斥；隔离运行是运行环境隔离，不修改任何测试。
    """
    root = REPO_ROOT / "tests" / root_name
    children = sorted(
        path
        for path in root.iterdir()
        if (path.is_dir() and path.name != "__pycache__")
        or (path.suffix == ".py" and path.name != "__init__.py")
    )
    return _run_many(
        [
            (
                f"{root_name}/{path.name}",
                _python_module("pytest", str(path.relative_to(REPO_ROOT)), "-q"),
            )
            for path in children
        ]
    )


def fast_gate(part: str, targets: str) -> bool:
    """执行 Fast Gate。"""
    if part == "backend-static":
        return _run_many(
            [
                ("ruff", _python_module("ruff", "check", "src", "tests", "scripts")),
                ("mypy", _python_module("mypy", "src")),
                ("import-linter", _tool("lint-imports")),
            ]
        )

    if part == "openapi-drift":
        return _run(
            "openapi-drift",
            _python(str(REPO_ROOT / "scripts" / "generate_api_contract.py"), "--check"),
        )

    if part == "backend-tests":
        selected = [target.strip() for target in targets.split(",") if target.strip()]
        valid = {"unit", "component", "contract"}
        unknown = [target for target in selected if target not in valid]
        if unknown:
            print(f"unknown Fast targets: {', '.join(unknown)}", file=sys.stderr)
            return False
        if not selected:
            print("GATE RESULT: NOT_APPLICABLE")
            return True
        # unit/component 均含并行迁移期同名 pb2 的测试包（如
        # tests/component/ctl 引旧 collector_pb2，tests/component/collector
        # 引新 collector_pb2），单进程全目录收集必然 duplicate symbol；
        # 与 unit 相同的逐子包隔离执行。contract 无此冲突，保持单进程。
        checks = [
            (target, _python_module("pytest", f"tests/{target}", "-q"))
            for target in selected
            if target not in {"unit", "component"}
        ]
        other_passed = _run_many(checks)
        unit_passed = _isolated_tests("unit") if "unit" in selected else True
        component_passed = _isolated_tests("component") if "component" in selected else True
        return other_passed and unit_passed and component_passed

    if part == "frontend":
        return _run_many(
            [
                ("frontend-lint", _npm("--prefix", str(FRONTEND_DIR), "run", "lint")),
                ("frontend-format", _npm("--prefix", str(FRONTEND_DIR), "run", "format:check")),
                ("frontend-vitest", _npm("--prefix", str(FRONTEND_DIR), "test")),
                ("frontend-build", _npm("--prefix", str(FRONTEND_DIR), "run", "build")),
            ]
        )

    raise ValueError(f"unsupported Fast part: {part}")


def pr_gate(targets: str) -> bool:
    """执行由变更风险选择的后端 integration/system 目标。"""
    selected = [target.strip() for target in targets.split(",") if target.strip()]
    if not selected:
        print("GATE RESULT: NOT_APPLICABLE")
        return True

    unknown = [target for target in selected if target not in PR_TARGETS]
    if unknown:
        print(f"unknown PR targets: {', '.join(unknown)}", file=sys.stderr)
        return False

    paths: list[str] = []
    for target in selected:
        paths.extend(PR_TARGETS[target])

    unique_paths = list(dict.fromkeys(paths))
    return _run(
        "targeted-pr",
        _python_module("pytest", *unique_paths, "-q"),
    )


def frontend_e2e_gate() -> bool:
    """执行当前 Playwright 应用外壳 E2E。"""
    return _run(
        "frontend-e2e",
        _npm("--prefix", str(FRONTEND_DIR), "run", "test:e2e"),
    )


def release_gate(part: str) -> bool:
    """执行 Release Gate。"""
    commands: dict[str, list[tuple[str, Sequence[str]]]] = {
        "backend": [
            ("ruff", _python_module("ruff", "check", "src", "tests", "scripts")),
            ("mypy", _python_module("mypy", "src")),
            ("import-linter", _tool("lint-imports")),
            (
                "backend-full",
                _python_module(
                    "pytest",
                    "tests",
                    "-q",
                    "-m",
                    "not hardware and not performance and not soak",
                ),
            ),
        ],
        "frontend": [
            ("frontend-build", _npm("--prefix", str(FRONTEND_DIR), "run", "build")),
            ("frontend-vitest", _npm("--prefix", str(FRONTEND_DIR), "test")),
            ("frontend-e2e", _npm("--prefix", str(FRONTEND_DIR), "run", "test:e2e")),
        ],
    }
    selected = commands if part == "all" else {part: commands[part]}
    return all(_run_many(items) for items in selected.values())


def hardware_gate() -> bool:
    """执行真实 ADS hardware 测试。"""
    return _run(
        "ads-hardware",
        _python_module(
            "pytest",
            "tests/integration/protocols/ads",
            "-q",
            "-m",
            "hardware",
        ),
    )


def performance_gate(args: argparse.Namespace) -> bool:
    """执行 performance benchmark。"""
    command = _python("scripts/run_benchmark.py")
    if args.mode == "quick":
        command.append("--quick")
    if args.duration is not None:
        command.extend(["--duration", str(args.duration)])
    if args.output:
        command.extend(["--output", args.output])
    return _run("performance", command)


def soak_gate() -> bool:
    """执行 soak 测试。"""
    return _run(
        "soak",
        _python_module(
            "pytest",
            "tests/reliability/soak",
            "-q",
            "-m",
            "soak and not performance",
        ),
    )


def build_parser() -> argparse.ArgumentParser:
    """构建 CLI。"""
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="gate", required=True)

    fast = subparsers.add_parser("fast")
    fast.add_argument(
        "--part",
        choices=("backend-static", "backend-tests", "frontend", "openapi-drift"),
        required=True,
    )
    fast.add_argument("--targets", default="")

    pr = subparsers.add_parser("pr")
    pr.add_argument("--targets", default="")

    subparsers.add_parser("frontend-e2e")

    release = subparsers.add_parser("release")
    release.add_argument("--part", choices=("all", "backend", "frontend"), default="all")

    subparsers.add_parser("hardware")

    performance = subparsers.add_parser("performance")
    performance.add_argument("--mode", choices=("quick", "full"), default="quick")
    performance.add_argument("--duration", type=float)
    performance.add_argument("--output")

    subparsers.add_parser("soak")

    return parser


def main() -> int:
    """运行指定 Gate。"""
    args = build_parser().parse_args()

    if args.gate == "fast":
        passed = fast_gate(args.part, args.targets)
    elif args.gate == "pr":
        passed = pr_gate(args.targets)
    elif args.gate == "frontend-e2e":
        passed = frontend_e2e_gate()
    elif args.gate == "release":
        passed = release_gate(args.part)
    elif args.gate == "hardware":
        passed = hardware_gate()
    elif args.gate == "performance":
        passed = performance_gate(args)
    elif args.gate == "soak":
        passed = soak_gate()
    else:
        raise AssertionError(f"unhandled gate: {args.gate}")

    print(f"\nGATE RESULT: {'PASS' if passed else 'FAIL'}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
