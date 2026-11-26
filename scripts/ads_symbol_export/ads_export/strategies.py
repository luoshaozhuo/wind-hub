"""三种读取方法的调度与失败隔离。"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import connection, file_service, runtime
from .config import Settings, validate_method
from .connection import _ConnectionManager
from .runtime import _SYMBOL_HEADER_SIZE

_LOG = logging.getLogger(__name__)


@dataclass
class StrategyOutcome:
    name: str
    ok: bool
    source: str = "runtime"  # runtime | file
    count: int = 0
    # 验证等级：runtime_verified | content_matched | count_matched | unverified
    verification: str = "runtime_verified"
    reason: str = ""

    def describe(self) -> str:
        if self.ok:
            return (
                f"{self.name}: 成功, source={self.source}, count={self.count}, "
                f"verification={self.verification}"
            )
        return f"{self.name}: {self.reason}"

@dataclass
class _RunContext:
    conn: _ConnectionManager
    output: Path
    settings: Settings
    file_service_factory: Callable[[], Any] | None = None
    outcomes: list[StrategyOutcome] = field(default_factory=list)
    read_method: str = "auto"

def _run_strategies(ctx: _RunContext) -> int:
    """按优先级执行方案，成功即停；每个方案独立管理连接与资源。"""
    outcomes = ctx.outcomes
    settings = ctx.settings
    method = ctx.read_method

    def record(name: str, source: str, run: Callable[[], int], available: str = "") -> int | None:
        if available:
            outcomes.append(StrategyOutcome(name, False, source, reason=available))
            return None
        try:
            count = run()
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
            outcomes.append(StrategyOutcome(name, False, source, reason=reason))
            _LOG.warning("方案失败：%s: %s", name, reason)
            return None
        outcomes.append(StrategyOutcome(name, True, source, count=count))
        _LOG.info("方案成功：%s", name)
        return count

    # 方案 1：ADS 整表
    if ctx.conn.current is not None and (
        method == "full" or (method == "auto" and settings.TRY_FULL_TABLE_FIRST)
    ):
        _LOG.warning("先尝试读取完整符号表；已知部分 PLC 存在内存溢出风险")
        result = record(
            "full-table", "runtime", lambda: runtime._export_full_table(ctx.conn.current, ctx.output, settings)
        )
        if result is not None:
            return result
        if method == "auto" and not ctx.conn.recover():
            outcomes.append(
                StrategyOutcome(
                    "runtime-reconnect", False, reason="连接断开且无法恢复，跳过运行时方案"
                )
            )

    if method == "full":
        raise RuntimeError("指定 full 方法读取失败：\n" + "\n".join(o.describe() for o in outcomes))

    # 方案 2：ADS Chunk 分段
    if method in ("auto", "chunk") and ctx.conn.usable():
        for chunk_size in settings.CHUNK_SIZES:
            if chunk_size < _SYMBOL_HEADER_SIZE + 3:
                outcomes.append(StrategyOutcome(f"chunk={chunk_size}", False, reason="配置值过小"))
                continue
            result = record(
                f"chunk={chunk_size}",
                "runtime",
                lambda s=chunk_size: runtime._export_in_chunks(ctx.conn.current, ctx.output, s, settings),
            )
            if result is not None:
                return result
            # 区分协议拒绝与连接失效；失效连接不得继续发送请求
            if not ctx.conn.recover():
                outcomes.append(
                    StrategyOutcome(
                        "runtime-reconnect", False, reason="连接断开且无法恢复，停止分段尝试"
                    )
                )
                break
    elif method in ("auto", "chunk"):
        outcomes.append(StrategyOutcome("chunk", False, reason="连接不可用，跳过分段方案"))

    if method == "chunk":
        raise RuntimeError("指定 chunk 方法读取失败：\n" + "\n".join(o.describe() for o in outcomes))

    # 方案 3：ADS 文件服务（独立 System Service 端口，PLC Runtime 失效也可尝试）
    runtime_count: int | None = None
    if method == "auto" and ctx.conn.usable():
        try:
            runtime_count, _ = runtime._read_symbol_table_info(ctx.conn.current, settings)
        except Exception:
            runtime_count = None
    factory = ctx.file_service_factory
    unavailable = ""
    if method == "auto" and not settings.FILE_SERVICE_ENABLED:
        unavailable = "文件服务方案已在配置中禁用"
    elif factory is None:
        unavailable = "未提供 System Service 连接工厂"
    if not unavailable:
        assert factory is not None
        reachable, unavailable = connection._probe_file_service(factory)
        if reachable:
            unavailable = ""
    if method in ("auto", "file") and unavailable:
        outcomes.append(StrategyOutcome("ads-file", False, "file", reason=unavailable))
    elif method in ("auto", "file"):
        assert factory is not None

        def run_file() -> int:
            count, verification = file_service._export_via_file_service(
                factory, ctx.output, runtime_count=runtime_count, settings=settings
            )
            outcomes.append(
                StrategyOutcome("ads-file", True, "file", count=count, verification=verification)
            )
            return count

        try:
            return run_file()
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
            outcomes.append(StrategyOutcome("ads-file", False, "file", reason=reason))
            _LOG.warning("方案失败：ads-file: %s", reason)

    if method == "file":
        raise RuntimeError("指定 file 方法读取失败：\n" + "\n".join(o.describe() for o in outcomes))
    raise RuntimeError("所有已启用的枚举策略均失败：\n" + "\n".join(o.describe() for o in outcomes))


def export_symbols(
    plc: Any,
    output: Path,
    *,
    settings: Settings,
    reconnect: Callable[[], Any] | None = None,
    file_service_factory: Callable[[], Any] | None = None,
    outcomes: list[StrategyOutcome] | None = None,
    owns_connection: bool = False,
    read_method: str | None = None,
) -> int:
    """先整表、后分段、再文件服务；成功方案的结果必须校验完整。

    连接所有权：外部传入的 plc 默认归调用方所有，本函数不关闭；
    owns_connection=True 时由本函数负责最终释放当前实际连接。
    reconnect 工厂创建的连接始终归本函数所有并保证释放。
    """
    method = settings.READ_METHOD if read_method is None else read_method
    validate_method(settings, method)
    manager = _ConnectionManager(plc, reconnect, owns_initial=owns_connection, settings=settings)
    ctx = _RunContext(
        conn=manager,
        output=output,
        settings=settings,
        file_service_factory=file_service_factory,
        outcomes=outcomes if outcomes is not None else [],
        read_method=method,
    )
    try:
        return _run_strategies(ctx)
    finally:
        manager.close()
