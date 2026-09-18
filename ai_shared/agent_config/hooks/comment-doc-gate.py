#!/usr/bin/env python3
"""按显式范围检查文档注释、类型抑制和危险异常模式。"""

from __future__ import annotations

import ast
import argparse
import re
import subprocess
import sys
from pathlib import Path


SUPPRESSION_PATTERNS = (
    "type: ignore",
    "noqa",
    "nolint",
    "NOLINT",
    "@ts-ignore",
    "shellcheck disable",
)


def staged_files() -> list[Path]:
    """返回 Git index 中存在且当前工作区仍可读取的 staged 文件。"""
    try:
        out = subprocess.check_output(
            ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMRT"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        return []
    return [Path(name) for name in sorted(out.splitlines()) if name and Path(name).is_file()]


def all_files() -> list[Path]:
    """返回当前工作区内已跟踪和 untracked 文件。"""
    names: set[str] = set()
    for args in (["git", "ls-files"], ["git", "ls-files", "--others", "--exclude-standard"]):
        try:
            out = subprocess.check_output(args, text=True, stderr=subprocess.DEVNULL)
        except Exception:
            continue
        names.update(line.strip() for line in out.splitlines() if line.strip())
    return [Path(name) for name in sorted(names) if Path(name).exists()]


def check_python(path: Path, failures: list[str], warnings: list[str]) -> None:
    """检查 Python public 接口 docstring 与异常模式。"""
    try:
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
    except Exception as exc:
        failures.append(f"{path}: 无法解析 Python 文件: {exc}")
        return

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            name = node.name
            is_public = not name.startswith("_")
            if is_public and ast.get_docstring(node) is None:
                warnings.append(f"{path}:{node.lineno}: public {type(node).__name__} `{name}` 缺少 docstring")
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and is_public:
                if node.returns is None:
                    warnings.append(f"{path}:{node.lineno}: public function `{name}` 缺少返回类型")
                for arg in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]:
                    if arg.arg in {"self", "cls"}:
                        continue
                    if arg.annotation is None:
                        warnings.append(f"{path}:{node.lineno}: public function `{name}` 参数 `{arg.arg}` 缺少类型")
        if isinstance(node, ast.ExceptHandler):
            if node.type is None:
                failures.append(f"{path}:{node.lineno}: 裸 except")
            elif isinstance(node.type, ast.Name) and node.type.id == "Exception":
                warnings.append(f"{path}:{node.lineno}: except Exception 需要确认是否记录/转换/重新抛出")

    for lineno, line in enumerate(text.splitlines(), start=1):
        if "type: ignore" in line and not re.search(r"type:\s*ignore(?:\[[^\]]+\])?\s*#\s*\S+", line):
            warnings.append(f"{path}:{lineno}: type ignore 缺少解释")


def check_text_patterns(path: Path, warnings: list[str]) -> None:
    """检查通用抑制指令是否缺少解释。"""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return
    for lineno, line in enumerate(text.splitlines(), start=1):
        for pattern in SUPPRESSION_PATTERNS:
            if pattern in line and "reason" not in line.lower() and "原因" not in line:
                warnings.append(f"{path}:{lineno}: `{pattern}` 可能缺少解释")


def main() -> int:
    """执行用户显式触发的注释与异常模式检查。"""
    if len(sys.argv) == 1:
        # 兼容当前会话已加载的旧自动调用；无显式范围时不执行检查。
        return 0
    parser = argparse.ArgumentParser()
    parser.add_argument("--scope", choices=("staged", "all"), required=True)
    args = parser.parse_args()
    failures: list[str] = []
    warnings: list[str] = []

    paths = staged_files() if args.scope == "staged" else all_files()
    for path in paths:
        if path.suffix == ".py":
            check_python(path, failures, warnings)
        check_text_patterns(path, warnings)

    for item in warnings:
        print(f"WARNING: {item}", file=sys.stderr)
    for item in failures:
        print(f"ERROR: {item}", file=sys.stderr)

    return 2 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
