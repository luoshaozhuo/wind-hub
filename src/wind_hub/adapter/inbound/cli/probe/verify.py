"""probe verify 的点表只读验证（决策 1-5/9-11）。

只读保证（决策 4/11）：驱动实例一律经 :class:`_ReadOnlyDriver` 包装
后再使用——``write`` / ``subscribe`` 在包装层直接抛错，verify 代码
路径上不存在触发写/订阅的可能（结构性保证，不靠约定）。

读取策略（决策 2）：复用驱动的批量读（IEC104 总召 / Modbus 连续地
址合并 / ADS Sum 命令），一次 ``read()`` 调用覆盖全点表，不逐点单读。
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable

from wind_hub.adapter.inbound.cli.probe.diagnose import _create_driver
from wind_hub.adapter.inbound.cli.probe.verify_models import (
    DeviceVerifyResult,
    PointVerifyResult,
    VerifyResult,
)
from wind_hub.config.schema import Config, DeviceConfig, PointConfig
from wind_hub.domain.model.command import Command, CommandResult
from wind_hub.domain.model.errors import ConfigError, ProtocolError, WindHubError
from wind_hub.domain.model.point import PointRef, PointValue
from wind_hub.domain.port.outbound import HealthStatus, ProtocolPort


class _ReadOnlyDriver:
    """:class:`ProtocolPort` 的只读包装（决策 4）。

    ``connect`` / ``close`` / ``read`` / ``set_points_mapping`` /
    ``health`` 直接委托；``write`` / ``subscribe`` 抛
    :class:`ProtocolError`——verify 绝不触发写与订阅（决策 11）。
    """

    def __init__(self, inner: ProtocolPort) -> None:
        self._inner = inner

    def set_points_mapping(self, points: list[PointConfig]) -> None:
        self._inner.set_points_mapping(points)

    async def connect(self) -> None:
        await self._inner.connect()

    async def close(self) -> None:
        await self._inner.close()

    async def read(self, points: list[PointRef]) -> list[PointValue]:
        return await self._inner.read(points)

    async def write(self, cmds: list[Command]) -> list[CommandResult]:
        raise ProtocolError("probe verify 是只读操作，禁止 write")

    async def subscribe(
        self, points: list[PointRef], callback: Callable[[PointValue], Awaitable[None]]
    ) -> None:
        raise ProtocolError("probe verify 是只读操作，禁止 subscribe")

    def health(self) -> HealthStatus:
        return self._inner.health()


def _all_points_failed(
    device_cfg: DeviceConfig,
    points: list[PointConfig],
    connect_ok: bool,
    reason: str,
) -> DeviceVerifyResult:
    """连接/映射/整批读取失败：该设备所有点标记 fail（决策 10）。"""
    return DeviceVerifyResult(
        device_id=device_cfg.device_id,
        protocol=device_cfg.protocol,
        connect_ok=connect_ok,
        connect_error=None if connect_ok else reason,
        points=[PointVerifyResult(point_id=p.point_id, ok=False, error=reason) for p in points],
    )


async def verify_device(
    device_cfg: DeviceConfig,
    points: list[PointConfig],
    connect_timeout: float = 5.0,
    read_timeout: float = 10.0,
) -> DeviceVerifyResult:
    """验证单个设备的所有点（决策 3/10）。

    流程：建驱动 → 点表映射 → connect（带超时）→ 批量 read（带超
    时）→ 按序匹配返回值 → close。连接失败时所有点标记 fail，不影
    响其他设备（由调用方保证隔离，决策 10）。
    """
    try:
        driver = _ReadOnlyDriver(_create_driver(device_cfg))
    except ConfigError as exc:
        return _all_points_failed(device_cfg, points, False, f"无法创建协议驱动：{exc}")
    try:
        driver.set_points_mapping(points)
    except Exception as exc:  # 点表解析失败：所有点无法读，视为未联通
        return _all_points_failed(device_cfg, points, False, f"点表解析失败：{exc}")
    try:
        await asyncio.wait_for(driver.connect(), timeout=connect_timeout)
    except TimeoutError:
        with contextlib.suppress(Exception):
            await driver.close()
        return _all_points_failed(
            device_cfg, points, False, f"连接超时（{connect_timeout:g}s 内未完成）"
        )
    except Exception as exc:  # 各驱动异常类型不一，验证只关心成败
        with contextlib.suppress(Exception):
            await driver.close()
        return _all_points_failed(device_cfg, points, False, f"连接失败：{exc}")

    try:
        if not points:
            return DeviceVerifyResult(
                device_id=device_cfg.device_id,
                protocol=device_cfg.protocol,
                connect_ok=True,
                connect_error=None,
                points=[],
            )
        refs = [PointRef(device_id=device_cfg.device_id, point_id=p.point_id) for p in points]
        try:
            values = await asyncio.wait_for(driver.read(refs), timeout=read_timeout)
        except TimeoutError:
            return _all_points_failed(
                device_cfg, points, True, f"读取超时（{read_timeout:g}s 内未完成）"
            )
        except Exception as exc:  # 整批读失败：所有点标记 fail
            return _all_points_failed(device_cfg, points, True, f"读取失败：{exc}")
        results: list[PointVerifyResult] = []
        for idx, point in enumerate(points):
            if idx < len(values):
                value = values[idx]
                results.append(
                    PointVerifyResult(
                        point_id=point.point_id,
                        ok=True,
                        value=value.value,
                        quality=value.quality.value,
                    )
                )
            else:
                # 驱动返回数量不足：防御性兜底（ProtocolPort 约定同序同数）
                results.append(
                    PointVerifyResult(point_id=point.point_id, ok=False, error="驱动未返回该点的值")
                )
        return DeviceVerifyResult(
            device_id=device_cfg.device_id,
            protocol=device_cfg.protocol,
            connect_ok=True,
            connect_error=None,
            points=results,
        )
    finally:
        with contextlib.suppress(Exception):
            await driver.close()


async def verify_all(
    config: Config,
    device_id: str | None = None,
    concurrency: int = 1,
    connect_timeout: float = 5.0,
    read_timeout: float = 10.0,
) -> VerifyResult:
    """验证所有设备（或 ``--device`` 指定的单个设备，决策 1）。

    设备间默认串行（决策 9：诊断场景避免并发压力），``concurrency``
    经 Semaphore 限流。单设备失败不影响其他设备。

    Raises:
        WindHubError: 指定的 ``device_id`` 不在配置中。
    """
    started = time.perf_counter()
    devices = list(config.devices.devices)
    if device_id is not None:
        devices = [d for d in devices if d.device_id == device_id]
        if not devices:
            known = [d.device_id for d in config.devices.devices]
            raise WindHubError(f"设备 '{device_id}' 不存在（已配置：{known}）")

    points_by_device: dict[str, list[PointConfig]] = {}
    for point in config.points.points:
        points_by_device.setdefault(point.device_id, []).append(point)

    semaphore = asyncio.Semaphore(concurrency)

    async def run(device_cfg: DeviceConfig) -> DeviceVerifyResult:
        async with semaphore:
            return await verify_device(
                device_cfg,
                points_by_device.get(device_cfg.device_id, []),
                connect_timeout=connect_timeout,
                read_timeout=read_timeout,
            )

    device_results = await asyncio.gather(*(run(d) for d in devices))
    duration_ms = (time.perf_counter() - started) * 1000
    return VerifyResult(devices=list(device_results), duration_ms=duration_ms)
