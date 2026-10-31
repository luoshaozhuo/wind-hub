"""独立协议只读诊断，复用现有 Driver；不修改或持有生产会话。"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path

from core.application.diagnostic import (
    DiagnosticFinding,
    DiagnosticResult,
    DiagnosticStatus,
)
from core.application.port.protocol import ProtocolPort
from core.application.protocol_contract import ProtocolCapability
from core.infrastructure.config.effective import resolve_effective_device
from core.infrastructure.config.shared_documents import (
    SharedConfigPaths,
    SharedDocumentLoader,
)
from core.infrastructure.config.shared_snapshot import build_shared_snapshot
from core.infrastructure.diagnostic.benchmark import benchmark


def _driver(config: str | Path, device_id: str) -> ProtocolPort:
    root = Path(config)
    documents = SharedDocumentLoader(
        SharedConfigPaths(
            instances=root / "instances",
            templates=root / "templates",
        )
    ).load()
    snapshot = build_shared_snapshot(documents)
    device = next((d for key, d in snapshot.site.devices.items() if str(key) == device_id), None)
    if device is None:
        raise ValueError(f"unknown device: {device_id}")
    model = snapshot.device_models[device.device_model_id]
    table = snapshot.point_tables[model.point_table_id]
    resolved, options = resolve_effective_device(device, model, table.protocol.name)
    if table.protocol.name == "ads":
        from core.infrastructure.protocol.ads.driver import ADSDriver

        return ADSDriver(resolved.endpoint, table, options)
    if table.protocol.name == "modbus":
        from core.infrastructure.protocol.modbus.driver import ModbusDriver

        return ModbusDriver(resolved.endpoint, table, options)
    if table.protocol.name == "iec104":
        from core.infrastructure.protocol.iec104.driver import IEC104Driver

        return IEC104Driver(resolved.endpoint, table, options)
    raise ValueError(f"unsupported protocol: {table.protocol.name}")


def _failure(target: str, exc: Exception) -> DiagnosticResult:
    return DiagnosticResult(
        category="protocol",
        target=target,
        status=DiagnosticStatus.FAIL,
        findings=(
            DiagnosticFinding(
                code="PROTOCOL_DIAGNOSTIC_FAILED",
                status=DiagnosticStatus.FAIL,
                message=f"{type(exc).__name__}: {exc}",
            ),
        ),
    )


async def check_protocol(
    config: str | Path,
    device_id: str,
    *,
    point_id: str | None = None,
    timeout: float = 5.0,
) -> DiagnosticResult:
    """连接、协议健康与可选单点读取；不执行写入、订阅或总召。"""
    driver: ProtocolPort | None = None
    try:
        driver = _driver(config, device_id)
        async with asyncio.timeout(timeout):
            await driver.connect()
            health = await driver.health()
            if not health.healthy:
                raise RuntimeError(health.message or "protocol health check failed")
            evidence: dict[str, object] = {"connected": True, "healthy": True}
            if point_id is not None:
                if ProtocolCapability.READ not in driver.capabilities():
                    raise ValueError("driver does not support point read")
                sample = await driver.read_one(point_id)
                evidence["point"] = {
                    "point_id": sample.point_id,
                    "value": sample.value,
                    "quality": sample.quality.value,
                    "timestamp": sample.timestamp.isoformat(),
                }
        return DiagnosticResult(
            category="protocol",
            target=device_id,
            status=DiagnosticStatus.PASS,
            findings=(
                DiagnosticFinding(
                    code="PROTOCOL_OK",
                    status=DiagnosticStatus.PASS,
                    message="Protocol handshake and health verified",
                ),
            ),
            evidence=evidence,
        )
    except Exception as exc:
        return _failure(device_id, exc)
    finally:
        if driver is not None:
            try:
                async with asyncio.timeout(timeout):
                    await driver.close()
            except Exception:
                # 关闭失败不覆盖原诊断结果；异常详情由独立日志记录。
                import logging

                logging.getLogger(__name__).exception("Diagnostic driver close failed")


async def benchmark_protocol(
    config: str | Path,
    device_id: str,
    point_ids: Sequence[str],
    *,
    samples: int = 10,
    interval: float = 0.2,
    timeout: float = 5.0,
) -> DiagnosticResult:
    """只读、串行、有上限的协议性能测试，拒绝空点列表。"""
    if not point_ids or len(point_ids) > 200 or len(set(point_ids)) != len(point_ids):
        raise ValueError("benchmark requires 1..200 unique point IDs")
    driver = _driver(config, device_id)
    try:
        async with asyncio.timeout(timeout):
            await driver.connect()
        capabilities = driver.capabilities()
        if len(point_ids) > 1 and ProtocolCapability.READ_MANY not in capabilities:
            raise ValueError("driver does not support read_many")
        if len(point_ids) == 1 and ProtocolCapability.READ not in capabilities:
            raise ValueError("driver does not support read_one")

        async def read() -> int:
            async with asyncio.timeout(timeout):
                if len(point_ids) == 1:
                    await driver.read_one(point_ids[0])
                    return 1
                return len(await driver.read_many(point_ids))

        return await benchmark(device_id, read, samples=samples, interval=interval)
    finally:
        async with asyncio.timeout(timeout):
            await driver.close()
