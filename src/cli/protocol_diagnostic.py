"""配置驱动的独立协议诊断；不借用旧 Core Diagnostic。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from typing import Any

from core.application.config_snapshot import ConfigSnapshot
from core.application.protocol_contract import (
    InterrogationRequest,
    ProtocolCapability,
    ProtocolSample,
    ProtocolWrite,
)
from core.application.port.protocol import ProtocolPort
from core.infrastructure.config.effective import resolve_effective_device
from core.infrastructure.protocol.ads.driver import ADSDriver
from core.infrastructure.protocol.iec104.driver import IEC104Driver
from core.infrastructure.protocol.modbus.driver import ModbusDriver


def create_driver(snapshot: ConfigSnapshot, device_id: str) -> ProtocolPort[Any]:
    """从有效配置创建专用诊断 Driver；绝不复用生产连接。"""
    device = next(
        (item for key, item in snapshot.site.devices.items() if str(key) == device_id),
        None,
    )
    if device is None:
        raise ValueError(f"unknown device: {device_id}")
    model = snapshot.device_models[device.device_model_id]
    table = snapshot.point_tables[model.point_table_id]
    resolved, options = resolve_effective_device(device, model, table.protocol.name)
    if table.protocol.name == "ads":
        return ADSDriver(resolved.endpoint, table, options)
    if table.protocol.name == "modbus":
        return ModbusDriver(resolved.endpoint, table, options)
    if table.protocol.name == "iec104":
        return IEC104Driver(resolved.endpoint, table, options)
    raise ValueError(f"unsupported protocol: {table.protocol.name}")


def sample_dict(sample: ProtocolSample) -> dict[str, Any]:
    return {
        "point_id": sample.point_id,
        "value": sample.value,
        "quality": sample.quality.value,
        "timestamp": sample.timestamp.isoformat(),
        "timestamp_source": sample.timestamp_source,
    }


async def execute(
    snapshot: ConfigSnapshot,
    device_id: str,
    operation: str,
    *,
    point_ids: Sequence[str] = (),
    value: bool | int | float | str | None = None,
    interval: float | None = None,
    count: int = 1,
    timeout: float = 5.0,
    common_address: int | None = None,
    on_sample: Callable[[dict[str, Any]], None] | None = None,
) -> list[dict[str, Any]]:
    """执行单个操作；连续操作复用连接，取消时必须释放资源。"""
    if timeout <= 0 or count < 0 or (interval is not None and interval <= 0):
        raise ValueError("timeout/count/interval must be positive")
    if operation == "read" and len(point_ids) != 1:
        raise ValueError("read requires exactly one point; use read-many for batches")
    if operation == "read-many" and not point_ids:
        raise ValueError("read-many requires at least one point")
    driver = create_driver(snapshot, device_id)
    results: list[dict[str, Any]] = []
    try:
        async with asyncio.timeout(timeout):
            await driver.connect()
        caps = driver.capabilities()
        if operation == "check":
            async with asyncio.timeout(timeout):
                health = await driver.health()
            if not health.healthy:
                raise RuntimeError(health.message or "protocol unhealthy")
            return [{"healthy": True, "message": health.message}]
        if operation == "write":
            if ProtocolCapability.WRITE not in caps:
                raise ValueError("protocol does not support write")
            if len(point_ids) != 1 or value is None:
                raise ValueError("write requires one configured point and a non-null value")
            async with asyncio.timeout(timeout):
                outcome = await driver.write_one(ProtocolWrite(point=point_ids[0], value=value))
            return [{"point_id": outcome.point_id, "outcome": outcome.outcome.value, "message": outcome.message}]
        if operation == "gi":
            if ProtocolCapability.INTERROGATE not in caps:
                raise ValueError("protocol does not support interrogation")
            async with asyncio.timeout(timeout):
                samples = await driver.interrogate(
                    InterrogationRequest(common_address=common_address)
                )
            return [sample_dict(sample) for sample in samples]
        if operation not in {"read", "read-many"}:
            raise ValueError(f"unsupported diagnostic operation: {operation}")
        if not point_ids:
            raise ValueError("read requires at least one point")
        if count == 0 and interval is None:
            raise ValueError("unbounded read requires --interval")
        if operation == "read-many" and ProtocolCapability.READ_MANY not in caps:
            raise ValueError("protocol has no native read_many capability")
        if operation == "read" and ProtocolCapability.READ not in caps:
            raise ValueError("protocol has no read capability")
        index = 0
        while count == 0 or index < count:
            async with asyncio.timeout(timeout):
                if operation == "read":
                    samples = (await driver.read_one(point_ids[0]),)
                else:
                    samples = await driver.read_many(point_ids)
            for sample in samples:
                record = sample_dict(sample)
                if on_sample is not None:
                    on_sample(record)
                else:
                    results.append(record)
            index += 1
            if interval is not None and (count == 0 or index < count):
                await asyncio.sleep(interval)
        return results
    finally:
        try:
            async with asyncio.timeout(timeout):
                await driver.close()
        except Exception:
            # 不应把清理失败误认为成功；对已经发生的协议结果保留原始异常。
            import logging

            logging.getLogger(__name__).exception("diagnostic driver cleanup failed")
