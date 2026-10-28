"""Sink 下游诊断：区分网络连通性与实际应用层健康。"""

from __future__ import annotations

import asyncio
import base64
import json
import urllib.request
from pathlib import Path

from core.application.diagnostic import DiagnosticFinding, DiagnosticResult, DiagnosticStatus
from core.infrastructure.config.adapter import YamlConfigAdapter
from sink_service.infrastructure.adapters.redis import RedisSink


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

    if sink.kind == "tdengine":
        return await _tdengine_readonly(target, connection, timeout)

    if sink.kind == "redis":
        adapter = RedisSink(sink)
        try:
            async with asyncio.timeout(timeout):
                await adapter.open()
                health = await adapter.health()
            return _result(
                target,
                DiagnosticStatus.PASS if health.healthy else DiagnosticStatus.FAIL,
                "SINK_HEALTH_OK" if health.healthy else "SINK_HEALTH_FAILED",
                health.message or "Sink application-level health checked",
            )
        except Exception as exc:
            return _result(
                target,
                DiagnosticStatus.FAIL,
                "SINK_CONNECT_FAILED",
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
            target,
            DiagnosticStatus.UNKNOWN,
            "SINK_SERVER_CONTEXT_REQUIRED",
            f"Modbus Sink is server-side ({host}:{port}); check running listener and remote read",
        )
    if sink.kind == "grpc":
        import grpc

        endpoint = str(connection.get("endpoint", "127.0.0.1:50073"))
        channel = grpc.aio.insecure_channel(endpoint)
        try:
            await asyncio.wait_for(channel.channel_ready(), timeout)
        except (grpc.RpcError, TimeoutError, OSError):
            return _result(
                target,
                DiagnosticStatus.FAIL,
                "GRPC_CHANNEL_UNAVAILABLE",
                "gRPC channel not ready",
            )
        finally:
            await channel.close()
        return _result(
            target,
            DiagnosticStatus.UNKNOWN,
            "GRPC_SERVICE_UNVERIFIED",
            "gRPC channel ready; destination application method not verified",
        )
    if sink.kind == "kafka":
        try:
            from aiokafka import AIOKafkaConsumer  # type: ignore[import-untyped]

            topic = str(connection.get("topic", ""))
            consumer = AIOKafkaConsumer(
                bootstrap_servers=str(connection.get("bootstrap_servers", "")),
                request_timeout_ms=max(1000, int(timeout * 1000)),
            )
            try:
                await asyncio.wait_for(consumer.start(), timeout)
                topics = await asyncio.wait_for(consumer.topics(), timeout)
            finally:
                await asyncio.wait_for(consumer.stop(), timeout)
        except Exception as exc:
            return _result(
                target,
                DiagnosticStatus.FAIL,
                "KAFKA_METADATA_FAILED",
                f"Kafka metadata request failed ({type(exc).__name__})",
            )
        if topic not in topics:
            return _result(
                target,
                DiagnosticStatus.FAIL,
                "KAFKA_TOPIC_NOT_FOUND",
                "Configured topic is absent from broker metadata",
            )
        return _result(
            target,
            DiagnosticStatus.UNKNOWN,
            "KAFKA_WRITE_UNVERIFIED",
            "Kafka metadata and topic visible; produce ACL and delivery not verified",
        )
    return _result(
        target,
        DiagnosticStatus.UNKNOWN,
        "SINK_PROTOCOL_NOT_VERIFIED",
        "No safe application-level diagnostic available without a resolved runtime instance",
    )


def _result(
    target: str,
    status: DiagnosticStatus,
    code: str,
    message: str,
) -> DiagnosticResult:
    return DiagnosticResult(
        category="sink",
        target=target,
        status=status,
        findings=(DiagnosticFinding(code=code, status=status, message=message),),
    )


async def benchmark_sink(
    config: str | Path,
    sink_id: str,
    *,
    samples: int = 10,
    interval: float = 0.2,
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

    measured = await benchmark(sink_id, probe, samples=samples, interval=interval)
    return DiagnosticResult(
        category="sink",
        target=measured.target,
        status=measured.status,
        findings=measured.findings,
        measurements={**measured.measurements, "measurement": "health_probe_latency_only"},
        evidence=measured.evidence,
    )


async def _tdengine_readonly(
    target: str,
    connection: object,
    timeout: float,
) -> DiagnosticResult:
    """只读查询 TDengine 版本，不创建表、不插入诊断点。"""
    from collections.abc import Mapping

    if not isinstance(connection, Mapping):
        return _result(target, DiagnosticStatus.FAIL, "SINK_CONFIG_INVALID", "Invalid connection")
    host = str(connection.get("host", "localhost"))
    port = int(connection.get("port", 6041))
    database = str(connection.get("database", "wind_hub"))
    scheme = "https" if connection.get("tls", False) else "http"
    username = str(connection.get("username", "root"))
    secret = connection.get("password")
    if secret is None:
        return _result(
            target, DiagnosticStatus.FAIL, "SINK_CREDENTIALS_MISSING", "Password missing"
        )
    password = secret.get_secret_value() if hasattr(secret, "get_secret_value") else str(secret)
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    url = f"{scheme}://{host}:{port}/rest/sql/{database}"

    def query() -> bool:
        request = urllib.request.Request(
            url,
            data=b"SELECT SERVER_VERSION()",
            headers={"Authorization": f"Basic {token}", "Content-Type": "text/plain"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read())
        return isinstance(body, dict) and body.get("code") == 0

    try:
        ok = await asyncio.wait_for(asyncio.to_thread(query), timeout=timeout + 1)
    except Exception as exc:
        return _result(
            target,
            DiagnosticStatus.FAIL,
            "TDENGINE_QUERY_FAILED",
            f"Read-only query failed ({type(exc).__name__})",
        )
    return _result(
        target,
        DiagnosticStatus.PASS if ok else DiagnosticStatus.FAIL,
        "TDENGINE_QUERY_OK" if ok else "TDENGINE_QUERY_REJECTED",
        "Read-only TDengine SQL query confirmed" if ok else "TDengine rejected query",
    )
