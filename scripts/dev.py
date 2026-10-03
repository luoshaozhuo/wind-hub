"""统一 VS Code Coding Agent 的本地开发工具入口。

本脚本只依赖 Python 标准库，检查并使用 Agent 启动时激活的环境。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def resolve_executable(kind: str) -> str:
    """使用当前 Python 解释器或 PATH 中的前端工具。"""
    if kind == "python":
        return sys.executable

    if kind in {"node", "npm"}:
        return shutil.which(kind) or kind

    raise ValueError(f"unsupported executable kind: {kind}")


def _check(label: str, command: Sequence[str]) -> bool:
    """执行单项环境检查并输出结果。"""
    try:
        result = subprocess.run(
            list(command),
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"[FAIL] {label}: {exc}")
        return False

    output = (result.stdout or result.stderr).strip().splitlines()
    detail = output[0] if output else f"exit={result.returncode}"
    status = "PASS" if result.returncode == 0 else "FAIL"
    print(f"[{status}] {label}: {detail}")
    return result.returncode == 0


def check_environment(*, frontend: bool) -> int:
    """检查后端开发环境，并按需检查前端工具链。"""
    python = resolve_executable("python")
    print(f"Repository: {REPO_ROOT}")
    print(f"Python executable: {python}")
    conda_prefix = os.environ.get("CONDA_PREFIX")
    if (
        os.environ.get("CONDA_DEFAULT_ENV") != "wind-hub"
        or not conda_prefix
        or Path(sys.prefix).resolve() != Path(conda_prefix).resolve()
        or Path(python).resolve().parent != Path(conda_prefix).resolve() / "bin"
    ):
        print("[FAIL] 当前 Python 未运行在已激活的 wind-hub Conda 环境中")
        print("RESULT: FAIL")
        return 2

    checks = [
        (
            "Python >= 3.11",
            [
                python,
                "-c",
                "import sys; assert sys.version_info >= (3, 11), sys.version; "
                "print(sys.version.split()[0])",
            ],
        ),
        ("pytest", [python, "-m", "pytest", "--version"]),
        ("ruff", [python, "-m", "ruff", "--version"]),
        ("mypy", [python, "-m", "mypy", "--version"]),
        (
            "import-linter",
            [python, "-c", "import importlinter; print('available')"],
        ),
    ]

    passed = True
    for label, command in checks:
        passed = _check(label, command) and passed

    if frontend:
        node = resolve_executable("node")
        npm = resolve_executable("npm")
        print(f"Node executable: {node}")
        print(f"npm executable: {npm}")
        passed = _check("node", [node, "--version"]) and passed
        passed = _check("npm", [npm, "--version"]) and passed

    print(f"RESULT: {'PASS' if passed else 'FAIL'}")
    return 0 if passed else 2


def run_configured(kind: str, arguments: Sequence[str]) -> int:
    """使用当前环境中的可执行文件运行透传参数。"""
    executable = resolve_executable(kind)
    forwarded = list(arguments)
    if forwarded[:1] == ["--"]:
        forwarded = forwarded[1:]

    try:
        completed = subprocess.run(
            [executable, *forwarded],
            cwd=REPO_ROOT,
            check=False,
        )
    except OSError as exc:
        print(f"failed to execute {executable}: {exc}", file=sys.stderr)
        return 2
    return completed.returncode


def build_parser() -> argparse.ArgumentParser:
    """构建命令行参数解析器。"""
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    env_parser = subparsers.add_parser("env", help="检查本地开发环境")
    env_parser.add_argument("--frontend", action="store_true")

    resolve_parser = subparsers.add_parser("resolve", help="输出实际可执行文件路径")
    resolve_parser.add_argument("kind", choices=("python", "node", "npm"))

    for kind in ("python", "node", "npm"):
        run_parser = subparsers.add_parser(kind, help=f"使用当前环境的 {kind} 执行命令")
        run_parser.add_argument("arguments", nargs=argparse.REMAINDER)

    return parser


def main() -> int:
    """执行开发环境工具入口。"""
    args = build_parser().parse_args()

    if args.command == "env":
        return check_environment(frontend=args.frontend)
    if args.command == "resolve":
        print(resolve_executable(args.kind))
        return 0
    if args.command in {"python", "node", "npm"}:
        return run_configured(args.command, args.arguments)

    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
