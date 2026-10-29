"""R5-5：NATS → Sink Service（Modbus TCP Server 型）真实下行寄存器链路。

生产 ``ModbusSink`` 在本机起只读 Modbus TCP Server；测试侧用独立
``pymodbus`` 主站客户端读线圈/保持寄存器核对 16/32/64 位编码与字节/字序，
并验证：主站写被 ILLEGAL_FUNCTION 拒绝、非法/重叠配置在装配层拒绝、
监听端口冲突失败后自动恢复、进程/管理面重启后寄存器重新可读。
不得修改任何 Protocol Adapter——本套件只驱动 sink_service。
"""

from __future__ import annotations

import socket
from pathlib import Path
from typing import Any

import pytest

from core.application.errors import ConfigError
from tests.support.config_helper import write_config_tree
from tests.support.process import free_port
from tests.support.wait import wait_until
from tests.system.r5.conftest import make_sample, mgmt, publish, wait_state
from tests.system.r5.r5_config import DEVICE_ID, TASK_ID

pytestmark = [pytest.mark.system, pytest.mark.real_service, pytest.mark.modbus]

SLAVE = 1
POINTS = {
    "speed_f32": {
        "point_groups": ["telemetry"],
        "address": {"register_type": "holding", "address": 0},
        "data_type": "float32",
    },
    "power_u64": {
        "point_groups": ["telemetry"],
        "address": {"register_type": "holding", "address": 8},
        "data_type": "uint64",
    },
    "count_i32": {
        "point_groups": ["telemetry"],
        "address": {"register_type": "holding", "address": 16},
        "data_type": "int32",
    },
    "temp_i16": {
        "point_groups": ["telemetry"],
        "address": {"register_type": "holding", "address": 20},
        "data_type": "int16",
    },
    "flag_b": {
        "point_groups": ["telemetry"],
        "address": {"register_type": "coil", "address": 0},
        "data_type": "bool",
    },
}


def _modbus_sink(listen_port: int, *, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    points = [
        {
            "source": {"device_id": DEVICE_ID, "point_id": "speed_f32"},
            "datatype": "float32",
            "address": {
                "slave_id": SLAVE,
                "register_type": "holding",
                "address": 100,
                "byte_order": "big",
                "word_order": "big",
            },
        },
        {
            "source": {"device_id": DEVICE_ID, "point_id": "power_u64"},
            "datatype": "uint64",
            "address": {
                "slave_id": SLAVE,
                "register_type": "holding",
                "address": 110,
                "byte_order": "little",
                "word_order": "little",
            },
        },
        {
            "source": {"device_id": DEVICE_ID, "point_id": "count_i32"},
            "datatype": "int32",
            "address": {
                "slave_id": SLAVE,
                "register_type": "holding",
                "address": 120,
                "byte_order": "big",
                "word_order": "little",
            },
        },
        {
            "source": {"device_id": DEVICE_ID, "point_id": "temp_i16"},
            "datatype": "int16",
            "address": {"slave_id": SLAVE, "register_type": "holding", "address": 130},
        },
        {
            "source": {"device_id": DEVICE_ID, "point_id": "flag_b"},
            "address": {"slave_id": SLAVE, "register_type": "coil", "address": 10},
        },
    ]
    if overrides:
        points = overrides
    return {
        "modbus_sink": {
            "type": "modbus",
            "connection": {"host": "127.0.0.1", "port": listen_port},
            "outputs": {TASK_ID: {"points": points}},
        }
    }


def _write_config(
    base: Path,
    nats_url: str,
    listen_port: int,
    *,
    modbus_port: int | None = None,
    overrides: dict[str, Any] | None = None,
) -> Path:
    device_port = modbus_port if modbus_port is not None else free_port()
    return write_config_tree(
        base,
        devices={
            DEVICE_ID: {
                "protocol": "modbus",
                "point_table": "modbus",
                "endpoint": {"host": "127.0.0.1", "port": device_port},
                "protocol_options": {"slave_id": SLAVE, "timeout": 2.0},
            }
        },
        point_tables={"modbus": {"points": {k: dict(v) for k, v in POINTS.items()}}},
        sinks=_modbus_sink(listen_port, overrides=overrides),
        tasks={
            TASK_ID: {"device_group_id": DEVICE_ID, "point_group": "telemetry", "interval": 0.2}
        },
        system={
            "runtime": {
                "connect_timeout": 2.0,
                "read_timeout": 2.0,
                "write_timeout": 2.0,
                "shutdown_timeout": 5.0,
                "watchdog_interval": 0.5,
                "health_timeout": 0.5,
            },
            "messaging": {
                "backend": "nats",
                "servers": [nats_url],
                "delivery": "realtime",
                "max_staleness_ms": 60_000,
            },
        },
    )


async def _client(port: int):
    from pymodbus.client import AsyncModbusTcpClient

    client = AsyncModbusTcpClient("127.0.0.1", port=port, timeout=3.0)
    await client.connect()
    return client


def _decode_words(words: list[int], fmt: str, *, byte_order: str, word_order: str):
    """与生产 encode_modbus_value 严格互逆的解码（pymodbus 3.15 已移除 payload 模块）。"""
    import struct

    raw_words = [w.to_bytes(2, "big") for w in words]
    if byte_order == "little":
        raw_words = [w[::-1] for w in raw_words]
    if word_order == "little":
        raw_words.reverse()
    return struct.unpack(">" + fmt, b"".join(raw_words))[0]


async def test_register_encoding_16_32_64_and_word_orders(
    r5_sink_factory, r5_stack, tmp_path: Path
) -> None:
    listen = free_port()
    config_dir = _write_config(tmp_path / "cfg", r5_stack.nats, listen)
    sink = await r5_sink_factory(config_dir, realtime_sinks=("modbus_sink",))
    await wait_state(sink, "modbus_sink", "running")

    values = {
        "speed_f32": 3.14159,
        "power_u64": 1_234_567_890_123,
        "count_i32": -12345,
        "temp_i16": -42,
        "flag_b": True,
    }
    for point, value in values.items():
        await publish(r5_stack, make_sample(point, value, sample_id=f"r5m-{point}"), "realtime")

    client = await _client(listen)
    try:

        async def _populated() -> bool | None:
            # 稀疏地址空间：只能按已声明区间读。
            rr = await client.read_holding_registers(100, count=2, device_id=SLAVE)
            if rr.isError() or not any(rr.registers):
                return None
            return True

        await wait_until(_populated, timeout=20.0, interval=0.3, description="registers populated")

        rr_speed = await client.read_holding_registers(100, count=2, device_id=SLAVE)
        rr_power = await client.read_holding_registers(110, count=4, device_id=SLAVE)
        rr_count = await client.read_holding_registers(120, count=2, device_id=SLAVE)
        rr_temp = await client.read_holding_registers(130, count=1, device_id=SLAVE)
        assert not rr_speed.isError() and not rr_power.isError()
        assert not rr_count.isError() and not rr_temp.isError()
        speed = _decode_words(list(rr_speed.registers), "f", byte_order="big", word_order="big")
        power = _decode_words(
            list(rr_power.registers), "Q", byte_order="little", word_order="little"
        )
        count = _decode_words(list(rr_count.registers), "i", byte_order="big", word_order="little")
        temp = rr_temp.registers[0]
        if temp >= 0x8000:
            temp -= 0x10000

        assert speed == pytest.approx(3.14159, abs=1e-6)
        assert power == 1_234_567_890_123
        assert count == -12345
        assert temp == -42

        cr = await client.read_coils(10, count=1, device_id=SLAVE)
        assert not cr.isError()
        assert bool(cr.bits[0]) is True
    finally:
        client.close()


async def test_master_writes_are_rejected_illegal_function(
    r5_sink_factory, r5_stack, tmp_path: Path
) -> None:
    listen = free_port()
    config_dir = _write_config(tmp_path / "cfg", r5_stack.nats, listen)
    sink = await r5_sink_factory(config_dir, realtime_sinks=("modbus_sink",))
    await wait_state(sink, "modbus_sink", "running")
    client = await _client(listen)
    try:
        wr = await client.write_register(100, 0x1234, device_id=SLAVE)
        assert wr.isError()
        wc = await client.write_coil(10, True, device_id=SLAVE)
        assert wc.isError()
    finally:
        client.close()


def _assemble(config_dir: Path, stack, deployment: str) -> None:
    from sink_service.assembly import assemble_sinks

    assemble_sinks(
        config_dir,
        servers=(stack.nats,),
        delivery="realtime",
        wind_farm_id="test",
        deployment_id=deployment,
    )


async def test_invalid_and_overlapping_config_rejected_at_assembly(
    r5_stack, tmp_path: Path
) -> None:
    # 重叠寄存器：float32@100 占 100-101，int16@101 冲突——解析层拒绝。
    overlap = [
        {
            "source": {"device_id": DEVICE_ID, "point_id": "speed_f32"},
            "datatype": "float32",
            "address": {"slave_id": 1, "register_type": "holding", "address": 100},
        },
        {
            "source": {"device_id": DEVICE_ID, "point_id": "temp_i16"},
            "datatype": "int16",
            "address": {"slave_id": 1, "register_type": "holding", "address": 101},
        },
    ]
    cfg = _write_config(tmp_path / "overlap", r5_stack.nats, free_port(), overrides=overlap)
    with pytest.raises(ConfigError, match="[Oo]verlap"):
        _assemble(cfg, r5_stack, "r5-modbus-overlap")

    # slave_id 超出 0-255：契约校验拒绝。
    bad_slave = [
        {
            "source": {"device_id": DEVICE_ID, "point_id": "temp_i16"},
            "datatype": "int16",
            "address": {"slave_id": 300, "register_type": "holding", "address": 100},
        },
    ]
    cfg = _write_config(tmp_path / "badslave", r5_stack.nats, free_port(), overrides=bad_slave)
    # parse_domain 将 pydantic ValidationError 统一包装为 ConfigError。
    with pytest.raises(ConfigError, match="less than or equal to 255"):
        _assemble(cfg, r5_stack, "r5-modbus-badslave")


async def test_listen_port_conflict_fails_then_supervisor_recovers(
    r5_sink_factory, r5_stack, tmp_path: Path
) -> None:
    listen = free_port()
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    blocker.bind(("127.0.0.1", listen))
    blocker.listen(1)
    config_dir = _write_config(tmp_path / "cfg", r5_stack.nats, listen)
    try:
        sink = await r5_sink_factory(config_dir, realtime_sinks=("modbus_sink",))

        async def _failed() -> dict[str, Any] | None:
            status = await mgmt(sink, "GetSinkStatus", "modbus_sink")
            if status["state"] == "failed":
                return status
            return None

        await wait_until(_failed, timeout=15.0, interval=0.5, description="bind conflict failed")
    finally:
        blocker.close()

    # 端口释放后 supervisor 每 5s 重启，Modbus Server 重新监听并继续接收更新。
    await wait_state(sink, "modbus_sink", "running", timeout=60.0)
    await publish(r5_stack, make_sample("temp_i16", 7, sample_id="r5m-afterbind"), "realtime")
    client = await _client(listen)
    try:

        async def _value() -> int | None:
            rr = await client.read_holding_registers(130, count=1, device_id=SLAVE)
            if not rr.isError() and rr.registers and rr.registers[0] == 7:
                return int(rr.registers[0])
            return None

        await wait_until(_value, timeout=20.0, interval=0.3, description="post-bind value")
    finally:
        client.close()


async def test_process_restart_resumes_register_serving(
    r5_sink_factory, r5_stack, tmp_path: Path
) -> None:
    import asyncio

    listen = free_port()
    config_dir = _write_config(tmp_path / "cfg", r5_stack.nats, listen)
    sink_a = await r5_sink_factory(config_dir, realtime_sinks=("modbus_sink",))
    await wait_state(sink_a, "modbus_sink", "running")
    await publish(r5_stack, make_sample("temp_i16", 11, sample_id="r5m-restart-a"), "realtime")
    client = await _client(listen)
    try:

        async def _v(expected: int) -> int | None:
            rr = await client.read_holding_registers(130, count=1, device_id=SLAVE)
            if not rr.isError() and rr.registers and rr.registers[0] == expected:
                return expected
            return None

        await wait_until(lambda: _v(11), timeout=15.0, interval=0.3)
    finally:
        client.close()

    sink_a.terminate()
    await asyncio.sleep(1.0)
    assert not sink_a.is_running()

    # 新进程同配置/同端口重新监听，内存寄存器清空，新样本重新可读。
    sink_b = await r5_sink_factory(config_dir, realtime_sinks=("modbus_sink",))
    await wait_state(sink_b, "modbus_sink", "running")
    await publish(r5_stack, make_sample("temp_i16", 22, sample_id="r5m-restart-b"), "realtime")
    client = await _client(listen)
    try:

        async def _v22() -> int | None:
            rr = await client.read_holding_registers(130, count=1, device_id=SLAVE)
            if not rr.isError() and rr.registers and rr.registers[0] == 22:
                return 22
            return None

        await wait_until(_v22, timeout=15.0, interval=0.3)
    finally:
        client.close()
