"""Sink 下游诊断：区分网络连通性与实际应用层健康。"""

from __future__ import annotations

import asyncio
from pathlib import Path

from core.application.diagnostic import DiagnosticFinding, DiagnosticResult, DiagnosticStatus
from core.infrastructure.config.adapter import YamlConfigAdapter
from core.infrastructure.diagnostic.network import tcp_probe
from core.infrastructure.sink.redis import RedisSink
from core.infrastructure.sink.tdengine import TDengineSink


async def check_sink(
    config: str | Path,
    sink_id: str,
    *,
    timeout: float = 5.0,
) -> DiagnosticResult:
    """只读诊断；不发送模拟业务点或启动 Modbus TCP Server。"""
    snapshot = YamlConfigAdapter(config).load()
    sink = snapshot.sinks.get(sink_id)
    if sink is None:
        raise ValueError(f"unknown sink: {sink_id}")
    connection = sink.connection
    target = f"{sink_id} ({sink.kind})"
    if not sink.enabled:
        return _result(target, DiagnosticStatus.SKIPPED, "SINK_DISABLED", "Sink is disabled")

    if sink.kind in {"redis", "tdengine"}:
        adapter = RedisSink(sink) if sink.kind == "redis" else TDengineSink(sink)
        try:
            async with asyncio.timeout(timeout):
                await adapter.open()
                health = await adapter.health()
            return _result(
                target, DiagnosticStatus.PASS if health.healthy else DiagnosticStatus.FAIL,
                "SINK_HEALTH_OK" if health.healthy else "SINK_HEALTH_FAILED",
                health.message or "Sink application-level health checked",
            )
        except Exception as exc:
            return _result(
                target, DiagnosticStatus.FAIL, "SINK_CONNECT_FAILED",
                f"{type(exc).__name__}: {exc}",
            )
        finally:
            try:
                async with asyncio.timeout(timeout):
                    await adapter.close()
            except Exception:
                pass

    if sink.kind == "modbus":
        host = str(connection.get("host", "0.0.0.0"))
        port = int(connection.get("port", 502))
        return _result(
            target, DiagnosticStatus.UNKNOWN, "SINK_SERVER_CONTEXT_REQUIRED",
            f"Modbus Sink is server-side ({host}:{port}); check running listener and remote read",
        )
    if sink.kind == "grpc":
        endpoint = str(connection.get("endpoint", "127.0.0.1:50073"))
        host, separator, port = endpoint.rpartition(":")
        if separator and host and port.isdecimal():
            probe = await tcp_probe(host.strip("[]"), int(port), timeout=timeout)
            return _result(
                target, DiagnosticStatus.UNKNOWN, "SINK_GRPC_UNVERIFIED",
                f"TCP connectivity: {probe.status.value}; gRPC service contract not verified",
            )
    if sink.kind == "kafka":
        servers = str(connection.get("bootstrap_servers", ""))
        first = servers.split(",")[0].strip()
        host, separator, port = first.rpartition(":")
        if separator and host and port.isdecimal():
            probe = await tcp_probe(host, int(port), timeout=timeout)
            return _result(
                target, DiagnosticStatus.UNKNOWN, "SINK_KAFKA_UNVERIFIED",
                f"Broker TCP connectivity: {probe.status.value}; Topic/ACL not verified",
            )
    return _result(
        target, DiagnosticStatus.UNKNOWN, "SINK_PROTOCOL_NOT_VERIFIED",
        "No safe application-level diagnostic available without a resolved runtime instance",
    )


def _result(
    target: str, status: DiagnosticStatus, code: str, message: str,
) -> DiagnosticResult:
    return DiagnosticResult(
        category="sink", target=target, status=status,
        findings=(DiagnosticFinding(code=code, status=status, message=message),),
    )


async def benchmark_sink(
    config: str | Path, sink_id: str, *, samples: int = 10, interval: float = 0.2,
    timeout: float = 5.0,
) -> DiagnosticResult:
    """只对已支持的无副作用应用层健康检查测速，绝不发送业务测试数据。"""
    from core.infrastructure.diagnostic.benchmark import benchmark

    result = await check_sink(config, sink_id, timeout=timeout)
    if result.status is not DiagnosticStatus.PASS:
        return result

    async def probe() -> int:
        checked = await check_sink(config, sink_id, timeout=timeout)
        if checked.status is not DiagnosticStatus.PASS:
            raise RuntimeError("sink health probe failed")
        return 0

    return await benchmark(sink_id, probe, samples=samples, interval=interval)
