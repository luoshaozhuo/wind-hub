"""ADS 动态点（显式 index 地址，不登记 PointTable）读写测试。

fake pyads Connection：动态 ADSPoint 直接按 index_group/index_offset
读写，不做 symbol 解析；批读覆盖 sequential 与 Sum Read 两种模式。
"""

from __future__ import annotations

import struct

import pytest

import core.infrastructure.protocol.ads.driver as driver_module
from core.application import ConfigError, ProtocolError, ProtocolWrite
from core.application.protocol_contract import Quality
from core.application.recovery import RecoveringProtocol, RecoverySettings
from core.domain import ConnectionEndpoint, PointTable, Protocol
from core.infrastructure.protocol.ads import ADSDriver, ADSPoint, ads_point


class _Connection:
    """按 index 地址读写的最小 pyads Connection fake。"""

    def __init__(self) -> None:
        self.read_calls: list[tuple[int, int]] = []
        self.write_calls: list[tuple[int, int, object]] = []
        self.values: dict[tuple[int, int], object] = {}
        self.read_error: Exception | None = None
        self.write_error: Exception | None = None

    def read(self, index_group: int, index_offset: int, datatype: object) -> object:
        del datatype
        self.read_calls.append((index_group, index_offset))
        if self.read_error is not None:
            raise self.read_error
        return self.values[(index_group, index_offset)]

    def write(
        self, index_group: int, index_offset: int, value: object, datatype: object
    ) -> None:
        del datatype
        if self.write_error is not None:
            raise self.write_error
        self.write_calls.append((index_group, index_offset, value))


def _driver(
    connection: _Connection,
    *,
    read_mode: str = "sequential",
    point_table: PointTable | None = None,
) -> ADSDriver:
    table = (
        point_table
        if point_table is not None
        else PointTable("ads", Protocol("ads"), {})
    )
    options = {"read_mode": read_mode} if read_mode != "sum" else {}
    driver = ADSDriver(ConnectionEndpoint("192.0.2.20", 801), table, options)
    driver._connection = connection
    driver._connected = True
    return driver


def _dyn(
    point_id: str = "dyn",
    *,
    offset: int = 100,
    data_type: str = "DINT",
) -> ADSPoint:
    return ads_point(
        point_id,
        index_group=0x4020,
        index_offset=offset,
        data_type=data_type,
    )


# ---------------------------------------------------------------------------
# ads_point 工厂校验
# ---------------------------------------------------------------------------


class TestADSPointFactory:
    def test_defaults_size_from_type_width(self) -> None:
        point = ads_point("d", index_group=1, index_offset=2, data_type="uint16")
        assert point.data_type == "UINT"
        assert point.size == 2
        assert point.symbol is None
        assert point.address_resolved is True

    def test_rejects_bool_and_negative_index(self) -> None:
        with pytest.raises(ConfigError, match="index_group must be an integer"):
            ads_point("d", index_group=True, index_offset=0, data_type="DINT")
        with pytest.raises(ConfigError, match="index_offset must be >= 0"):
            ads_point("d", index_group=0, index_offset=-1, data_type="DINT")

    def test_rejects_unknown_type_and_size_mismatch(self) -> None:
        with pytest.raises(ConfigError, match="unsupported ADS type"):
            ads_point("d", index_group=0, index_offset=0, data_type="FLOAT128")
        with pytest.raises(ConfigError, match="does not match"):
            ads_point("d", index_group=0, index_offset=0, data_type="DINT", size=2)

    def test_string_allows_explicit_size(self) -> None:
        point = ads_point("d", index_group=0, index_offset=0, data_type="str", size=81)
        assert point.data_type == "STRING"
        assert point.size == 81
        with pytest.raises(ConfigError, match="size must be >= 0"):
            ads_point("d", index_group=0, index_offset=0, data_type="STRING", size=-1)


# ---------------------------------------------------------------------------
# 单点读写
# ---------------------------------------------------------------------------


class TestDynamicReadOne:
    @pytest.mark.asyncio
    async def test_unregistered_dynamic_point_reads_by_index(self) -> None:
        connection = _Connection()
        connection.values[(0x4020, 100)] = 42
        driver = _driver(connection)

        sample = await driver.read_one(_dyn())

        assert sample.point_id == "dyn"
        assert sample.value == 42
        assert sample.quality == Quality.GOOD
        assert sample.timestamp.tzinfo is not None
        assert connection.read_calls == [(0x4020, 100)]

    @pytest.mark.asyncio
    async def test_dynamic_read_never_queries_point_table(self) -> None:
        connection = _Connection()
        connection.values[(0x4020, 100)] = 7
        driver = _driver(connection)

        def fail(point_id: str) -> ADSPoint:
            raise AssertionError(f"point table queried for '{point_id}'")

        driver._mapped_point = fail  # type: ignore[method-assign]
        sample = await driver.read_one(_dyn())
        assert sample.value == 7

    @pytest.mark.asyncio
    async def test_same_point_id_different_addresses_are_distinct(self) -> None:
        connection = _Connection()
        connection.values[(0x4020, 100)] = 1
        connection.values[(0x4020, 200)] = 2
        driver = _driver(connection)

        first = await driver.read_one(_dyn("dup", offset=100))
        second = await driver.read_one(_dyn("dup", offset=200))

        assert (first.value, second.value) == (1, 2)
        assert connection.read_calls == [(0x4020, 100), (0x4020, 200)]

    @pytest.mark.asyncio
    async def test_dynamic_read_skips_symbol_resolution_state(self) -> None:
        """动态点 address_resolved 恒 True：断连失效流程不影响显式地址。"""
        connection = _Connection()
        connection.values[(0x4020, 100)] = 5
        driver = _driver(connection)
        # 模拟重连前的 symbol 地址失效——动态点不在 _points 中，不受影响。
        driver._invalidate_symbol_addresses()

        sample = await driver.read_one(_dyn())
        assert sample.value == 5


class TestDynamicWriteOne:
    @pytest.mark.asyncio
    async def test_unregistered_dynamic_point_writes_by_index(self) -> None:
        connection = _Connection()
        driver = _driver(connection)

        result = await driver.write_one(_dyn(), 123)

        assert result.success
        assert result.point_id == "dyn"
        assert connection.write_calls == [(0x4020, 100, 123)]

    @pytest.mark.asyncio
    async def test_dynamic_write_validates_value(self) -> None:
        connection = _Connection()
        driver = _driver(connection)

        result = await driver.write_one(_dyn(data_type="BOOL"), 1)

        assert not result.success
        assert "bool" in (result.message or "")
        assert connection.write_calls == []

    @pytest.mark.asyncio
    async def test_dynamic_write_requires_explicit_value(self) -> None:
        driver = _driver(_Connection())
        with pytest.raises(TypeError, match="requires an explicit value"):
            await driver.write_one(_dyn())

    @pytest.mark.asyncio
    async def test_protocol_write_form_still_works(self) -> None:
        connection = _Connection()
        driver = _driver(connection)
        driver._points["reg"] = _dyn("reg")
        result = await driver.write_one(ProtocolWrite("reg", 9))
        assert result.success
        assert connection.write_calls == [(0x4020, 100, 9)]
        with pytest.raises(TypeError, match="does not take a separate value"):
            await driver.write_one(ProtocolWrite("reg", 9), 1)

    @pytest.mark.asyncio
    async def test_dynamic_write_transport_error_raises_once(self) -> None:
        connection = _Connection()
        connection.write_error = ConnectionError("route lost")
        driver = _driver(connection)

        with pytest.raises(ProtocolError, match="write failed"):
            await driver.write_one(_dyn(), 1)

        assert driver.is_open() is False


# ---------------------------------------------------------------------------
# 批量读取
# ---------------------------------------------------------------------------


class TestDynamicReadMany:
    @pytest.mark.asyncio
    async def test_pure_dynamic_batch_preserves_order_and_duplicates(self) -> None:
        connection = _Connection()
        connection.values[(0x4020, 100)] = 1
        connection.values[(0x4020, 104)] = 2
        driver = _driver(connection)

        samples = await driver.read_many([_dyn(offset=100), _dyn("b", offset=104),
                                          _dyn(offset=100)])

        assert [(s.point_id, s.value, s.quality) for s in samples] == [
            ("dyn", 1, Quality.GOOD),
            ("b", 2, Quality.GOOD),
            ("dyn", 1, Quality.GOOD),
        ]

    @pytest.mark.asyncio
    async def test_mixed_registered_and_dynamic_batch(self) -> None:
        connection = _Connection()
        connection.values[(0x4020, 100)] = 10
        connection.values[(0x4020, 200)] = 20
        driver = _driver(connection)
        driver._points["reg"] = _dyn("reg", offset=200)

        samples = await driver.read_many(["reg", _dyn(offset=100)])

        assert [(s.point_id, s.value) for s in samples] == [("reg", 20), ("dyn", 10)]

    @pytest.mark.asyncio
    async def test_sum_mode_keeps_dynamic_points_on_sum_read(self) -> None:
        """sum 模式下动态点使用 index Sum Read，不降级 read_list_by_name。"""

        class _SumConnection(_Connection):
            def __init__(self) -> None:
                super().__init__()
                self.sum_calls: list[object] = []

            def read_write(
                self,
                index_group: int,
                index_offset: int,
                read_datatype: object,
                value: object,
                write_datatype: object,
                *,
                check_length: bool,
            ) -> bytes:
                del read_datatype, write_datatype, check_length
                self.sum_calls.append((index_group, index_offset))
                # 两个 DINT 子请求：各 4 字节错误码 + 4 字节数据。
                return struct.pack("<IIii", 0, 0, 111, 222)

        import pyads

        monkey_connection = _SumConnection()
        driver = _driver(monkey_connection, read_mode="sum")
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(driver_module, "_pyads", lambda: pyads)
        try:
            samples = await driver.read_many(
                [_dyn("a", offset=100), _dyn("b", offset=104)]
            )
        finally:
            monkeypatch.undo()

        assert [s.value for s in samples] == [111, 222]
        assert monkey_connection.sum_calls != []
        assert monkey_connection.read_calls == []


# ---------------------------------------------------------------------------
# RecoveringProtocol 透传
# ---------------------------------------------------------------------------


class TestDynamicPointsThroughRecovery:
    @pytest.mark.asyncio
    async def test_recovery_passthrough_read_and_write(self) -> None:
        connection = _Connection()
        connection.values[(0x4020, 100)] = 33
        driver = _driver(connection)
        wrapped = RecoveringProtocol(driver, RecoverySettings(read_retries=1))

        sample = await wrapped.read_one(_dyn())
        assert sample.value == 33

        result = await wrapped.write_one(_dyn(), 44)
        assert result.success
        assert connection.write_calls == [(0x4020, 100, 44)]

        samples = await wrapped.read_many([_dyn(), _dyn("b", offset=100)])
        assert [s.value for s in samples] == [33, 33]
