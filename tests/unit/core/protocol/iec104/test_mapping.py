"""IEC104 配置解析与 (ca,ioa) 多 CA 寻址映射的单元测试。"""

from __future__ import annotations

import pytest

from core.application import ConfigError
from core.application.protocol_contract import ProtocolCapability
from core.domain import (
    ConnectionEndpoint,
    Point,
    PointAccess,
    PointTable,
)
from core.domain import (
    Protocol as DeviceProtocol,
)
from core.domain.identities import BusinessPointId, PointTableId
from core.infrastructure.protocol.iec104 import (
    IEC104Driver,
    build_iec104_index,
    parse_iec104_config,
    parse_iec104_point,
)
from tests.support.units import NONE_UNIT


def _endpoint() -> ConnectionEndpoint:
    return ConnectionEndpoint("192.0.2.30")


def _point(
    point_id: str,
    *,
    access: PointAccess = PointAccess.READ,
    ext: dict[str, str | int] | None = None,
) -> Point:
    return Point(
        point_id=point_id,
        business_point_id=BusinessPointId(point_id),
        source_unit=NONE_UNIT,
        access=access,
        ext={} if ext is None else ext,
    )


def _point_ext(
    *,
    ioa: int,
    type_id: str | None = None,
    ca: int | None = None,
) -> dict[str, str | int]:
    options: dict[str, str | int] = {"ioa": ioa}
    if type_id is not None:
        options["type_id"] = type_id
    if ca is not None:
        options["ca"] = ca
    return options


def _table(*points: Point) -> PointTable:
    return PointTable(
        PointTableId("iec_pt"),
        DeviceProtocol("iec104"),
        {point.point_id: point for point in points},
    )


# ---------------------------------------------------------------------------
# 配置解析
# ---------------------------------------------------------------------------


def test_iec104_config_defaults() -> None:
    config = parse_iec104_config(_endpoint(), {})

    assert config.port == 2404
    assert config.common_addresses == (1,)
    assert config.default_ca == 1
    assert (config.k, config.w) == (12, 8)
    assert (config.t0, config.t1, config.t2, config.t3) == (30.0, 15.0, 10.0, 20.0)
    assert config.health_point is None


def test_iec104_config_common_addresses_int_csv_dedup_and_range() -> None:
    assert parse_iec104_config(_endpoint(), {"common_addresses": 3}).common_addresses == (3,)
    assert parse_iec104_config(
        _endpoint(), {"common_addresses": "1, 2, 2, 65535"}
    ).common_addresses == (1, 2, 65535)

    with pytest.raises(ConfigError, match="common address"):
        parse_iec104_config(_endpoint(), {"common_addresses": "1,65536"})
    with pytest.raises(ConfigError, match="int or comma-separated"):
        parse_iec104_config(_endpoint(), {"common_addresses": True})
    with pytest.raises(ConfigError, match="int or comma-separated"):
        parse_iec104_config(_endpoint(), {"common_addresses": "  "})


def test_iec104_config_window_and_timeout_validation() -> None:
    with pytest.raises(ConfigError, match="must not exceed k"):
        parse_iec104_config(_endpoint(), {"k": 4, "w": 8})
    with pytest.raises(ConfigError, match="t2"):
        parse_iec104_config(_endpoint(), {"t1": 2.0, "t2": 3.0})
    with pytest.raises(ConfigError, match="unknown IEC104 options"):
        parse_iec104_config(_endpoint(), {"common_addr": 1})


# ---------------------------------------------------------------------------
# 点映射与多 CA 索引
# ---------------------------------------------------------------------------


def test_iec104_point_maps_full_address_and_type_id() -> None:
    mapped = parse_iec104_point(
        _point("active_power", ext=_point_ext(ioa=1001, ca=7, type_id="M_ME_NC_1")),
        default_ca=1,
    )

    assert mapped.ioa == 1001
    assert mapped.common_address == 7
    assert mapped.type_id == "M_ME_NC_1"


def test_iec104_point_defaults_to_connection_default_ca() -> None:
    mapped = parse_iec104_point(_point("p", ext=_point_ext(ioa=1)), default_ca=42)
    assert mapped.common_address == 42


def test_iec104_index_rejects_duplicate_address_within_same_ca() -> None:
    first = _point("p1", ext=_point_ext(ioa=100, ca=1))
    second = _point("p2", ext=_point_ext(ioa=100, ca=1))

    with pytest.raises(ConfigError, match="duplicate address"):
        build_iec104_index([first, second], common_addresses=(1, 2), default_ca=1)


def test_iec104_index_allows_same_ioa_under_different_ca() -> None:
    first = _point("ca1_speed", ext=_point_ext(ioa=100, ca=1))
    second = _point("ca2_speed", ext=_point_ext(ioa=100, ca=2))

    by_id, by_address = build_iec104_index([first, second], common_addresses=(1, 2), default_ca=1)

    assert by_id["ca1_speed"].common_address == 1
    assert by_id["ca2_speed"].common_address == 2
    assert by_address[(1, 100)].point_id == "ca1_speed"
    assert by_address[(2, 100)].point_id == "ca2_speed"


def test_iec104_index_rejects_ca_outside_connection_config() -> None:
    point = _point("p", ext=_point_ext(ioa=100, ca=9))

    with pytest.raises(ConfigError, match="not configured in common_addresses"):
        build_iec104_index([point], common_addresses=(1, 2), default_ca=1)


@pytest.mark.parametrize(
    ("ioa", "ca"),
    [(-1, 1), (0xFFFFFF + 1, 1), (1, -1), (1, 65536)],
)
def test_iec104_index_rejects_out_of_range_address(ioa: int, ca: int) -> None:
    point = _point("p", ext={"ioa": ioa, "ca": ca})

    with pytest.raises(ConfigError):
        build_iec104_index([point], common_addresses=(1,), default_ca=1)


def test_iec104_index_rejects_unknown_point_option() -> None:
    point = _point("p", ext={"ioa": 1, "register_type": "holding"})

    with pytest.raises(ConfigError, match="unknown options"):
        build_iec104_index([point], common_addresses=(1,), default_ca=1)


# ---------------------------------------------------------------------------
# Driver 装配：类型方向、健康点、能力
# ---------------------------------------------------------------------------


def test_iec104_writable_point_requires_command_type() -> None:
    point = _point("setpoint", access=PointAccess.WRITE, ext=_point_ext(ioa=2001))

    with pytest.raises(ConfigError, match="requires type_id"):
        IEC104Driver(_endpoint(), _table(point), {})


def test_iec104_driver_builds_with_command_point() -> None:
    point = _point(
        "setpoint",
        access=PointAccess.WRITE,
        ext=_point_ext(ioa=2001, type_id="C_SE_NC_1"),
    )

    driver = IEC104Driver(_endpoint(), _table(point), {})

    assert driver.is_open() is False
    assert ProtocolCapability.SUBSCRIBE not in driver.capabilities()
    assert driver.capabilities() == frozenset(
        {
            ProtocolCapability.READ,
            ProtocolCapability.WRITE,
            ProtocolCapability.LISTEN,
            ProtocolCapability.INTERROGATE,
        }
    )


def test_iec104_writable_point_rejects_monitoring_type() -> None:
    point = _point(
        "setpoint",
        access=PointAccess.WRITE,
        ext=_point_ext(ioa=2001, type_id="M_ME_NC_1"),
    )

    with pytest.raises(ConfigError, match="must use a C_\\* command type"):
        IEC104Driver(_endpoint(), _table(point), {})


def test_iec104_read_only_point_rejects_command_type() -> None:
    point = _point("breaker", ext=_point_ext(ioa=2001, type_id="C_SC_NA_1"))

    with pytest.raises(ConfigError, match="must use an M_\\* type"):
        IEC104Driver(_endpoint(), _table(point), {})


def test_iec104_index_default_ca_must_be_in_common_addresses() -> None:
    point = _point("p", ext=_point_ext(ioa=1))

    with pytest.raises(ConfigError, match="default CA"):
        build_iec104_index([point], common_addresses=(2, 3), default_ca=1)


def test_iec104_point_type_alias_and_uppercasing() -> None:
    mapped = parse_iec104_point(
        _point("p", ext={"ioa": 1, "type": " m_me_nc_1 "}),
        default_ca=1,
    )
    assert mapped.type_id == "M_ME_NC_1"


def test_iec104_point_rejects_non_integer_addresses_and_blank_type() -> None:
    with pytest.raises(ConfigError, match="ioa must be an integer"):
        parse_iec104_point(_point("p", ext={"ioa": "1"}), default_ca=1)
    with pytest.raises(ConfigError, match="ca must be an integer"):
        parse_iec104_point(_point("p", ext={"ioa": 1, "ca": "2"}), default_ca=1)
    with pytest.raises(ConfigError, match="non-empty string"):
        parse_iec104_point(_point("p", ext={"ioa": 1, "type_id": "  "}), default_ca=1)


@pytest.mark.parametrize(
    ("access", "extra", "expected"),
    [
        (PointAccess.READ_WRITE, {"type_id": "C_SC_NA_1"}, None),
        (PointAccess.WRITE, {"type_id": "C_DC_NA_1"}, None),
        (PointAccess.READ_WRITE, {}, "requires type_id"),
        (PointAccess.READ, {"type_id": "C_SC_NA_1"}, "must use an M_"),
        (PointAccess.READ, {"type_id": "UNKNOWN_X_1"}, "must use an M_"),
        (PointAccess.WRITE, {"type_id": "M_ME_NC_1"}, "must use a C_"),
        (PointAccess.WRITE, {"type_id": "M_TS_TA_1"}, "must use a C_"),
    ],
)
def test_iec104_access_direction_type_validation(
    access: PointAccess, extra: dict[str, str], expected: str | None
) -> None:
    point = _point("p", access=access, ext={"ioa": 1, **extra})
    if expected is None:
        by_id, _ = build_iec104_index([point], common_addresses=(1,), default_ca=1)
        assert by_id["p"].common_address == 1
    else:
        with pytest.raises(ConfigError, match=expected):
            build_iec104_index([point], common_addresses=(1,), default_ca=1)


def test_iec104_read_point_without_type_id_is_allowed() -> None:
    # 读取方向类型随对端上报报文确定，无需本地 type_id。
    mapped = parse_iec104_point(_point("p", ext={"ioa": 1}), default_ca=1)
    assert mapped.type_id is None


def test_iec104_command_mode_and_confirmation_validation() -> None:
    ok = parse_iec104_point(
        _point(
            "p",
            access=PointAccess.WRITE,
            ext={
                "ioa": 1,
                "type_id": "C_SC_NA_1",
                "command_mode": "select_execute",
                "confirmation": "act_con",
            },
        ),
        default_ca=1,
    )
    assert ok.command_mode == "select_execute"
    assert ok.confirmation == "act_con"

    defaults = parse_iec104_point(
        _point(
            "p",
            access=PointAccess.WRITE,
            ext={"ioa": 1, "type_id": "C_SC_NA_1"},
        ),
        default_ca=1,
    )
    assert defaults.command_mode == "direct"
    assert defaults.confirmation == "act_term"

    with pytest.raises(ConfigError, match="command_mode"):
        parse_iec104_point(
            _point(
                "p",
                access=PointAccess.WRITE,
                ext={
                    "ioa": 1,
                    "type_id": "C_SC_NA_1",
                    "command_mode": "sbo",
                },
            ),
            default_ca=1,
        )
    with pytest.raises(ConfigError, match="confirmation"):
        parse_iec104_point(
            _point(
                "p",
                access=PointAccess.WRITE,
                ext={
                    "ioa": 1,
                    "type_id": "C_SC_NA_1",
                    "confirmation": "act_ack",
                },
            ),
            default_ca=1,
        )


def test_iec104_data_type_option_is_recognized_but_not_command_type() -> None:
    # data_type 是允许的透传选项，但不参与 type_id 解析。
    mapped = parse_iec104_point(
        _point("p", ext={"ioa": 1, "data_type": "float32"}),
        default_ca=1,
    )
    assert mapped.type_id is None


def test_iec104_health_point_must_exist_in_point_table() -> None:
    point = _point("power", ext=_point_ext(ioa=100, type_id="M_ME_NC_1"))

    with pytest.raises(ConfigError, match="health point"):
        IEC104Driver(
            _endpoint(),
            _table(point),
            {"health_point": "missing"},
        )
