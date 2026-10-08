"""新 Collector Modbus Sink（codec/store/pipeline/sink 组合）单元测试。

不建立网络连接：验证编码、内存数据区布局、数据路径质量过滤与
Sink.write → store 的端到端内存路径。
"""

from __future__ import annotations

import dataclasses

import pytest

from collector.application.sink_export import ExportedSinkPointValue
from collector.application.sinks import (
    ModbusSinkAddress,
    ModbusSinkConnection,
    ResolvedSinkConfig,
    ResolvedSinkPoint,
    SinkSource,
)
from collector.domain.point_value import PointValue
from collector.infrastructure.sink.modbus.codec import encode_modbus_value
from collector.infrastructure.sink.modbus.pipeline import ModbusSinkDataPath
from collector.infrastructure.sink.modbus.sink import ModbusSink
from collector.infrastructure.sink.modbus.store import ModbusSinkStore
from core.application import ConfigError, Quality


def _point(
    *,
    device_id: str = "d1",
    point_id: str = "p1",
    datatype: str = "float32",
    scale: float = 1.0,
    offset: float = 0.0,
    register_type: str = "holding",
    address: int = 100,
    unit_id: int = 1,
    byte_order: str = "big",
    word_order: str = "big",
) -> ResolvedSinkPoint:
    return ResolvedSinkPoint(
        source=SinkSource(device_id=device_id, point_id=point_id),
        ref=f"{device_id}.{point_id}",
        source_data_type=datatype,
        source_unit="none",
        datatype=datatype,
        unit="none",
        scale=scale,
        offset=offset,
        address=ModbusSinkAddress(
            register_type=register_type,
            address=address,
            unit_id=unit_id,
            byte_order=byte_order,
            word_order=word_order,
        ),
    )


def _exported(point: ResolvedSinkPoint, value: object) -> ExportedSinkPointValue:
    return ExportedSinkPointValue(
        definition=point,
        value=value,
        quality=Quality.GOOD,
        timestamp=PointValue(device_id="d", point_id="p", value=0).timestamp,
        source_protocol="modbus",
    )


# ---------------------------------------------------------------------------
# codec
# ---------------------------------------------------------------------------


def test_encode_float32_big_endian() -> None:
    encoded = encode_modbus_value(_exported(_point(), 1.0))
    assert encoded.registers == (0x3F80, 0x0000)


def test_encode_float32_little_word_order() -> None:
    encoded = encode_modbus_value(_exported(_point(word_order="little"), 1.0))
    assert encoded.registers == (0x0000, 0x3F80)


def test_encode_uint16_and_bool_coil() -> None:
    encoded = encode_modbus_value(_exported(_point(datatype="uint16"), 513))
    assert encoded.registers == (0x0201,)

    coil = encode_modbus_value(_exported(_point(datatype="bool", register_type="coil"), True))
    assert coil.bits == (True,)
    assert coil.registers == ()


def test_encode_type_mismatch_rejected() -> None:
    with pytest.raises(TypeError):
        encode_modbus_value(_exported(_point(), "not-a-number"))
    with pytest.raises(TypeError, match="bool value"):
        encode_modbus_value(_exported(_point(datatype="bool", register_type="coil"), 1))
    with pytest.raises(ValueError, match="value is None"):
        encode_modbus_value(_exported(_point(), None))


# ---------------------------------------------------------------------------
# store
# ---------------------------------------------------------------------------


def test_store_predeclares_layout_and_roundtrip() -> None:
    store = ModbusSinkStore([_point()])  # float32 → 2 registers at 100..101
    assert store.unit_ids == (1,)
    assert store.read_registers(1, "holding", 100, 2) == [0, 0]

    store.write(encode_modbus_value(_exported(_point(), 1.0)))
    assert store.read_registers(1, "holding", 100, 2) == [0x3F80, 0x0000]


def test_store_rejects_undeclared_and_unknown_unit() -> None:
    store = ModbusSinkStore([_point()])
    encoded = encode_modbus_value(_exported(_point(), 1.0))
    with pytest.raises(KeyError, match="Unknown Modbus unit_id"):
        store.write(dataclasses.replace(encoded, unit_id=9))
    with pytest.raises(KeyError):
        store.read_registers(1, "holding", 200, 1)  # 未声明地址


# ---------------------------------------------------------------------------
# pipeline（质量过滤）
# ---------------------------------------------------------------------------


def test_pipeline_updates_only_good_values() -> None:
    path = ModbusSinkDataPath([_point()])
    good = PointValue(device_id="d1", point_id="p1", value=1.0, quality=Quality.GOOD)
    bad = PointValue(device_id="d1", point_id="p1", value=9.0, quality=Quality.BAD)
    none_value = PointValue(device_id="d1", point_id="p1", value=None)

    assert path.update([bad]) == 0
    assert path.update([none_value]) == 0
    assert path.update([good]) == 1
    assert path.store.read_registers(1, "holding", 100, 2) == [0x3F80, 0x0000]


def test_pipeline_applies_sink_scale_offset() -> None:
    path = ModbusSinkDataPath([_point(datatype="uint16", scale=2.0, offset=1.0)])
    updated = path.update([PointValue(device_id="d1", point_id="p1", value=3.0)])
    assert updated == 1
    assert path.store.read_registers(1, "holding", 100, 1) == [7]


# ---------------------------------------------------------------------------
# ModbusSink（不 start server）
# ---------------------------------------------------------------------------


def _sink_config(**conn: object) -> ResolvedSinkConfig:
    return ResolvedSinkConfig(
        name="mb",
        type="modbus",
        connection=ModbusSinkConnection(host="127.0.0.1", port=15020, **conn),  # type: ignore[arg-type]
        points=[_point()],
    )


def test_sink_requires_modbus_connection() -> None:
    from collector.application.sinks import FileSinkConnection

    with pytest.raises(ConfigError, match="ModbusSinkConnection"):
        ModbusSink(
            ResolvedSinkConfig.model_construct(
                name="mb",
                type="modbus",
                connection=FileSinkConnection(path="/tmp/x.jsonl"),
                points=[],
            )
        )


async def test_sink_write_flows_to_store_without_network() -> None:
    sink = ModbusSink(_sink_config())
    assert sink.exclusive_open is True
    assert not sink.health().healthy  # 未监听
    await sink.write([PointValue(device_id="d1", point_id="p1", value=1.0)])
    assert sink.data_path.store.read_registers(1, "holding", 100, 2) == [
        0x3F80,
        0x0000,
    ]


async def test_sink_server_lifecycle_on_real_port() -> None:
    sink = ModbusSink(_sink_config())
    await sink.open()
    assert sink.health().healthy
    assert sink.port == 15020
    await sink.close()
    assert not sink.health().healthy
    await sink.close()  # 幂等
