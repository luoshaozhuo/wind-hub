"""Explicit confirmation for CLI operations with remote side effects."""

from __future__ import annotations

import sys

import typer


def confirm_mutation(description: str, yes: bool, *, command: str = "mutation") -> None:
    """Never send a mutation after a declined or unavailable confirmation."""
    if yes:
        return
    if not sys.stdin.isatty():
        # 单次命令模式下不允许裸 ValueError 逃逸成 traceback：统一收敛为
        # 结构化错误 + 退出码 2（参数/配置/权限类失败）。
        from .main import runtime

        runtime().error(
            command,
            ValueError("interactive confirmation unavailable; pass --yes explicitly"),
        )
        raise typer.Exit(code=2)
    if not typer.confirm(description, default=False):
        raise typer.Abort()
