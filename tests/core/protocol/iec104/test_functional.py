"""IEC104 完整地址、类型与访问方向的独立单元测试。"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from hat.drivers.iec101 import common as iec101

from core.application.errors import ConfigError
from core.application.protocol_contract import ProtocolLifecycleState, ProtocolSample, Quality
from core.domain import Point, PointAccess
from core.domain.identities import BusinessPointId
from core.infrastructure.protocol.iec104.codec import build_command, decode_quality
from core.infrastructure.protocol.iec104.mapping import (
    IEC104Point,
    build_iec104_index,
    parse_iec104_point,
    validate_iec104_write_type,
)
from tests.support.units import NONE_UNIT

if TYPE_CHECKING:
    from core.infrastructure.protocol.iec104 import IEC104Driver


def _point(
    point_id: str,
    *,
    access: PointAccess = PointAccess.READ,
    **ext: str | int,
) -> Point:
    return Point(
        point_id=point_id,
        business_point_id=BusinessPointId(point_id),
        source_unit=NONE_UNIT,
        access=access,
        ext=ext,
    )


def test_index_keys_include_both_ca_and_ioa() -> None:
    points = [
        _point("a", ca=1, ioa=100, type_id="M_ME_NC_1"),
        _point("b", ca=2, ioa=100, type_id="M_ME_NC_1"),
    ]
    by_id, by_address = build_iec104_index(points)
    assert by_address[(1, 100)] == by_id["a"]
    assert by_address[(2, 100)] == by_id["b"]


def test_duplicate_address_within_ca_is_rejected() -> None:
    points = [_point("a", ca=1, ioa=1), _point("b", ca=1, ioa=1)]
    with pytest.raises(ConfigError, match="duplicate address"):
        build_iec104_index(points)


@pytest.mark.parametrize(
    ("ca", "ioa"),
    [(True, 1), (-1, 1), (65536, 1), (1, True), (1, -1), (1, 0x1000000)],
)
def test_invalid_protocol_address_is_rejected(ca: int, ioa: int) -> None:
    with pytest.raises(ConfigError):
        parse_iec104_point(_point("p", ca=ca, ioa=ioa))


def test_ca_is_required_and_monitoring_type_is_normalized() -> None:
    with pytest.raises(ConfigError, match="ca"):
        parse_iec104_point(_point("p", ioa=10))
    mapped = parse_iec104_point(_point("p", ca=1, ioa=10, type_id=" m_me_nc_1 "))
    assert mapped.type_id == "M_ME_NC_1"
    assert mapped.confirmation == "act_term"


def test_command_configuration_and_type_validation() -> None:
    command = _point(
        "cmd",
        access=PointAccess.WRITE,
        ca=1,
        ioa=100,
        type_id="C_SE_NC_1",
        command_mode="select_execute",
        confirmation="act_con",
    )
    mapped = parse_iec104_point(command)
    validate_iec104_write_type(mapped)
    assert mapped.command_mode == "select_execute"
    assert mapped.confirmation == "act_con"


@pytest.mark.parametrize("type_id", ["M_ME_NC_1", "C_RC_NA_1", "C_BO_NA_1"])
def test_unsupported_writable_types_are_rejected(type_id: str) -> None:
    with pytest.raises(ConfigError):
        build_iec104_index([_point("cmd", access=PointAccess.WRITE, ca=1, ioa=1, type_id=type_id)])


def test_monitoring_point_cannot_have_control_options() -> None:
    with pytest.raises(ConfigError, match="command options"):
        parse_iec104_point(
            _point("p", ca=1, ioa=1, type_id="M_SP_NA_1", command_mode="select_execute")
        )


def test_read_only_point_cannot_use_control_type() -> None:
    with pytest.raises(ConfigError, match="M_"):
        build_iec104_index([_point("p", ca=1, ioa=1, type_id="C_SC_NA_1")])


def test_unknown_point_options_are_rejected() -> None:
    with pytest.raises(ConfigError, match="unknown options"):
        parse_iec104_point(_point("p", ca=1, ioa=1, register_type="holding"))


def _mapped(type_id: str) -> IEC104Point:
    return IEC104Point(point_id="p", common_address=1, ioa=100, type_id=type_id)


def test_single_command_requires_bool_and_encodes_on() -> None:
    command = build_command(_mapped("C_SC_NA_1"), True)
    assert isinstance(command, iec101.SingleCommand)
    assert command.value is iec101.SingleValue.ON
    assert command.select is False
    with pytest.raises(TypeError):
        build_command(_mapped("C_SC_NA_1"), 1)


@pytest.mark.parametrize("value", [1, 2])
def test_double_command_values(value: int) -> None:
    command = build_command(_mapped("C_DC_NA_1"), value)
    assert isinstance(command, iec101.DoubleCommand)
    assert command.value is (iec101.DoubleValue.OFF if value == 1 else iec101.DoubleValue.ON)


@pytest.mark.parametrize("value", [0, 3, True])
def test_double_command_rejects_invalid_values(value: object) -> None:
    with pytest.raises((ValueError, TypeError)):
        build_command(_mapped("C_DC_NA_1"), value)


def test_normalized_command_range() -> None:
    assert isinstance(build_command(_mapped("C_SE_NA_1"), 0.5), iec101.NormalizedCommand)
    with pytest.raises(ValueError):
        build_command(_mapped("C_SE_NA_1"), 1.5)


def test_scaled_command_integer_range() -> None:
    assert isinstance(build_command(_mapped("C_SE_NB_1"), -32768), iec101.ScaledCommand)
    with pytest.raises(ValueError):
        build_command(_mapped("C_SE_NB_1"), 32768)


def test_floating_command_rejects_non_finite_value() -> None:
    assert isinstance(build_command(_mapped("C_SE_NC_1"), 12.5), iec101.FloatingCommand)
    with pytest.raises(ValueError, match="finite"):
        build_command(_mapped("C_SE_NC_1"), float("nan"))


def test_unsupported_command_does_not_fall_back() -> None:
    with pytest.raises(ConfigError, match="unsupported command"):
        build_command(_mapped("C_RC_NA_1"), 1)


def test_monitoring_quality_precedence() -> None:
    assert decode_quality(None) is Quality.UNCERTAIN
    assert decode_quality(SimpleNamespace(invalid=False, blocked=False)) is Quality.GOOD
    assert decode_quality(SimpleNamespace(invalid=False, blocked=True)) is Quality.UNCERTAIN
    assert decode_quality(SimpleNamespace(invalid=True, blocked=True)) is Quality.BAD


class _FakeConnectedSession:
    """立即完成 pending C_RD 的在线验证替身；不发起任何真实网络 I/O。"""

    is_open = True

    def __init__(self, driver: IEC104Driver) -> None:
        self._driver = driver
        self.sent: list[object] = []

    async def send(self, messages: list[object]) -> None:
        self.sent.extend(messages)
        for pending in list(self._driver._read.values()):
            if not pending.result.done():
                pending.result.set_result(
                    ProtocolSample(
                        point_id=pending.point.point_id,
                        value=0,
                        timestamp=datetime.now(UTC),
                    )
                )


async def test_connected_point_table_update_reuses_locks_and_changes_mapping() -> None:
    """点表热更新不拆会话，同地址保留锁，新地址获得独立锁。"""
    from core.application.protocol_contract import PointTableUpdateStatus
    from core.domain import ConnectionEndpoint
    from core.infrastructure.protocol.iec104 import IEC104Driver
    from tests.core.protocol.conftest import make_table

    old = _point("a", ca=1, ioa=100, type_id="M_ME_NC_1")
    new = _point("b", ca=1, ioa=200, type_id="M_ME_NC_1")
    driver = IEC104Driver(
        ConnectionEndpoint("127.0.0.1", 2404),
        make_table("iec104", old),
        {},
    )
    locked = driver._locks[(1, 100)]
    station_lock = driver._station_locks[1]
    driver._state = ProtocolLifecycleState.CONNECTED
    driver._session = _FakeConnectedSession(driver)

    result = await driver.update_point_table(make_table("iec104", old, new), timeout=5.0)

    assert result.status is PointTableUpdateStatus.SUCCESS
    assert result.applied is True
    assert result.active_version == 1
    assert driver._locks[(1, 100)] is locked
    assert driver._station_locks[1] is station_lock
    assert (1, 200) in driver._locks
    assert driver._by_address[(1, 200)].point_id == "b"
    assert driver._state is ProtocolLifecycleState.CONNECTED


async def test_invalid_connected_table_update_keeps_original_snapshot() -> None:
    """重复地址在线校验失败时返回 VALIDATION_FAILED，原映射与代次保持不变。"""
    from core.application.protocol_contract import PointTableUpdateStatus
    from core.domain import ConnectionEndpoint
    from core.infrastructure.protocol.iec104 import IEC104Driver
    from tests.core.protocol.conftest import make_table

    old = _point("a", ca=1, ioa=100, type_id="M_ME_NC_1")
    duplicate = _point("b", ca=1, ioa=100, type_id="M_ME_NC_1")
    driver = IEC104Driver(
        ConnectionEndpoint("127.0.0.1", 2404),
        make_table("iec104", old),
        {},
    )
    driver._state = ProtocolLifecycleState.CONNECTED
    driver._session = _FakeConnectedSession(driver)
    original = driver._by_address

    result = await driver.update_point_table(make_table("iec104", old, duplicate), timeout=5.0)

    assert result.status is PointTableUpdateStatus.VALIDATION_FAILED
    assert "duplicate address" in result.message
    assert result.applied is False
    assert driver._by_address is original
