"""modbus_debug 集成测试 — localhost 真实 Modbus server。

只访问 127.0.0.1 上的 :class:`ModbusMockServer`，绝不访问现场 IP。
覆盖：read-all（多机并发 / 单机失败隔离）/ watch（连续读取、取消后关闭连接）/
raw（int16 原始输出、int32 四种字序候选、配置解析结果）。

site_wtg_modbus 设备统一 ``word_order: little_endian``（低地址寄存器 = 低 16 位），
mock server 寄存器布局与之对应。
"""

from __future__ import annotations

import asyncio
import shutil
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from modbus_debug.cli import app, watch_loop
from tests.fixtures.servers.modbus_server import ModbusMockServer
from wind_hub.config.loader import load_config
from wind_hub.config.schema import Config, DeviceConfig
from wind_hub.domain.model.device import Endpoint

SITE_DIR = Path(__file__).resolve().parents[2] / "configs" / "site_wtg_modbus"
PORT_A = 15202
PORT_B = 15203
PORT_DEAD = 15299  # 无 server 监听——连接即失败

# 原始寄存器值（mock server 预置）——输出只含原始解码值，不乘 scale
_RAW_ACTIVE_POWER = 123456
_RAW_REACTIVE_POWER = -500
_RAW_WIND_SPEED = 850
_RAW_FAULT_CODE = 42
_RAW_TURBINE_STATUS = 7


def _site_input_registers() -> list[int]:
    """按 site_wtg_modbus 点表布局构造输入寄存器块（little_endian）。"""
    values = [0] * 1024

    def put_i32(addr: int, raw: int) -> None:
        values[addr] = raw & 0xFFFF  # little_endian：低地址放低 16 位
        values[addr + 1] = (raw >> 16) & 0xFFFF

    values[0] = _RAW_TURBINE_STATUS
    values[19] = _RAW_FAULT_CODE
    put_i32(178, _RAW_ACTIVE_POWER)
    put_i32(180, _RAW_REACTIVE_POWER)
    values[357] = _RAW_WIND_SPEED
    return values


@contextmanager
def _servers_in_thread(*servers: ModbusMockServer) -> Iterator[None]:
    """在后台线程的独立事件循环上运行多个 mock server。

    CLI 级测试是同步函数（CliRunner 内部 ``asyncio.run`` 不能嵌套在运行中的
    事件循环里），server 必须在另一个线程的事件循环上 serve。
    """
    loop = asyncio.new_event_loop()
    ready = threading.Event()

    async def _serve() -> None:
        for s in servers:
            await s.start()
        ready.set()
        await asyncio.Event().wait()  # serve 直到线程结束

    def _run() -> None:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(_serve())

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    assert ready.wait(timeout=10), "mock server 启动超时"
    try:
        yield
    finally:
        stop_done = threading.Event()

        async def _stop() -> None:
            for s in servers:
                await s.stop()
            stop_done.set()

        loop.call_soon_threadsafe(lambda: asyncio.ensure_future(_stop()))
        assert stop_done.wait(timeout=10), "mock server 停止超时"
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=10)
        loop.close()


def _local_config_copy(tmp_path: Path) -> Path:
    """把 site + common 配置树拷到临时目录，三台设备分别指向两个 live
    server 与一个死端口。返回 site 配置目录。"""
    dst = tmp_path / "cfg"
    site = shutil.copytree(SITE_DIR, dst / "site")
    shutil.copytree(SITE_DIR.parent / "common", dst / "common")
    raw = yaml.safe_load((site / "devices.yaml").read_text(encoding="utf-8"))
    # 现场配置为两台机组；第三台（死端口）由测试补齐，用于失败隔离用例。
    devices = raw["devices"][:2]
    devices.append(
        {
            "device_id": "wtg-004",
            "model": devices[0]["model"],
            "device_group": devices[0].get("device_group"),
            "enabled": True,
            "endpoint": {"host": "127.0.0.1"},
        }
    )
    devices[0]["endpoint"]["host"] = "127.0.0.1"
    devices[0]["endpoint"]["port"] = PORT_A
    devices[1]["endpoint"]["host"] = "127.0.0.1"
    devices[1]["endpoint"]["port"] = PORT_B
    devices[2]["endpoint"]["host"] = "127.0.0.1"
    devices[2]["endpoint"]["port"] = PORT_DEAD
    (site / "devices.yaml").write_text(
        yaml.safe_dump({"devices": devices}, allow_unicode=True), encoding="utf-8"
    )
    return site


@pytest.fixture(scope="module")
def site_config() -> Config:
    return load_config(SITE_DIR)


def _local_device(site_config: Config, device_id: str, port: int) -> DeviceConfig:
    """设备配置副本——endpoint 指向 localhost mock server。"""
    dev = next(d for d in site_config.devices.devices if d.device_id == device_id)
    return dev.model_copy(
        update={
            "endpoint": Endpoint(
                host="127.0.0.1",
                port=port,
                extensions={"unit_id": 1, "timeout": 3.0, "word_order": "little_endian"},
            )
        }
    )


class TestReadAll:
    def test_read_all_multiple_devices(self, tmp_path: Path) -> None:
        config_dir = _local_config_copy(tmp_path)
        server_a = ModbusMockServer(port=PORT_A, inputs=_site_input_registers())
        server_b = ModbusMockServer(port=PORT_B, inputs=_site_input_registers())
        with _servers_in_thread(server_a, server_b):
            result = CliRunner().invoke(app, ["read-all", "--config", str(config_dir)])
        assert result.exit_code == 0, result.output
        assert "wtg-002  127.0.0.1  OK" in result.output
        assert "wtg-003  127.0.0.1  OK" in result.output
        assert "active_power" in result.output
        assert "123456" in result.output
        assert "850" in result.output

    def test_read_all_single_failure_isolated(self, tmp_path: Path) -> None:
        """wtg-004 指向死端口——ERROR 不影响 wtg-002/wtg-003 正常输出。"""
        config_dir = _local_config_copy(tmp_path)
        server_a = ModbusMockServer(port=PORT_A, inputs=_site_input_registers())
        server_b = ModbusMockServer(port=PORT_B, inputs=_site_input_registers())
        with _servers_in_thread(server_a, server_b):
            result = CliRunner().invoke(app, ["read-all", "--config", str(config_dir)])
        assert result.exit_code == 0, result.output
        assert "wtg-002  127.0.0.1  OK" in result.output
        assert "wtg-003  127.0.0.1  OK" in result.output
        assert "wtg-004  127.0.0.1  ERROR" in result.output


class TestWatch:
    async def test_watch_reads_repeatedly_and_closes_on_cancel(
        self, site_config: Config, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        device = _local_device(site_config, "wtg-002", PORT_A)
        points = [p for p in site_config.points_for_device("wtg-002") if "all" in p.point_groups]

        server = ModbusMockServer(port=PORT_A, inputs=_site_input_registers())
        await server.start()
        clients: list[object] = []

        import modbus_debug.cli as cli_module

        real_connect = cli_module.connect_device

        async def _recording_connect(dev: DeviceConfig) -> object:
            client = await real_connect(dev)
            clients.append(client)
            return client

        monkeypatch.setattr(cli_module, "connect_device", _recording_connect)

        frames: list[str] = []
        try:
            task = asyncio.create_task(watch_loop(device, points, interval=0.05, render=frames.append))
            await asyncio.sleep(0.3)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            await server.stop()

        # 连续读取多帧，内容含设备信息与原始值；每帧都是完整 frame
        assert len(frames) >= 2
        for frame in frames:
            assert frame.startswith("device: wtg-002")
            assert frame.endswith("Ctrl+C 退出")
        assert "123456" in frames[-1]
        # 只建连一次（连接复用），取消后连接已关闭
        assert len(clients) == 1
        assert clients[0].connected is False  # type: ignore[attr-defined]

    def test_format_watch_is_plain_text(self, site_config: Config) -> None:
        """format_watch 只生成纯文本，不含 ANSI 转义字符。"""
        from modbus_debug.cli import format_watch

        device = _local_device(site_config, "wtg-002", PORT_A)
        points = site_config.points_for_device("wtg-002")
        frame = format_watch(device, [(p, 123) for p in points])
        assert "\033[" not in frame
        err_frame = format_watch(device, error=TimeoutError("timeout"))
        assert "\033[" not in err_frame

    async def test_watch_error_renders_complete_error_frame(
        self, site_config: Config, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """单次读取异常时渲染完整错误帧（含 device 头部），而不是追加错误行。"""
        import modbus_debug.cli as cli_module

        device = _local_device(site_config, "wtg-002", PORT_A)
        points = [p for p in site_config.points_for_device("wtg-002") if "all" in p.point_groups]

        server = ModbusMockServer(port=PORT_A, inputs=_site_input_registers())
        await server.start()

        real_read_point = cli_module.read_point
        state = {"fail": False}

        async def _maybe_fail(client: object, dev: DeviceConfig, point: object) -> object:
            if state["fail"]:
                raise TimeoutError("modbus timeout")
            return await real_read_point(client, dev, point)  # type: ignore[arg-type]

        monkeypatch.setattr(cli_module, "read_point", _maybe_fail)

        frames: list[str] = []
        try:
            task = asyncio.create_task(watch_loop(device, points, interval=0.05, render=frames.append))
            await asyncio.sleep(0.15)
            state["fail"] = True  # 后续每轮读取失败
            await asyncio.sleep(0.15)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            await server.stop()

        # 前段为正常帧，后段为完整错误帧；错误帧同样是整帧（以 device 头开头）
        assert any("123456" in f for f in frames)
        error_frames = [f for f in frames if "读取失败" in f]
        assert len(error_frames) >= 2
        for f in error_frames:
            assert f.startswith("device: wtg-002")
            assert "正在继续重试" in f
            assert "modbus timeout" in f


class TestRaw:
    def test_raw_int16(self, tmp_path: Path) -> None:
        config_dir = _local_config_copy(tmp_path)
        server = ModbusMockServer(port=PORT_A, inputs=_site_input_registers())
        with _servers_in_thread(server):
            result = CliRunner().invoke(
                app,
                ["raw", "--config", str(config_dir), "--device", "wtg-002", "--point", "wind_speed"],
            )
        assert result.exit_code == 0, result.output
        assert "type=input  address=357  count=1" in result.output
        assert f"decimal={_RAW_WIND_SPEED}" in result.output
        assert "hex=0x0352" in result.output
        # 单寄存器不输出字序候选
        assert "候选" not in result.output
        assert "配置 word_order: little_endian" in result.output
        assert "配置解析结果: 850" in result.output

    def test_raw_int32_word_order_candidates(self, tmp_path: Path) -> None:
        config_dir = _local_config_copy(tmp_path)
        server = ModbusMockServer(port=PORT_A, inputs=_site_input_registers())
        with _servers_in_thread(server):
            result = CliRunner().invoke(
                app,
                [
                    "raw",
                    "--config",
                    str(config_dir),
                    "--device",
                    "wtg-002",
                    "--point",
                    "active_power",
                ],
            )
        assert result.exit_code == 0, result.output
        assert "type=input  address=178  count=2" in result.output
        # little_endian：低地址 178 放低字 0xE240，179 放高字 0x0001
        assert "address=178  decimal=57920  hex=0xe240" in result.output
        assert "address=179  decimal=1  hex=0x0001" in result.output
        # 四种候选都输出
        assert "候选 int32 little_endian: 123456" in result.output
        assert "候选 uint32 big_endian:" in result.output
        assert "候选 uint32 little_endian: 123456" in result.output
        assert "候选 int32 big_endian:" in result.output
        # 配置解析结果（little_endian 原始解码，不乘 scale）
        assert "配置 word_order: little_endian" in result.output
        assert "配置解析结果: 123456" in result.output
