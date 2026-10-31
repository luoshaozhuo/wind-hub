"""协议只读诊断 protocol.py 契约。

关键不变量：
- 只执行 connect/health/(read_one|read_many)，绝不写入/订阅/总召；
- 每次诊断使用独立 driver 实例，诊断结束必关闭，关闭失败不覆盖结果；
- 不健康、连接失败、读取超时、能力缺失、未知设备均转 FAIL 结果；
- benchmark 串行、只读、有上限。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.application.diagnostic import DiagnosticStatus
from core.application.protocol_contract import ConnectionHealth, ProtocolCapability, ProtocolSample
from core.domain.sample_value import Quality
from core.infrastructure.diagnostic import protocol


class FakeDriver:
    """记录全部调用的可控 ProtocolPort 双机；写/订阅类方法显式爆炸。"""

    def __init__(
        self,
        *,
        capabilities: frozenset[ProtocolCapability] = frozenset(
            {ProtocolCapability.READ, ProtocolCapability.READ_MANY}
        ),
        healthy: bool = True,
        health_message: str | None = None,
        connect_raises: Exception | None = None,
        read_value: float = 35000.0,
        delay: float = 0.0,
        close_raises: Exception | None = None,
    ) -> None:
        self._capabilities = capabilities
        self._healthy = healthy
        self._health_message = health_message
        self._connect_raises = connect_raises
        self._read_value = read_value
        self._delay = delay
        self._close_raises = close_raises
        self.calls: list[str] = []
        self.closed = False

    def capabilities(self) -> frozenset[ProtocolCapability]:
        return self._capabilities

    async def connect(self) -> None:
        self.calls.append("connect")
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._connect_raises:
            raise self._connect_raises

    async def health(self) -> ConnectionHealth:
        self.calls.append("health")
        return ConnectionHealth(healthy=self._healthy, message=self._health_message)

    async def read_one(self, point_id: str) -> ProtocolSample:
        self.calls.append(f"read_one:{point_id}")
        if self._delay:
            await asyncio.sleep(self._delay)
        return ProtocolSample(
            point_id=point_id,
            value=self._read_value,
            quality=Quality.GOOD,
            timestamp=datetime.now(UTC),
        )

    async def read_many(self, point_ids: list[str]):
        self.calls.append("read_many:" + ",".join(point_ids))
        return tuple(
            ProtocolSample(
                point_id=pid,
                value=self._read_value,
                quality=Quality.GOOD,
                timestamp=datetime.now(UTC),
            )
            for pid in point_ids
        )

    async def close(self) -> None:
        self.calls.append("close")
        self.closed = True
        if self._close_raises:
            raise self._close_raises

    # ---- 诊断路径严禁触碰的能力 ----
    async def write(self, *args, **kwargs):
        raise AssertionError("diagnostic must never write")

    async def write_many(self, *args, **kwargs):
        raise AssertionError("diagnostic must never write_many")

    async def subscribe(self, *args, **kwargs):
        raise AssertionError("diagnostic must never subscribe")

    async def interrogate(self, *args, **kwargs):
        raise AssertionError("diagnostic must never interrogate")


@pytest.fixture
def patched_driver(monkeypatch: pytest.MonkeyPatch):
    holder: dict[str, FakeDriver] = {}

    def factory(driver: FakeDriver) -> FakeDriver:
        def _fake_driver(config: str | Path, device_id: str) -> FakeDriver:
            holder["device"] = driver
            return driver

        monkeypatch.setattr(protocol, "_driver", _fake_driver)
        return driver

    return holder, factory


async def test_check_pass_connects_healthchecks_and_closes(patched_driver) -> None:
    holder, install = patched_driver
    driver = install(FakeDriver())

    result = await protocol.check_protocol("config.yaml", "dev-1")

    assert result.status is DiagnosticStatus.PASS
    assert result.findings[0].code == "PROTOCOL_OK"
    assert result.evidence == {"connected": True, "healthy": True}
    assert result.target == "dev-1"
    assert driver.calls == ["connect", "health", "close"]
    assert driver.closed


async def test_check_with_point_records_value_without_write(patched_driver) -> None:
    _, install = patched_driver
    driver = install(FakeDriver())

    result = await protocol.check_protocol("config.yaml", "dev-1", point_id="grid_freq")

    assert result.status is DiagnosticStatus.PASS
    point = result.evidence["point"]
    assert point["point_id"] == "grid_freq"
    assert point["value"] == 35000.0
    assert point["quality"] == "good"
    assert "T" in point["timestamp"]
    assert driver.calls == ["connect", "health", "read_one:grid_freq", "close"]


async def test_unhealthy_driver_is_fail_and_still_closed(patched_driver) -> None:
    _, install = patched_driver
    driver = install(FakeDriver(healthy=False, health_message="protocol stalled"))

    result = await protocol.check_protocol("config.yaml", "dev-1")

    assert result.status is DiagnosticStatus.FAIL
    assert "protocol stalled" in result.findings[0].message
    assert driver.closed


async def test_connect_failure_is_fail_and_still_closed(patched_driver) -> None:
    _, install = patched_driver
    driver = install(FakeDriver(connect_raises=ConnectionError("refused")))

    result = await protocol.check_protocol("config.yaml", "dev-1")

    assert result.status is DiagnosticStatus.FAIL
    assert "ConnectionError" in result.findings[0].message
    assert driver.closed


async def test_missing_read_capability_is_fail(patched_driver) -> None:
    _, install = patched_driver
    driver = install(FakeDriver(capabilities=frozenset()))

    result = await protocol.check_protocol("config.yaml", "dev-1", point_id="p")

    assert result.status is DiagnosticStatus.FAIL
    assert "read" in result.findings[0].message
    assert driver.closed


async def test_close_failure_does_not_override_pass(patched_driver) -> None:
    _, install = patched_driver
    install(FakeDriver(close_raises=OSError("fd gone")))

    result = await protocol.check_protocol("config.yaml", "dev-1")

    assert result.status is DiagnosticStatus.PASS


async def test_timeout_aborts_and_closes(patched_driver) -> None:
    _, install = patched_driver
    driver = install(FakeDriver(delay=1.0))

    result = await protocol.check_protocol("config.yaml", "dev-1", timeout=0.05)

    assert result.status is DiagnosticStatus.FAIL
    assert "TimeoutError" in result.findings[0].message
    assert driver.closed


async def test_unknown_device_from_driver_constructor_is_fail(monkeypatch) -> None:
    def boom(config: str | Path, device_id: str) -> FakeDriver:
        raise ValueError(f"unknown device: {device_id}")

    monkeypatch.setattr(protocol, "_driver", boom)

    result = await protocol.check_protocol("config.yaml", "ghost")

    assert result.status is DiagnosticStatus.FAIL
    assert "ghost" in result.findings[0].message


# --------------------------------------------------------------------------
# benchmark_protocol
# --------------------------------------------------------------------------


async def test_benchmark_single_point_uses_read_one(patched_driver) -> None:
    _, install = patched_driver
    driver = install(FakeDriver())

    result = await protocol.benchmark_protocol(
        "config.yaml", "dev-1", ["p1"], samples=4, interval=0, timeout=5
    )

    assert result.status is DiagnosticStatus.PASS
    assert result.measurements["successes"] == 4
    assert result.measurements["points"] == 4
    assert driver.calls.count("read_one:p1") == 4
    assert driver.closed


async def test_benchmark_multiple_points_requires_read_many(patched_driver) -> None:
    _, install = patched_driver
    driver = install(FakeDriver(capabilities=frozenset({ProtocolCapability.READ})))

    with pytest.raises(ValueError, match="read_many"):
        await protocol.benchmark_protocol("config.yaml", "dev-1", ["p1", "p2"])
    assert driver.closed


async def test_benchmark_single_point_requires_read(patched_driver) -> None:
    _, install = patched_driver
    driver = install(FakeDriver(capabilities=frozenset({ProtocolCapability.READ_MANY})))

    with pytest.raises(ValueError, match="read_one"):
        await protocol.benchmark_protocol("config.yaml", "dev-1", ["p1"])
    assert driver.closed


@pytest.mark.parametrize(
    "points",
    [(), ("a", "a"), tuple(str(i) for i in range(201))],
)
async def test_benchmark_point_list_bounds(patched_driver, points) -> None:
    _, install = patched_driver
    install(FakeDriver())

    with pytest.raises(ValueError, match="1..200 unique"):
        await protocol.benchmark_protocol("config.yaml", "dev-1", list(points))


async def test_benchmark_read_failure_yields_warning_not_retried(patched_driver) -> None:
    class FlakyDriver(FakeDriver):
        def __init__(self) -> None:
            super().__init__()
            self.n = 0

        async def read_one(self, point_id: str) -> ProtocolSample:
            self.calls.append("read_one")
            self.n += 1
            if self.n == 2:
                raise TimeoutError("slow device")
            return await super().read_one(point_id)

    _, install = patched_driver
    driver = install(FlakyDriver())

    result = await protocol.benchmark_protocol("config.yaml", "dev-1", ["p"], samples=3, interval=0)

    assert result.status.value == "WARNING"
    assert result.measurements["failures"] == 1
    assert driver.calls.count("read_one") == 3


# --------------------------------------------------------------------------
# _driver：真实配置驱动的协议选择（不发起任何连接）
# --------------------------------------------------------------------------


def test_driver_selects_modbus_from_shared_config() -> None:
    config = Path(__file__).resolve().parents[3] / "configs"
    driver = protocol._driver(config, "WT01")

    from core.infrastructure.protocol.modbus.driver import ModbusDriver

    assert isinstance(driver, ModbusDriver)


def test_driver_unknown_device_raises() -> None:
    config = Path(__file__).resolve().parents[3] / "configs"
    with pytest.raises(ValueError, match="unknown device"):
        protocol._driver(config, "no-such-device")


@pytest.mark.parametrize(
    ("protocol_name", "module", "driver_name"),
    [
        ("ads", "core.infrastructure.protocol.ads.driver", "ADSDriver"),
        ("modbus", "core.infrastructure.protocol.modbus.driver", "ModbusDriver"),
        ("iec104", "core.infrastructure.protocol.iec104.driver", "IEC104Driver"),
    ],
)
def test_driver_dispatches_by_protocol(
    monkeypatch: pytest.MonkeyPatch,
    protocol_name: str,
    module: str,
    driver_name: str,
) -> None:
    import importlib

    marker = object()
    table = SimpleNamespace(protocol=SimpleNamespace(name=protocol_name))
    snapshot = SimpleNamespace(
        site=SimpleNamespace(devices={"d": SimpleNamespace(device_model_id="m")}),
        device_models={"m": SimpleNamespace(point_table_id="t")},
        point_tables={"t": table},
    )
    monkeypatch.setattr(
        protocol,
        "SharedDocumentLoader",
        lambda paths: SimpleNamespace(load=lambda: object()),
    )
    monkeypatch.setattr(protocol, "build_shared_snapshot", lambda documents: snapshot)
    monkeypatch.setattr(
        protocol,
        "resolve_effective_device",
        lambda device, model, name: (SimpleNamespace(endpoint="endpoint"), {}),
    )
    imported = importlib.import_module(module)
    monkeypatch.setattr(imported, driver_name, lambda *args: marker)

    assert protocol._driver("ignored", "d") is marker


def test_driver_unsupported_protocol_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    table = SimpleNamespace(protocol=SimpleNamespace(name="s7"))
    snapshot = SimpleNamespace(
        site=SimpleNamespace(devices={"d": SimpleNamespace(device_model_id="m")}),
        device_models={"m": SimpleNamespace(point_table_id="t")},
        point_tables={"t": table},
    )
    monkeypatch.setattr(
        protocol,
        "SharedDocumentLoader",
        lambda paths: SimpleNamespace(load=lambda: object()),
    )
    monkeypatch.setattr(protocol, "build_shared_snapshot", lambda documents: snapshot)
    monkeypatch.setattr(
        protocol,
        "resolve_effective_device",
        lambda device, model, name: (SimpleNamespace(endpoint="endpoint"), {}),
    )
    with pytest.raises(ValueError, match="unsupported protocol"):
        protocol._driver("ignored", "d")


async def test_benchmark_multiple_points_uses_read_many(patched_driver) -> None:
    _, install = patched_driver
    driver = install(FakeDriver())

    result = await protocol.benchmark_protocol(
        "config.yaml", "dev-1", ["p1", "p2"], samples=3, interval=0
    )

    assert result.status is DiagnosticStatus.PASS
    assert result.measurements["points"] == 6
    assert driver.calls.count("read_many:p1,p2") == 3
