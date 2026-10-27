"""IEC104 完整地址、类型与访问方向的独立单元测试。"""

from __future__ import annotations

import pytest

from core.application.errors import ConfigError
from core.domain import Point, PointAccess
from core.domain.identities import BusinessPointId
from core.infrastructure.protocol.iec104.mapping import (
    build_iec104_index,
    parse_iec104_point,
    validate_iec104_write_type,
)
from tests.support.units import NONE_UNIT


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
