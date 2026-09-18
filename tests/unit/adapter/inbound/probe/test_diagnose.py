"""Unit tests for ``cli/probe/diagnose.py`` — 分层诊断编排。

各层（本机信息 / ARP / ICMP / TCP / 协议驱动）全部 monkeypatch 替换，
不触网；重点覆盖短路语义与结论汇聚（决策 2/6/7）。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from wind_hub.adapter.inbound.cli.probe import diagnose as diag
from wind_hub.adapter.inbound.cli.probe.diagnose_models import (
    DiagnoseStep,
    LocalInfo,
    StepStatus,
)
from wind_hub.adapter.inbound.cli.probe.ports_models import PortResult, PortState
from wind_hub.config.schema import DeviceConfig, PointConfig
from wind_hub.domain.model.device import Endpoint
from wind_hub.domain.model.errors import ConfigError, ProtocolError
from wind_hub.domain.model.point import PointValue

_LOCAL = LocalInfo(
    hostname="test-host",
    ips=[("10.0.1.100/24", "eth0")],
    default_gateway="10.0.1.1",
)


def _device(host: str = "10.0.1.1", port: int = 2404, protocol: str = "iec104") -> DeviceConfig:
    return DeviceConfig(
        device_id="wtg-001",
        protocol=protocol,
        endpoint=Endpoint(host=host, port=port),
    )


def _point(device_id: str = "wtg-001") -> PointConfig:
    return PointConfig(
        point_id="rotor.speed",
        device_id=device_id,
        address={"type": "measured_value", "ioa": 1001},
    )


class _FakeDriver:
    """协议层假驱动：可分别注入 connect / read 失败。"""

    def __init__(
        self,
        connect_exc: Exception | None = None,
        read_exc: Exception | None = None,
    ) -> None:
        self._connect_exc = connect_exc
        self._read_exc = read_exc
        self.points_mapped = False
        self.connected = False
        self.closed = False

    def set_points_mapping(self, points: list[PointConfig]) -> None:
        self.points_mapped = True

    async def connect(self) -> None:
        if self._connect_exc is not None:
            raise self._connect_exc
        self.connected = True

    async def read(self, points: list[Any]) -> list[PointValue]:
        if self._read_exc is not None:
            raise self._read_exc
        return [PointValue(device_id="wtg-001", point_id=points[0].point_id, value=12.5)]

    async def close(self) -> None:
        self.closed = True


class _Harness:
    """记录各层被调用情况的统一替身集合。"""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.arp: dict[str, str] = {}
        self.icmp_alive: set[str] = set()
        self.ping_available = True
        self.port_state = PortState.OPEN
        self.port_called = False
        self.driver: _FakeDriver | None = None
        self.next_driver: _FakeDriver | None = _FakeDriver()

        monkeypatch.setattr(diag, "get_local_info", self._local_info)
        monkeypatch.setattr(diag, "scan_arp", self._scan_arp)
        monkeypatch.setattr(diag, "scan_icmp", self._scan_icmp)
        monkeypatch.setattr(diag, "scan_port", self._scan_port)
        monkeypatch.setattr(diag, "_create_driver", self._create_driver)
        monkeypatch.setattr(diag.shutil, "which", self._which)

    async def _local_info(self) -> LocalInfo:
        return _LOCAL

    async def _scan_arp(self, network: str) -> dict[str, str]:
        return self.arp

    async def _scan_icmp(self, ips: list[str], timeout: float, concurrency: int) -> set[str]:
        return self.icmp_alive

    async def _scan_port(self, ip: str, port: int, timeout: float) -> PortResult:
        self.port_called = True
        return PortResult(port=port, state=self.port_state)

    def _create_driver(self, device_cfg: DeviceConfig) -> _FakeDriver:
        assert self.next_driver is not None
        self.driver = self.next_driver
        return self.driver

    def _which(self, name: str) -> str | None:
        return "/bin/ping" if self.ping_available else None


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> _Harness:
    return _Harness(monkeypatch)


def _steps_by_name(steps: list[DiagnoseStep]) -> dict[str, DiagnoseStep]:
    return {s.name: s for s in steps}


# ---------------------------------------------------------------------------
# 全通路径
# ---------------------------------------------------------------------------


async def test_diagnose_all_ok(harness: _Harness) -> None:
    harness.arp = {"10.0.1.1": "aa:bb:cc:dd:ee:01"}
    harness.icmp_alive = {"10.0.1.1"}
    result = await diag.diagnose_device(_device(), points=[_point()])

    assert result.overall is StepStatus.OK
    assert result.same_subnet is True
    assert result.subnet_detail == "10.0.1.0/24 (eth0)"
    assert all(s.status is StepStatus.OK and not s.skipped for s in result.steps)
    names = _steps_by_name(result.steps)
    assert "aa:bb:cc:dd:ee:01" in names["ARP"].detail
    assert names["TCP 2404"].detail.startswith("端口开放")
    assert "12.5" in names["IEC104"].detail
    assert harness.driver is not None and harness.driver.closed  # 诊断后释放连接


async def test_diagnose_loopback_all_ok_without_arp(harness: _Harness) -> None:
    """回环目标：无需 ARP 也视为同网段（本机），ARP 步骤直接 OK。"""
    harness.icmp_alive = {"127.0.0.1"}
    result = await diag.diagnose_device(_device(host="127.0.0.1"), points=[_point()])

    assert result.overall is StepStatus.OK
    assert result.same_subnet is True
    assert result.subnet_detail == "本机回环"
    names = _steps_by_name(result.steps)
    assert "无需 ARP" in names["ARP"].detail


# ---------------------------------------------------------------------------
# 短路（决策 2）
# ---------------------------------------------------------------------------


async def test_network_failure_short_circuits(harness: _Harness) -> None:
    """网络层失败 → TCP / 协议层跳过，且不再发起 TCP 探测。"""
    # arp 空、icmp 不通（默认即此状态）
    result = await diag.diagnose_device(_device(), points=[_point()])

    assert result.overall is StepStatus.FAIL
    assert harness.port_called is False
    names = _steps_by_name(result.steps)
    assert names["TCP 2404"].skipped is True
    assert "网络层失败" in names["TCP 2404"].detail
    assert names["IEC104"].skipped is True
    # 结论含网段不匹配提示（本机 10.0.1.100/24，目标 10.0.1.1 —— 实际同网段！
    # 这里目标同网段，所以 ARP 步骤的 fail 走「同网段但不通」分支）
    assert "同网段" in names["ARP"].detail


async def test_cross_subnet_failure_adds_subnet_note(harness: _Harness) -> None:
    """不同网段失败 → 结论前缀网段提示（决策 4）。"""
    result = await diag.diagnose_device(_device(host="192.168.9.9"), points=[])

    assert result.overall is StepStatus.FAIL
    assert result.same_subnet is False
    assert "不在同一网段" in result.conclusion
    assert "10.0.1.100/24" in result.conclusion
    assert "192.168.9.9" in result.conclusion
    names = _steps_by_name(result.steps)
    assert "不在同一网段" in names["ARP"].detail


async def test_transport_failure_skips_protocol(harness: _Harness) -> None:
    """传输层失败 → 协议层跳过，协议驱动不被创建。"""
    harness.icmp_alive = {"10.0.1.1"}
    harness.port_state = PortState.CLOSED
    result = await diag.diagnose_device(_device(), points=[_point()])

    assert result.overall is StepStatus.FAIL
    names = _steps_by_name(result.steps)
    assert names["TCP 2404"].status is StepStatus.FAIL
    assert "服务未启动" in (names["TCP 2404"].suggestion or "")
    assert names["IEC104"].skipped is True
    assert "传输层失败" in names["IEC104"].detail
    assert harness.driver is None  # 未创建驱动


# ---------------------------------------------------------------------------
# 各层定级
# ---------------------------------------------------------------------------


async def test_arp_miss_but_icmp_ok_is_warn(harness: _Harness) -> None:
    """决策 6 的 warn 示例：ARP 无记录但 ICMP 通 → 步骤 warn，整体 warn。"""
    harness.icmp_alive = {"10.0.1.1"}
    result = await diag.diagnose_device(_device(), points=[_point()])

    names = _steps_by_name(result.steps)
    assert names["ARP"].status is StepStatus.WARN
    assert result.overall is StepStatus.WARN


async def test_ping_missing_degrades_to_warn(harness: _Harness) -> None:
    """ping 二进制缺失 → ICMP 步骤 warn；ARP 命中仍放行传输层。"""
    harness.ping_available = False
    harness.arp = {"10.0.1.1": "aa:bb:cc:dd:ee:01"}
    result = await diag.diagnose_device(_device(), points=[_point()])

    names = _steps_by_name(result.steps)
    assert names["ICMP"].status is StepStatus.WARN
    assert "ping" in names["ICMP"].detail
    assert names["TCP 2404"].status is StepStatus.OK
    assert result.overall is StepStatus.WARN


async def test_port_timeout_with_ping_suggests_firewall(harness: _Harness) -> None:
    """决策 9：timeout + ping 通 → 疑似防火墙。"""
    harness.icmp_alive = {"10.0.1.1"}
    harness.port_state = PortState.TIMEOUT
    result = await diag.diagnose_device(_device(), points=[])

    names = _steps_by_name(result.steps)
    assert names["TCP 2404"].status is StepStatus.FAIL
    assert "防火墙" in (names["TCP 2404"].suggestion or "")


async def test_port_unreachable_suggests_route(harness: _Harness) -> None:
    """决策 9 + step20 任务 0.1：unreachable → 路由不存在。"""
    harness.icmp_alive = {"10.0.1.1"}
    harness.port_state = PortState.UNREACHABLE
    result = await diag.diagnose_device(_device(), points=[])

    names = _steps_by_name(result.steps)
    assert names["TCP 2404"].status is StepStatus.FAIL
    assert "路由不存在" in (names["TCP 2404"].suggestion or "")


# ---------------------------------------------------------------------------
# 协议层
# ---------------------------------------------------------------------------


async def test_protocol_connect_timeout_is_fail(harness: _Harness) -> None:
    """step21 任务 0.1：connect 包外层超时，截断驱动重试，判 fail。"""
    harness.icmp_alive = {"10.0.1.1"}

    class _HangDriver(_FakeDriver):
        async def connect(self) -> None:
            await asyncio.sleep(10)

    harness.next_driver = _HangDriver()
    result = await diag.diagnose_device(_device(), points=[_point()], connect_timeout=0.05)

    names = _steps_by_name(result.steps)
    assert names["IEC104"].status is StepStatus.FAIL
    assert "协议握手超时" in names["IEC104"].detail
    assert result.overall is StepStatus.FAIL
    assert harness.driver is not None and harness.driver.closed


async def test_protocol_handshake_failure_is_fail(harness: _Harness) -> None:
    harness.icmp_alive = {"10.0.1.1"}
    harness.next_driver = _FakeDriver(connect_exc=ProtocolError("STARTDT timeout"))
    result = await diag.diagnose_device(_device(), points=[_point()])

    assert result.overall is StepStatus.FAIL
    names = _steps_by_name(result.steps)
    assert names["IEC104"].status is StepStatus.FAIL
    assert "STARTDT" in names["IEC104"].detail


async def test_protocol_read_failure_is_warn(harness: _Harness) -> None:
    """握手成功但 read 失败 → warn（联通性已证实，疑似点表问题）。"""
    harness.icmp_alive = {"10.0.1.1"}
    harness.next_driver = _FakeDriver(read_exc=ProtocolError("unknown ioa"))
    result = await diag.diagnose_device(_device(), points=[_point()])

    names = _steps_by_name(result.steps)
    assert names["IEC104"].status is StepStatus.WARN
    assert "点表" in (names["IEC104"].suggestion or "")
    assert result.overall is StepStatus.WARN
    assert harness.driver is not None and harness.driver.closed


async def test_protocol_without_points_is_warn(harness: _Harness) -> None:
    """无点表：只验证到握手，整体 warn 提示未验证 read。"""
    harness.icmp_alive = {"10.0.1.1"}
    result = await diag.diagnose_device(_device(), points=[])

    names = _steps_by_name(result.steps)
    assert names["IEC104"].status is StepStatus.WARN
    assert "未验证 read" in names["IEC104"].detail
    assert result.overall is StepStatus.WARN


async def test_unknown_protocol_is_fail(harness: _Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    """未注册的协议名 → 协议层 fail（ConfigError 不上抛）。"""
    harness.icmp_alive = {"10.0.1.1"}

    def _raise(device_cfg: DeviceConfig) -> Any:
        raise ConfigError("Unknown protocol driver 'foo'")

    monkeypatch.setattr(diag, "_create_driver", _raise)
    result = await diag.diagnose_device(_device(protocol="foo"), points=[])

    names = _steps_by_name(result.steps)
    assert names["FOO"].status is StepStatus.FAIL
    assert "Unknown protocol" in names["FOO"].detail
    assert result.overall is StepStatus.FAIL


# ---------------------------------------------------------------------------
# 结论汇聚与建议（决策 6/9）
# ---------------------------------------------------------------------------


def test_build_conclusion_all_ok() -> None:
    steps = [
        DiagnoseStep("ARP", StepStatus.OK, "ok"),
        DiagnoseStep("ICMP", StepStatus.OK, "ok"),
    ]
    overall, text = diag._build_conclusion(steps)
    assert overall is StepStatus.OK
    assert "通过" in text


def test_build_conclusion_warn_only() -> None:
    steps = [
        DiagnoseStep("ARP", StepStatus.WARN, "无记录但通"),
        DiagnoseStep("ICMP", StepStatus.OK, "ok"),
    ]
    overall, text = diag._build_conclusion(steps)
    assert overall is StepStatus.WARN
    assert "ARP" in text


def test_build_conclusion_fail_wins_and_skips_ignored() -> None:
    steps = [
        DiagnoseStep("ARP", StepStatus.FAIL, "无记录", suggestion="检查路由"),
        DiagnoseStep("TCP 2404", StepStatus.OK, "跳过", skipped=True),
    ]
    overall, text = diag._build_conclusion(steps)
    assert overall is StepStatus.FAIL
    assert "ARP" in text
    assert "检查路由" in text


@pytest.mark.parametrize(
    ("state", "ping_ok", "expected_keyword"),
    [
        (PortState.TIMEOUT, True, "防火墙"),
        (PortState.TIMEOUT, False, None),
        (PortState.CLOSED, True, "服务未启动"),
        (PortState.CLOSED, False, "服务未启动"),
        (PortState.UNREACHABLE, True, "路由不存在"),
        (PortState.OPEN, True, None),
    ],
)
def test_suggest_from_port_state(
    state: PortState, ping_ok: bool, expected_keyword: str | None
) -> None:
    suggestion = diag._suggest_from_port_state(state, ping_ok)
    if expected_keyword is None:
        assert suggestion is None
    else:
        assert suggestion is not None and expected_keyword in suggestion
