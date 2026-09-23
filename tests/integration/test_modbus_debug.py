"""modbus_debug 集成测试 — localhost 真实 Modbus server。

只访问 127.0.0.1 上的 :class:`ModbusMockServer`，绝不访问现场 IP。
覆盖：read all / read one / scale 换算 / int16 写 / int32 写 /
input 点禁止写 / 无 --confirm 禁止写 / raw-read / address-check。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from modbus_debug.cli import (
    app,
    check_address_candidates,
    raw_read_registers,
    read_device,
    write_device,
)
from tests.fixtures.servers.modbus_server import ModbusMockServer
from wind_hub.config.loader import load_config
from wind_hub.config.schema import Config, DeviceConfig
from wind_hub.domain.model.device import Endpoint
from wind_hub.domain.model.point import Quality

SITE_DIR = Path(__file__).resolve().parents[2] / "configs" / "site_wtg_modbus"
SERVER_PORT = 15202

# 原始寄存器值（mock server 预置）与期望工程值
_RAW_ACTIVE_POWER = 123456  # 0x0001E240 → 工程值 1234.56 kW
_RAW_REACTIVE_POWER = -500  # → 工程值 -5.0 kVar
_RAW_ACTIVE_POWER_1MIN = 100000  # → 工程值 1000.0 kW
_RAW_WIND_SPEED = 850  # → 工程值 8.5 m/s
_RAW_GENERATOR_SPEED = 150055  # 0x00024A27 → 工程值 1500.55 rpm
_RAW_PITCH_ANGLE = 123  # → 工程值 1.23 deg
_RAW_FAULT_CODE = 42
_RAW_TURBINE_STATUS = 7


def _site_input_registers() -> list[int]:
    """按 site_wtg_modbus 点表布局构造输入寄存器块（values[addr] == wire address）。"""
    values = [0] * 1024

    def put_i32(addr: int, raw: int) -> None:
        values[addr] = (raw >> 16) & 0xFFFF  # big_endian：低地址放高字
        values[addr + 1] = raw & 0xFFFF

    values[0] = _RAW_TURBINE_STATUS
    values[19] = _RAW_FAULT_CODE
    put_i32(104, _RAW_GENERATOR_SPEED)
    put_i32(178, _RAW_ACTIVE_POWER)
    put_i32(180, _RAW_REACTIVE_POWER)
    put_i32(204, _RAW_ACTIVE_POWER_1MIN)
    values[357] = _RAW_WIND_SPEED
    put_i32(859, _RAW_PITCH_ANGLE)
    return values


@pytest.fixture
def site_server_thread() -> Iterator[None]:
    """在后台线程的独立事件循环上运行 site 布局 mock server。

    CLI 级测试是同步函数（CliRunner 内部 ``asyncio.run`` 不能嵌套在
    运行中的事件循环里），无法用 pytest-asyncio 的 async fixture——
    server 必须在另一个线程的事件循环上 serve。
    """
    import asyncio
    import threading

    loop = asyncio.new_event_loop()
    server = ModbusMockServer(
        port=SERVER_PORT,
        holding=[0] * 2048,
        inputs=_site_input_registers(),
    )
    ready = threading.Event()

    async def _serve() -> None:
        await server.start()
        ready.set()
        await asyncio.Event().wait()  # serve 直到线程结束

    def _run() -> None:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(_serve())

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    assert ready.wait(timeout=10), "site mock server 启动超时"
    try:
        yield
    finally:
        # 在线程自己的 loop 上优雅 shutdown 后停 loop。
        stop_done = threading.Event()

        async def _stop() -> None:
            await server.stop()
            stop_done.set()

        loop.call_soon_threadsafe(lambda: asyncio.ensure_future(_stop()))
        assert stop_done.wait(timeout=10), "site mock server 停止超时"
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=10)
        loop.close()


def _local_config_copy(tmp_path: Path) -> Path:
    """把 site 配置拷到临时目录，并将 wtg-002 endpoint 指向 localhost mock server。

    供 CLI 级测试使用——CLI 只接受 --config 目录，不能注入 DeviceConfig。
    """
    import shutil

    dst = tmp_path / "site_local"
    shutil.copytree(SITE_DIR, dst)
    devices = dst / "devices.yaml"
    text = devices.read_text(encoding="utf-8")
    text = text.replace("host: 192.168.100.102", "host: 127.0.0.1").replace(
        "port: 502", f"port: {SERVER_PORT}", 1
    )
    devices.write_text(text, encoding="utf-8")
    return dst


@pytest.fixture
async def site_server() -> AsyncIterator[ModbusMockServer]:
    server = ModbusMockServer(
        port=SERVER_PORT,
        holding=[0] * 2048,
        inputs=_site_input_registers(),
    )
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


@pytest.fixture(scope="module")
def site_config() -> Config:
    return load_config(SITE_DIR)


@pytest.fixture
def local_device(site_config: Config) -> DeviceConfig:
    """wtg-002 配置副本——endpoint 指向 localhost mock server。"""
    dev = next(d for d in site_config.devices.devices if d.device_id == "wtg-002")
    return dev.model_copy(
        update={
            "endpoint": Endpoint(
                host="127.0.0.1",
                port=SERVER_PORT,
                extensions={"unit_id": 1, "timeout": 3.0, "byte_order": "big_endian"},
            )
        }
    )


class TestRead:
    async def test_read_all(
        self, site_config: Config, local_device: DeviceConfig, site_server: ModbusMockServer
    ) -> None:
        values, points = await read_device(site_config, local_device, None)
        by_id = {v.point_id: v for v in values}
        # all 组 8 个采集点，不含控制点
        assert set(by_id) == {
            "active_power",
            "active_power_1min",
            "reactive_power",
            "wind_speed",
            "generator_speed",
            "pitch_angle",
            "fault_code",
            "turbine_status",
        }
        assert all(v.quality == Quality.GOOD for v in values)
        assert by_id["active_power"].value == pytest.approx(1234.56)
        assert by_id["reactive_power"].value == pytest.approx(-5.0)
        assert by_id["wind_speed"].value == pytest.approx(8.5)
        assert by_id["fault_code"].value == _RAW_FAULT_CODE
        assert by_id["turbine_status"].value == _RAW_TURBINE_STATUS
        # 点表随结果返回（variable_name/unit 供展示）
        assert {p.point_id for p in points} >= set(by_id)

    async def test_read_one_with_scale(
        self, site_config: Config, local_device: DeviceConfig, site_server: ModbusMockServer
    ) -> None:
        values, _ = await read_device(site_config, local_device, "generator_speed")
        assert len(values) == 1
        assert values[0].point_id == "generator_speed"
        # scale=0.01：raw 150055 → 1500.55（经 Device，与正式采集一致）
        assert values[0].value == pytest.approx(1500.55)
        assert values[0].quality == Quality.GOOD


class TestWrite:
    async def test_write_int16(
        self, site_config: Config, local_device: DeviceConfig, site_server: ModbusMockServer
    ) -> None:
        result = await write_device(site_config, local_device, "turbine_command", 3)
        assert result.success is True
        # Server 侧确认：单寄存器写到达 2005
        assert await site_server.read_holding(1, 2005) == [3]

    async def test_write_int32(
        self, site_config: Config, local_device: DeviceConfig, site_server: ModbusMockServer
    ) -> None:
        result = await write_device(site_config, local_device, "active_power_setpoint", 1000)
        assert result.success is True
        # int32 占 2 寄存器，big_endian：1000 = 0x000003E8 → [0x0000, 0x03E8]
        assert await site_server.read_holding(1, 2001, count=2) == [0x0000, 0x03E8]

    def test_write_input_point_rejected(self) -> None:
        runner = CliRunner()
        result = runner.invoke(
            app,
            [
                "write",
                "--config",
                str(SITE_DIR),
                "--device",
                "wtg-002",
                "--point",
                "active_power",  # input register — 只读
                "--value",
                "1",
                "--confirm",
            ],
        )
        assert result.exit_code == 1
        assert "只读" in result.output

    def test_write_without_confirm_rejected(self) -> None:
        runner = CliRunner()
        result = runner.invoke(
            app,
            [
                "write",
                "--config",
                str(SITE_DIR),
                "--device",
                "wtg-002",
                "--point",
                "turbine_command",
                "--value",
                "1",
            ],
        )
        assert result.exit_code == 1
        assert "--confirm" in result.output


class TestRawDiagnostics:
    async def test_raw_read(
        self, local_device: DeviceConfig, site_server: ModbusMockServer
    ) -> None:
        registers = await raw_read_registers(local_device, "input", 178, 2)
        assert [r.address for r in registers] == [178, 179]
        # big_endian 高字在前：0x0001, 0xE240
        assert [r.value for r in registers] == [0x0001, 0xE240]

    async def test_raw_read_word_order_candidates(
        self, local_device: DeviceConfig, site_server: ModbusMockServer
    ) -> None:
        registers = await raw_read_registers(local_device, "input", 178, 2)
        import struct

        w0, w1 = registers[0].value, registers[1].value
        assert struct.unpack(">i", struct.pack(">HH", w0, w1))[0] == _RAW_ACTIVE_POWER
        # little_endian（低地址放低字）应得到另一个值——供现场判断 word order
        le = struct.unpack(">i", struct.pack(">HH", w1, w0))[0]
        assert le != _RAW_ACTIVE_POWER

    async def test_address_check_count1(
        self, local_device: DeviceConfig, site_server: ModbusMockServer
    ) -> None:
        # count=1：address-1 / address / address+1 三次独立读取
        candidates = await check_address_candidates(local_device, "input", 357, 1)
        assert [c.start for c in candidates] == [356, 357, 358]
        assert all(c.error is None for c in candidates)
        assert all(len(c.registers) == 1 for c in candidates)
        assert candidates[1].registers[0].value == _RAW_WIND_SPEED

    async def test_address_check_candidate_failure_isolated(
        self, local_device: DeviceConfig, site_server: ModbusMockServer
    ) -> None:
        # address=1023：候选 1024 超出 mock server 输入块（1024 寄存器），
        # 该候选 ERROR；1022 / 1023 仍正常执行，整体不中断。
        candidates = await check_address_candidates(local_device, "input", 1023, 1)
        assert [c.start for c in candidates] == [1022, 1023, 1024]
        assert candidates[0].error is None and len(candidates[0].registers) == 1
        assert candidates[1].error is None and len(candidates[1].registers) == 1
        assert candidates[2].error is not None
        assert candidates[2].registers == ()

    async def test_address_check_count2(
        self, local_device: DeviceConfig, site_server: ModbusMockServer
    ) -> None:
        # count=2：分别读取 177~178 / 178~179 / 179~180 三个窗口
        candidates = await check_address_candidates(local_device, "input", 178, 2)
        assert [c.start for c in candidates] == [177, 178, 179]
        assert all(c.error is None for c in candidates)
        assert [[r.address for r in c.registers] for c in candidates] == [
            [177, 178],
            [178, 179],
            [179, 180],
        ]
        # start=178 窗口即 S32 原始值（big_endian 高字在前）
        assert [r.value for r in candidates[1].registers] == [0x0001, 0xE240]

    def test_address_check_count2_cli_outputs_word_order_candidates(
        self, site_server_thread: None, tmp_path: Path
    ) -> None:
        config_dir = _local_config_copy(tmp_path)
        runner = CliRunner()
        result = runner.invoke(
            app,
            [
                "address-check",
                "--config",
                str(config_dir),
                "--device",
                "wtg-002",
                "--type",
                "input",
                "--address",
                "178",
                "--count",
                "2",
            ],
        )
        assert result.exit_code == 0
        assert "candidate start=177" in result.output
        assert "candidate start=178" in result.output
        assert "candidate start=179" in result.output
        assert "int32 big_endian" in result.output
        assert "int32 little_endian" in result.output
        assert "uint32 big_endian" in result.output
        assert "uint32 little_endian" in result.output
        assert f"int32 big_endian: {_RAW_ACTIVE_POWER}" in result.output

    def test_address_check_cli_partial_failure(
        self, site_server_thread: None, tmp_path: Path
    ) -> None:
        # CLI 整体正常输出三项结果，失败候选显示 ERROR
        config_dir = _local_config_copy(tmp_path)
        runner = CliRunner()
        result = runner.invoke(
            app,
            [
                "address-check",
                "--config",
                str(config_dir),
                "--device",
                "wtg-002",
                "--type",
                "input",
                "--address",
                "1023",
            ],
        )
        assert result.exit_code == 0
        assert "candidate start=1022:  OK" in result.output
        assert "candidate start=1023:  OK" in result.output
        assert "candidate start=1024:  ERROR" in result.output

    def test_address_check_invalid_params_rejected(self) -> None:
        runner = CliRunner()
        for extra in (["--address", "0"], ["--address", "178", "--count", "0"]):
            result = runner.invoke(
                app,
                [
                    "address-check",
                    "--config",
                    str(SITE_DIR),
                    "--device",
                    "wtg-002",
                    "--type",
                    "input",
                    *extra,
                ],
            )
            assert result.exit_code == 1, extra
