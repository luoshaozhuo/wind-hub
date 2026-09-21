"""CLI 对共享 :class:`AppContext` 的适配层。

上下文本体在 application 层（:mod:`wind_hub.application.app_context`），
CLI 与 Web API 共享同一实例；本模块只提供 CLI 专属的
``get_context_or_exit``——上下文缺失时打印错误并以退出码 1 终止，
而 Web API 将其映射为 HTTP 503。
"""

from __future__ import annotations

from wind_hub.application.app_context import AppContext, get_context


def get_context_or_exit() -> AppContext:
    """Return the context, or print an error and exit(1).

    CLI-only convenience: the API instead maps a missing context to HTTP 503.
    """
    try:
        return get_context()
    except RuntimeError as exc:
        import typer

        from wind_hub.adapter.inbound.cli.output import print_error

        print_error(str(exc))
        raise typer.Exit(1) from exc


__all__ = ["get_context_or_exit"]
