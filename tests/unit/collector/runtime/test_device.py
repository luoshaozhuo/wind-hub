"""运行时 CollectorDeviceSession（``application/runtime/device.py``）的单元测试。

覆盖点（对应重构简报 spec §34）：

- point_group 选点（``point_group_points`` / ``point_refs``）与
  read / read_points / write 向协议实例的委托；
- ``start_acquisition`` 的 POLL 分支：参数校验（interval 必填、
  ADS sequential 不可周期采集、缺 acquire 报错）；
- ``start_acquisition`` 的 SUBSCRIBE 分支：订阅注册、单点回调包装为
  批次转发、``InterrogationCapable`` 协议订阅后自动触发一次总召；
- ``PollingAcquisitionHandle`` 的 fixed-rate 语义：首轮立即执行、
  串行不重入、overrun 时至多一次 catch-up 并跳过其余槽位、统计上报、
  close 干净停止。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import pytest

from wind_hub_collector.application.runtime.device import (
    CollectorDeviceSession,
    PollingAcquisitionHandle,
)
from wind_hub_core.config import DeviceConfig, PointAddress, PointConfig
from wind_hub_core.model.command import Command, CommandResult
from wind_hub_core.model.device import Endpoint
from wind_hub_core.model.errors import ConfigError
from wind_hub_core.model.health import HealthStatus
from wind_hub_core.model.point import PointRef, PointValue, Quality
from wind_hub_core.protocol.port import AcquisitionMode, SubscriptionHandle

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeSubscription:
    """SubscriptionHandle 替身——记录 close 调用。"""

    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class _FakeProtocol:
    """ProtocolPort 的结构化替身——记录读写/订阅委托，可切换采集能力。"""

    def __init__(
        self,
        mode: AcquisitionMode = AcquisitionMode.POLL,
        read_values: list[PointValue] | None = None,
    ) -> None:
        self._mode = mode
        self._read_values = read_values if read_values is not None else []
        self.read_calls: list[list[PointRef]] = []
        self.write_calls: list[list[Command]] = []
        self.subscribe_calls: list[tuple[list[PointRef], float | None]] = []
        self.set_mapping_calls: list[list[PointConfig]] = []
        self.subscription = _FakeSubscription()
        self._callback: Callable[[PointValue], Awaitable[None]] | None = None

    @property
    def acquisition_mode(self) -> AcquisitionMode:
        return self._mode

    def set_points_mapping(self, points: list[PointConfig]) -> None:
        self.set_mapping_calls.append(points)

    async def connect(self) -> None:
        pass

    async def close(self) -> None:
        pass

    def health(self) -> HealthStatus:
        return HealthStatus(healthy=True)

    async def read(self, refs: list[PointRef]) -> list[PointValue]:
        self.read_calls.append(refs)
        return list(self._read_values)

    async def write(self, cmds: list[Command]) -> list[CommandResult]:
        self.write_calls.append(cmds)
        return [CommandResult(command_id=c.command_id, success=True) for c in cmds]

    async def subscribe(
        self,
        points: list[PointRef],
        callback: Callable[[PointValue], Awaitable[None]],
        *,
        interval: float | None = None,
    ) -> SubscriptionHandle:
        self.subscribe_calls.append((points, interval))
        self._callback = callback
        return self.subscription

    async def push(self, pv: PointValue) -> None:
        """模拟协议侧数据到达。"""
        assert self._callback is not None
        await self._callback(pv)


class _InterrogatingProtocol(_FakeProtocol):
    """实现 InterrogationCapable 的协议替身（IEC104 语义）。"""

    def __init__(self) -> None:
        super().__init__(mode=AcquisitionMode.SUBSCRIBE)
        self.interrogate_calls = 0

    async def interrogate(self) -> None:
        self.interrogate_calls += 1


def _device_config(
    device_id: str = "d1",
    protocol: str = "modbus",
    read_mode: str = "sum",
) -> DeviceConfig:
    return DeviceConfig(
        device_id=device_id,
        protocol=protocol,
        endpoint=Endpoint(host="10.0.0.1", port=502),
        point_table="t1",
        read_mode=read_mode,
    )


def _point(point_id: str, groups: tuple[str, ...] = ("default",)) -> PointConfig:
    return PointConfig(
        point_id=point_id,
        point_groups=list(groups),
        address=PointAddress(type="holding_register"),
    )


def _make_device(
    proto: _FakeProtocol,
    points: list[PointConfig] | None = None,
    config: DeviceConfig | None = None,
) -> CollectorDeviceSession:
    return CollectorDeviceSession(
        config if config is not None else _device_config(),
        points if points is not None else [_point("p1"), _point("p2")],
        proto,  # type: ignore[arg-type]  # 结构化替身
    )


def _value(point_id: str = "p1") -> PointValue:
    return PointValue(device_id="d1", point_id=point_id, value=1.0, quality=Quality.GOOD)


# ---------------------------------------------------------------------------
# 选点与读写委托
# ---------------------------------------------------------------------------


class TestPointSelection:
    def test_point_group_points_selects_by_membership(self) -> None:
        device = _make_device(
            _FakeProtocol(),
            [_point("p1", ("fast",)), _point("p2", ("slow",)), _point("p3", ("fast", "slow"))],
        )
        assert [p.point_id for p in device.point_group_points("fast")] == ["p1", "p3"]
        assert [p.point_id for p in device.point_group_points("slow")] == ["p2", "p3"]
        assert device.point_group_points("none") == []

    def test_point_refs_carry_device_id(self) -> None:
        device = _make_device(_FakeProtocol(), [_point("p1", ("g",)), _point("p2", ("g",))])
        refs = device.point_refs("g")
        assert [r.point_id for r in refs] == ["p1", "p2"]
        assert all(r.device_id == "d1" for r in refs)

    def test_points_snapshot_is_immutable(self) -> None:
        device = _make_device(_FakeProtocol(), [_point("p1", ("g",))])

        assert device.points == (_point("p1", ("g",)),)
        assert isinstance(device.points, tuple)

    def test_set_points_updates_table_and_reinjects_mapping(self) -> None:
        proto = _FakeProtocol()
        device = _make_device(proto)
        new_points = [_point("p9", ("g",))]
        device.set_points(new_points)
        assert device.points == tuple(new_points)
        # 构造期已注入初始映射；set_points 必须再次注入新映射。
        assert proto.set_mapping_calls[-1] == new_points
        assert len(proto.set_mapping_calls) == 2


class TestReadWriteDelegation:
    async def test_read_delegates_group_refs_to_protocol(self) -> None:
        proto = _FakeProtocol(read_values=[_value()])
        device = _make_device(proto, [_point("p1", ("g",)), _point("p2", ("other",))])

        values = await device.read("g")

        assert proto.read_calls == [[PointRef(device_id="d1", point_id="p1")]]
        assert len(values) == 1

    async def test_read_points_passes_explicit_refs(self) -> None:
        proto = _FakeProtocol(read_values=[_value("p2")])
        device = _make_device(proto)
        refs = [PointRef(device_id="d1", point_id="p2")]

        values = await device.read_points(refs)

        assert proto.read_calls == [refs]
        assert values[0].point_id == "p2"

    async def test_write_delegates_to_protocol(self) -> None:
        proto = _FakeProtocol()
        device = _make_device(proto)
        cmd = Command(command_id="c1", device_id="d1", point_id="p1", value=5.0)

        results = await device.write([cmd])

        assert proto.write_calls == [[cmd]]
        assert results[0].success is True


# ---------------------------------------------------------------------------
# 点值解释（scale/offset —— 轮询与订阅同语义）
# ---------------------------------------------------------------------------


class TestNormalizeValues:
    async def test_read_applies_scale_and_offset(self) -> None:
        """read 返回工程值：value = raw * scale + offset。"""
        proto = _FakeProtocol(read_values=[_value("p1")])  # raw 1.0
        device = _make_device(
            proto,
            [
                PointConfig(
                    point_id="p1",
                    point_groups=["g"],
                    address=PointAddress(type="holding_register"),
                    scale=2.0,
                    offset=10.0,
                )
            ],
        )

        values = await device.read("g")

        assert values[0].value == 12.0

    async def test_read_points_applies_scale_and_offset(self) -> None:
        proto = _FakeProtocol(read_values=[_value("p1")])
        device = _make_device(
            proto,
            [
                PointConfig(
                    point_id="p1",
                    point_groups=["g"],
                    address=PointAddress(type="holding_register"),
                    scale=0.5,
                    offset=-1.0,
                )
            ],
        )

        values = await device.read_points([PointRef(device_id="d1", point_id="p1")])

        assert values[0].value == -0.5

    async def test_identity_transform_returns_same_object(self) -> None:
        """scale=1/offset=0（默认）——原样透传，不复制。"""
        pushed = _value("p1")
        proto = _FakeProtocol(read_values=[pushed])
        device = _make_device(proto, [_point("p1", ("g",))])

        values = await device.read("g")

        assert values[0] is pushed

    async def test_non_numeric_values_not_scaled(self) -> None:
        """None / str / bool 原样透传（即使配置了 scale/offset）。"""
        proto = _FakeProtocol(
            read_values=[
                PointValue(device_id="d1", point_id="p1", value=None, quality=Quality.BAD),
                PointValue(device_id="d1", point_id="p2", value="OPEN"),
                PointValue(device_id="d1", point_id="p3", value=True),
            ]
        )
        scaled = PointConfig(
            point_id="p1",
            point_groups=["g"],
            address=PointAddress(type="holding_register"),
            scale=2.0,
            offset=10.0,
        )
        device = _make_device(
            proto,
            [
                scaled,
                PointConfig(
                    point_id="p2",
                    point_groups=["g"],
                    address=PointAddress(type="holding_register"),
                    data_type="str",
                    scale=2.0,
                ),
                PointConfig(
                    point_id="p3",
                    point_groups=["g"],
                    address=PointAddress(type="holding_register"),
                    data_type="bool",
                    scale=2.0,
                ),
            ],
        )

        values = await device.read("g")

        assert [v.value for v in values] == [None, "OPEN", True]
        assert values[0].quality is Quality.BAD

    async def test_metadata_preserved_after_scaling(self) -> None:
        """quality / timestamp / source 不因换算改变。"""
        src = PointValue(
            device_id="d1",
            point_id="p1",
            value=2.0,
            quality=Quality.UNCERTAIN,
            source="test",
        )
        proto = _FakeProtocol(read_values=[src])
        device = _make_device(
            proto,
            [
                PointConfig(
                    point_id="p1",
                    point_groups=["g"],
                    address=PointAddress(type="holding_register"),
                    scale=3.0,
                    offset=1.0,
                )
            ],
        )

        values = await device.read("g")

        assert values[0].value == 7.0
        assert values[0].quality is Quality.UNCERTAIN
        assert values[0].timestamp == src.timestamp
        assert values[0].source == "test"
        assert values[0].device_id == "d1"
        assert values[0].point_id == "p1"

    async def test_unknown_point_passthrough(self) -> None:
        """点表查不到的点（防御性分支）原样透传。"""
        proto = _FakeProtocol(read_values=[_value("ghost")])
        device = _make_device(proto, [_point("p1", ("g",))])

        values = await device.read_points([PointRef(device_id="d1", point_id="ghost")])

        assert values[0].value == 1.0

    async def test_subscribe_normalizes_same_as_read(self) -> None:
        """订阅回调同样经 CollectorDeviceSession normalize——与轮询同语义。"""
        proto = _FakeProtocol(mode=AcquisitionMode.SUBSCRIBE)
        device = _make_device(
            proto,
            [
                PointConfig(
                    point_id="p1",
                    point_groups=["g"],
                    address=PointAddress(type="holding_register"),
                    scale=2.0,
                    offset=10.0,
                )
            ],
        )
        batches: list[list[PointValue]] = []

        async def on_data(values: list[PointValue]) -> None:
            batches.append(values)

        handle = await device.start_acquisition(point_group="g", interval=0.5, on_data=on_data)

        pushed = _value("p1")  # raw 1.0
        await proto.push(pushed)
        assert batches[0][0].value == 12.0
        assert batches[0][0].quality is pushed.quality
        assert batches[0][0].timestamp == pushed.timestamp

        await handle.close()

    async def test_set_points_applies_new_scale_offset(self) -> None:
        """热重载 set_points 后按新点表换算——新 scale/offset 立即生效。"""
        proto = _FakeProtocol(read_values=[_value("p1")])
        device = _make_device(proto, [_point("p1", ("g",))])  # 默认 scale=1/offset=0

        assert (await device.read("g"))[0].value == 1.0

        device.set_points(
            [
                PointConfig(
                    point_id="p1",
                    point_groups=["g"],
                    address=PointAddress(type="holding_register"),
                    scale=10.0,
                )
            ]
        )
        assert (await device.read("g"))[0].value == 10.0


# ---------------------------------------------------------------------------
# start_acquisition —— POLL 分支
# ---------------------------------------------------------------------------


class TestPollAcquisition:
    async def test_poll_requires_interval(self) -> None:
        device = _make_device(_FakeProtocol())

        async def acquire() -> None:
            pass

        with pytest.raises(ConfigError, match="interval"):
            await device.start_acquisition(point_group="default", interval=None, acquire=acquire)
        with pytest.raises(ConfigError, match="interval"):
            await device.start_acquisition(point_group="default", interval=0.0, acquire=acquire)

    async def test_poll_requires_acquire_callback(self) -> None:
        device = _make_device(_FakeProtocol())
        with pytest.raises(ConfigError, match="acquire"):
            await device.start_acquisition(point_group="default", interval=1.0)

    async def test_ads_sequential_device_rejects_scheduled_collection(self) -> None:
        device = _make_device(
            _FakeProtocol(),
            config=_device_config(protocol="ads", read_mode="sequential"),
        )

        async def acquire() -> None:
            pass

        with pytest.raises(ConfigError, match="scheduled collection"):
            await device.start_acquisition(point_group="default", interval=1.0, acquire=acquire)

    async def test_poll_handle_drives_acquire_and_closes(self) -> None:
        device = _make_device(_FakeProtocol())
        calls = 0

        async def acquire() -> None:
            nonlocal calls
            calls += 1

        handle = await device.start_acquisition(
            point_group="default", interval=0.02, acquire=acquire
        )
        await asyncio.sleep(0.07)
        await handle.close()
        stopped_at = calls
        await asyncio.sleep(0.05)

        assert calls >= 2  # 首轮立即 + 后续周期
        assert calls == stopped_at  # close 后不再触发


# ---------------------------------------------------------------------------
# start_acquisition —— SUBSCRIBE 分支
# ---------------------------------------------------------------------------


class TestSubscribeAcquisition:
    async def test_subscribe_registers_refs_and_forwards_batches(self) -> None:
        proto = _FakeProtocol(mode=AcquisitionMode.SUBSCRIBE)
        device = _make_device(proto, [_point("p1", ("g",)), _point("p2", ("other",))])
        batches: list[list[PointValue]] = []

        async def on_data(values: list[PointValue]) -> None:
            batches.append(values)

        handle = await device.start_acquisition(point_group="g", interval=0.5, on_data=on_data)

        # 只订阅 point_group 选中的点；interval 透传给协议
        refs, interval = proto.subscribe_calls[0]
        assert [r.point_id for r in refs] == ["p1"]
        assert interval == 0.5

        # 协议单点回调 → 包装为批次交给 on_data
        pushed = _value("p1")
        await proto.push(pushed)
        assert len(batches) == 1
        assert batches[0] == [pushed]

        await handle.close()
        assert proto.subscription.closed is True

    async def test_subscribe_requires_on_data(self) -> None:
        device = _make_device(_FakeProtocol(mode=AcquisitionMode.SUBSCRIBE))
        with pytest.raises(ConfigError, match="on_data"):
            await device.start_acquisition(point_group="default", interval=None)

    async def test_interrogation_capable_triggers_gi_after_subscribe(self) -> None:
        """协议实现 InterrogationCapable（IEC104）时：订阅建立后自动一次总召。"""
        proto = _InterrogatingProtocol()
        device = _make_device(proto)
        batches: list[list[PointValue]] = []

        async def on_data(values: list[PointValue]) -> None:
            batches.append(values)

        handle = await device.start_acquisition(
            point_group="default", interval=None, on_data=on_data
        )

        assert proto.interrogate_calls == 1
        assert proto.subscribe_calls  # 订阅先于总召建立
        await handle.close()

    async def test_non_interrogating_subscribe_skips_gi(self) -> None:
        """普通订阅协议（ADS notification）不触发总召。"""
        proto = _FakeProtocol(mode=AcquisitionMode.SUBSCRIBE)
        device = _make_device(proto)

        async def on_data(values: list[PointValue]) -> None:
            pass

        handle = await device.start_acquisition(
            point_group="default", interval=0.1, on_data=on_data
        )
        assert not isinstance(proto, _InterrogatingProtocol)
        await handle.close()


# ---------------------------------------------------------------------------
# PollingAcquisitionHandle —— fixed-rate 语义
# ---------------------------------------------------------------------------


class TestPollingHandle:
    async def test_first_run_immediate_then_fixed_rate(self) -> None:
        """首轮立即执行；快速 acquire 下按 interval 节拍推进（不随执行耗时长漂）。"""
        starts: list[float] = []
        loop = asyncio.get_running_loop()

        async def acquire() -> None:
            starts.append(loop.time())

        handle = PollingAcquisitionHandle(0.03, acquire)
        t0 = loop.time()
        await handle.start()
        await asyncio.sleep(0.115)
        await handle.close()

        assert 3 <= len(starts) <= 5
        # 首轮几乎立即启动
        assert starts[0] - t0 < 0.02
        # 相邻间隔 ≈ interval（固定节拍，不是 collect→sleep 的 fixed-delay）
        gaps = [b - a for a, b in zip(starts, starts[1:], strict=False)]
        assert all(abs(g - 0.03) < 0.02 for g in gaps)

    async def test_no_reentry_and_single_catchup(self) -> None:
        """acquire 耗时超过 interval：串行不重入；overrun 时至多一次立即
        catch-up，其余错过槽位跳过（不爆发补采）。"""
        concurrency = 0
        max_concurrency = 0
        calls = 0
        stats: list[tuple[float, bool, int]] = []

        async def acquire() -> None:
            nonlocal concurrency, max_concurrency, calls
            concurrency += 1
            max_concurrency = max(max_concurrency, concurrency)
            calls += 1
            await asyncio.sleep(0.045)  # 4.5 个 interval
            concurrency -= 1

        handle = PollingAcquisitionHandle(
            0.01, acquire, on_stats=lambda j, o, m: stats.append((j, o, m))
        )
        await handle.start()
        await asyncio.sleep(0.16)
        await handle.close()

        assert max_concurrency == 1  # 绝不重入
        # 无补采爆发：每次 acquire 占 4.5 槽位，窗口内调用数远小于 16
        assert calls <= 5
        # overrun 被如实上报，且跳过的完整周期数 > 0
        assert any(overrun for _, overrun, _ in stats)
        assert any(missed >= 1 for _, _, missed in stats)

    async def test_stats_report_jitter(self) -> None:
        """正常节拍：jitter ≈ 0、无 overrun、无 missed。"""
        stats: list[tuple[float, bool, int]] = []

        async def acquire() -> None:
            pass

        handle = PollingAcquisitionHandle(
            0.02, acquire, on_stats=lambda j, o, m: stats.append((j, o, m))
        )
        await handle.start()
        await asyncio.sleep(0.09)
        await handle.close()

        assert len(stats) >= 3
        assert all(abs(jitter) < 0.02 for jitter, _, _ in stats)
        assert all(not overrun for _, overrun, _ in stats)
        assert all(missed == 0 for _, _, missed in stats)

    async def test_acquire_failure_continues_loop(self) -> None:
        """单次 acquire 抛异常不终止轮询。"""
        calls = 0

        async def acquire() -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("boom")

        handle = PollingAcquisitionHandle(0.02, acquire)
        await handle.start()
        await asyncio.sleep(0.09)
        await handle.close()

        assert calls >= 3

    async def test_close_idempotent_and_no_orphan(self) -> None:
        """close 幂等；重复 start 不产生第二个协程。"""
        calls = 0

        async def acquire() -> None:
            nonlocal calls
            calls += 1

        handle = PollingAcquisitionHandle(0.02, acquire)
        await handle.start()
        await handle.start()  # 幂等
        await asyncio.sleep(0.05)
        await handle.close()
        await handle.close()  # 幂等
        stopped_at = calls
        await asyncio.sleep(0.05)
        assert calls == stopped_at
