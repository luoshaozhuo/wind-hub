"""Unit tests for ``cli/probe/verify.py`` — 点表只读验证。

驱动全部 monkeypatch 替换，不触网；重点覆盖只读保证（决策 4/11）、
连接失败整设备标记（决策 10）与质量 BAD 区分（决策 5）。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from wind_hub.adapter.inbound.cli.probe import verify as verify_mod
from wind_hub.adapter.inbound.cli.probe.verify import _ReadOnlyDriver
from wind_hub.config.schema import (
    Config,
    DeviceConfig,
    DevicesConfig,
    PointConfig,
    ResolvedPointTable,
    ResolvedPointTables,
    RoutingConfig,
    SystemConfig,
)
from wind_hub.domain.model.device import Endpoint
from wind_hub.domain.model.errors import ProtocolError, WindHubError
from wind_hub.domain.model.point import PointValue, Quality


def _device(device_id: str = "wtg-001", protocol: str = "modbus") -> DeviceConfig:
    return DeviceConfig(
        device_id=device_id,
        protocol=protocol,
        point_table="t1",
        endpoint=Endpoint(host="10.0.1.1", port=502),
    )


def _point(point_id: str) -> PointConfig:
    return PointConfig(
        point_id=point_id,
        address={"type": "holding_register", "address": 0},
        data_type="int16",
    )


def _config(devices: list[DeviceConfig], points: list[PointConfig]) -> Config:
    """全部设备共享表 ``t1``——点表设备无关、经绑定复用。"""
    return Config(
        system=SystemConfig(),
        devices=DevicesConfig(devices=devices),
        point_tables=ResolvedPointTables(tables={"t1": ResolvedPointTable(points=points)}),
        routing=RoutingConfig(rules=[]),
    )


class _FakeDriver:
    """可注入 connect/read 行为、并记录 write/subscribe 是否被调用的假驱动。"""

    def __init__(
        self,
        values: list[PointValue] | None = None,
        connect_exc: Exception | None = None,
        read_exc: Exception | None = None,
        hang_connect: bool = False,
    ) -> None:
        self._values = values
        self._connect_exc = connect_exc
        self._read_exc = read_exc
        self._hang_connect = hang_connect
        self.read_called = False
        self.write_called = False
        self.subscribe_called = False
        self.closed = False

    def set_points_mapping(self, points: list[PointConfig]) -> None:
        pass

    async def connect(self) -> None:
        if self._hang_connect:
            await asyncio.sleep(10)
        if self._connect_exc is not None:
            raise self._connect_exc

    async def read(self, points: list[Any]) -> list[PointValue]:
        self.read_called = True
        if self._read_exc is not None:
            raise self._read_exc
        assert self._values is not None
        return self._values

    async def write(self, cmds: list[Any]) -> list[Any]:
        self.write_called = True
        return []

    async def subscribe(self, points: list[Any], callback: Any) -> None:
        self.subscribe_called = True

    async def close(self) -> None:
        self.closed = True


def _patch_driver(monkeypatch: pytest.MonkeyPatch, driver: _FakeDriver) -> None:
    monkeypatch.setattr(verify_mod, "_create_driver", lambda device_cfg: driver)


def _values(device_id: str, *point_ids: str, quality: Quality = Quality.GOOD) -> list[PointValue]:
    return [
        PointValue(device_id=device_id, point_id=pid, value=1.0, quality=quality)
        for pid in point_ids
    ]


# ---------------------------------------------------------------------------
# verify_device
# ---------------------------------------------------------------------------


async def test_verify_device_all_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    driver = _FakeDriver(values=_values("wtg-001", "p1", "p2"))
    _patch_driver(monkeypatch, driver)
    result = await verify_mod.verify_device(_device(), [_point("p1"), _point("p2")])

    assert result.connect_ok is True
    assert result.connect_error is None
    assert result.total == 2
    assert result.ok_count == 2
    assert result.fail_count == 0
    assert result.bad_quality_count == 0
    assert [p.point_id for p in result.points] == ["p1", "p2"]
    assert driver.closed  # 验证后释放连接


async def test_verify_device_connect_failure_marks_all_points(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """决策 10：连接失败 → 该设备所有点 fail，且不再发起 read。"""
    driver = _FakeDriver(connect_exc=ProtocolError("connection refused"))
    _patch_driver(monkeypatch, driver)
    result = await verify_mod.verify_device(_device(), [_point("p1"), _point("p2")])

    assert result.connect_ok is False
    assert "connection refused" in (result.connect_error or "")
    assert result.fail_count == 2
    assert all("connection refused" in (p.error or "") for p in result.points)
    assert driver.read_called is False
    assert driver.closed


async def test_verify_device_connect_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """决策 3：connect 包外层超时（与 diagnose 一致的 5s 语义，测试用小值）。"""
    driver = _FakeDriver(hang_connect=True)
    _patch_driver(monkeypatch, driver)
    result = await verify_mod.verify_device(_device(), [_point("p1")], connect_timeout=0.05)

    assert result.connect_ok is False
    assert "连接超时" in (result.connect_error or "")
    assert result.fail_count == 1


async def test_verify_device_read_failure_marks_all_points(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """整批读失败（驱动抛错）→ 所有点 fail，但 connect_ok 仍为 True。"""
    driver = _FakeDriver(read_exc=ProtocolError("gateway path unavailable"))
    _patch_driver(monkeypatch, driver)
    result = await verify_mod.verify_device(_device(), [_point("p1"), _point("p2")])

    assert result.connect_ok is True
    assert result.fail_count == 2
    assert all("读取失败" in (p.error or "") for p in result.points)


async def test_verify_device_partial_missing_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """驱动返回数量不足（防御分支）→ 缺失的点 fail，其余正常。"""
    driver = _FakeDriver(values=_values("wtg-001", "p1"))
    _patch_driver(monkeypatch, driver)
    result = await verify_mod.verify_device(_device(), [_point("p1"), _point("p2")])

    assert result.ok_count == 1
    assert result.fail_count == 1
    assert result.points[1].ok is False
    assert "未返回" in (result.points[1].error or "")


async def test_verify_device_bad_quality_counted_separately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """决策 5：质量 BAD 是「能返回但不可信」，计入 ok 也计入 bad_quality。"""
    values = _values("wtg-001", "p1") + _values("wtg-001", "p2", quality=Quality.BAD)
    driver = _FakeDriver(values=values)
    _patch_driver(monkeypatch, driver)
    result = await verify_mod.verify_device(_device(), [_point("p1"), _point("p2")])

    assert result.ok_count == 2
    assert result.fail_count == 0
    assert result.bad_quality_count == 1
    bad = [p for p in result.points if p.quality_bad]
    assert [p.point_id for p in bad] == ["p2"]


async def test_verify_device_read_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """决策 3：批量读包 read_timeout。"""

    class _SlowDriver(_FakeDriver):
        async def read(self, points: list[Any]) -> list[PointValue]:
            self.read_called = True
            await asyncio.sleep(10)
            return []

    driver = _SlowDriver()
    _patch_driver(monkeypatch, driver)
    result = await verify_mod.verify_device(_device(), [_point("p1")], read_timeout=0.05)

    assert result.connect_ok is True
    assert result.fail_count == 1
    assert "读取超时" in (result.points[0].error or "")


# ---------------------------------------------------------------------------
# 只读保证（决策 4/11）
# ---------------------------------------------------------------------------


async def test_readonly_wrapper_blocks_write_and_subscribe() -> None:
    """_ReadOnlyDriver 结构性拦截 write / subscribe。"""
    driver = _ReadOnlyDriver(_FakeDriver())
    with pytest.raises(ProtocolError, match="禁止 write"):
        await driver.write([])
    with pytest.raises(ProtocolError, match="禁止 subscribe"):
        await driver.subscribe([], lambda v: None)


async def test_verify_device_never_calls_write_or_subscribe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """决策 4/11：完整验证流程不触发 write / subscribe。"""
    driver = _FakeDriver(values=_values("wtg-001", "p1"))
    _patch_driver(monkeypatch, driver)
    await verify_mod.verify_device(_device(), [_point("p1")])

    assert driver.write_called is False
    assert driver.subscribe_called is False


# ---------------------------------------------------------------------------
# verify_all
# ---------------------------------------------------------------------------


async def test_verify_all_multiple_devices(monkeypatch: pytest.MonkeyPatch) -> None:
    drivers = {
        "wtg-001": _FakeDriver(values=_values("wtg-001", "p1")),
        "wtg-002": _FakeDriver(connect_exc=ProtocolError("down")),
    }
    monkeypatch.setattr(
        verify_mod, "_create_driver", lambda device_cfg: drivers[device_cfg.device_id]
    )
    cfg = _config(
        [_device("wtg-001"), _device("wtg-002")],
        [_point("p1")],
    )
    result = await verify_mod.verify_all(cfg)

    assert [d.device_id for d in result.devices] == ["wtg-001", "wtg-002"]
    assert result.devices[0].connect_ok is True
    assert result.devices[1].connect_ok is False  # 单设备失败不影响其他设备
    assert result.total_points == 2
    assert result.total_ok == 1
    assert result.total_fail == 1
    assert result.duration_ms >= 0


async def test_verify_all_specific_device(monkeypatch: pytest.MonkeyPatch) -> None:
    drivers = {"wtg-002": _FakeDriver(values=_values("wtg-002", "p2"))}
    monkeypatch.setattr(
        verify_mod, "_create_driver", lambda device_cfg: drivers[device_cfg.device_id]
    )
    cfg = _config(
        [_device("wtg-001"), _device("wtg-002")],
        [_point("p2")],
    )
    result = await verify_mod.verify_all(cfg, device_id="wtg-002")

    assert len(result.devices) == 1
    assert result.devices[0].device_id == "wtg-002"
    assert result.total_ok == 1


async def test_verify_all_unknown_device_raises() -> None:
    cfg = _config([_device("wtg-001")], [_point("p1")])
    with pytest.raises(WindHubError, match="不存在"):
        await verify_mod.verify_all(cfg, device_id="no-such")


async def test_verify_all_concurrency_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """决策 9：concurrency 经 Semaphore 限流，峰值在飞验证不超过上限。"""
    in_flight = 0
    peak = 0

    class _TrackingDriver(_FakeDriver):
        async def read(self, points: list[Any]) -> list[PointValue]:
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.02)
            in_flight -= 1
            assert self._values is not None
            return self._values

    monkeypatch.setattr(
        verify_mod,
        "_create_driver",
        lambda device_cfg: _TrackingDriver(values=_values(device_cfg.device_id, "p1")),
    )
    device_ids = [f"wtg-{i:03d}" for i in range(5)]
    cfg = _config(
        [_device(did) for did in device_ids],
        [_point("p1")],
    )
    result = await verify_mod.verify_all(cfg, concurrency=2)

    assert result.total_ok == 5  # 并发路径确实执行了 read（非空转）
    assert peak <= 2
