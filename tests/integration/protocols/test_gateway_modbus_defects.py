"""industrial_gateway 生产缺陷的真实链路复现（基线 69e3aaf1）。

本文件的测试断言**修复后应有的正确行为**，在基线上全部失败，失败本身即
缺陷证据；修复生产代码后这些测试应转为通过。禁止对这些测试 skip/xfail
掩盖（见 ai_shared/rules/testing.md 第 5 节）。

缺陷索引（精确位置见各测试 docstring）：
- D1 出厂配置启动即崩：空 bit 块 INVALID 占位违反 pymodbus 3.15 约束；
- D2 所有 TCP 连接被 reset：allow_multiple_devices 触发 socket framer
  不存在的 setMultidrop；
- D3 FC05/FC06 单写响应路径：pymodbus 写后以写功能码回读，action 对
  set_values=None 一律拒绝，客户端收到异常，但副作用（反馈镜像/Commander
  RPC）已经提交——控制语义上最危险；
- D4 同一 TCP 段流水线第二事务无响应（pymodbus 剩余字节不即时重处理）；
- D5 FC15 多线圈批量写被错误拒绝（寄存器数与 bit 数混用）。

D1 不安装任何垫片；D2 仅安装 SimDevice 垫片并显式关闭 multidrop 垫片；
其余安装全部垫片。
"""

from __future__ import annotations

import asyncio
import os
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from core.infrastructure.rpc import commander_pb2 as pb  # noqa: F401  # 三态常量备用
from tests.support.gateway_harness import (
    COIL_FB_0,
    COLLECTOR_IP,
    COMMAND_UINT,
    COMMANDER_IP,
    FEEDBACK_FLOAT,
    FEEDBACK_UINT,
    FakeCommanderService,
    build_config,
    float_words,
    install_server_shims,
    reserve_port,
    start_gateway,
)

pytestmark = [
    pytest.mark.real_service,
    pytest.mark.modbus,
    pytest.mark.fast,
]

REPO_ROOT = Path(__file__).resolve().parents[3]


def _exc(response: object) -> int | None:
    if not response.isError():
        return None
    code = response.exception_code
    return int(code.value) if hasattr(code, "value") else int(code)


def _frame(transaction: int, unit: int, pdu: bytes) -> bytes:
    return struct.pack(">HHHB", transaction, 0, len(pdu) + 1, unit) + pdu


async def _read_frames(reader: asyncio.StreamReader, count: int) -> list[tuple[int, int, bytes]]:
    buffer = b""
    frames: list[tuple[int, int, bytes]] = []
    while len(frames) < count:
        try:
            chunk = await asyncio.wait_for(reader.read(64), timeout=3)
        except TimeoutError:
            break
        if not chunk:
            break
        buffer += chunk
        while len(buffer) >= 8:
            length = struct.unpack(">H", buffer[4:6])[0]
            if len(buffer) < 6 + length:
                break
            tid, _proto, _len, unit = struct.unpack(">HHHB", buffer[:7])
            payload = buffer[7 : 6 + length]
            frames.append((tid, unit, payload))
            buffer = buffer[6 + length :]
    return frames


# --- D1 --------------------------------------------------------------------


async def test_d1_shipped_config_process_starts() -> None:
    """D1：``configs/industrial_gateway.yaml`` 进程必须能正常启动。

    实际：``src/industrial_gateway/infrastructure/protocol/modbus/server.py:77``
    对空 bit 块追加 ``SimData(address=0, datatype=DataType.INVALID)``，
    pymodbus 3.15.0 的 SimDevice 对 tuple blocks 强制 DataType.BITS，
    ``assembly.py:109`` 构造阶段抛 TypeError，进程退出。
    """
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "industrial_gateway.main",
        "--config",
        "configs/industrial_gateway.yaml",
        cwd=REPO_ROOT,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")},
    )
    try:
        await asyncio.sleep(2.0)
        assert proc.returncode is None, "Gateway 进程启动即退出:\n" + (
            await proc.stderr.read()
        ).decode(errors="replace")
    finally:
        if proc.returncode is None:
            proc.terminate()
            await proc.wait()


def test_d1_shipped_config_startup_traceback_reference() -> None:
    """保留基线证据：当前复现为 TypeError，修复后删除本测试。"""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "industrial_gateway.main",
            "--config",
            "configs/industrial_gateway.yaml",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")},
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "industrial gateway started" in result.stderr


# --- D2 --------------------------------------------------------------------


async def test_d2_tcp_connections_served_without_multidrop_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D2：生产代码 ``server.py:202`` 无条件 ``allow_multiple_devices=True``。

    pymodbus 3.15.0 的 ``ServerRequestHandler.__init__`` 随后调用
    ``framer.setMultidrop``，而 TCP socket framer 未实现该方法
    （仅 FramerRTU 有），每个 TCP 连接工厂抛 AttributeError，
    客户端表现为连接被 reset、永远收不到 Modbus 响应。
    本测试显式不安装 multidrop 垫片。
    """
    install_server_shims(monkeypatch, multidrop=False)
    commander = FakeCommanderService()
    config = build_config(reserve_port(), reserve_port(), tmp_path / "d2.json")
    runtime = await start_gateway(config, commander)
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", runtime.modbus_port, local_addr=(COLLECTOR_IP, 0)),
            timeout=3,
        )
        # 没有 multidrop 垫片时连接工厂会抛异常；尝试发送 FC03
        pdu = bytes([3]) + struct.pack(">HH", FEEDBACK_UINT, 1)
        writer.write(_frame(1, 1, pdu))
        await writer.drain()
        frames = await _read_frames(reader, 1)
        writer.close()
        assert frames, "客户端未收到任何响应（连接被 reset / 工厂异常）"
    finally:
        await runtime.stop()


# --- D3 --------------------------------------------------------------------


async def test_d3_fc06_feedback_echo_does_not_split_brain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D3：FC06 反馈写必须返回成功。

    实际：写副作用已提交（FC03 回读得到新值），但 pymodbus 在
    ``WriteSingleRegisterRequest.datastore_update`` 内写后以 FC06 回读，
    simulator 以 set_values=None 再次调用 action，
    ``server.py:117-120`` 对写功能码 + 空 set_values 一律返回
    ILLEGAL_ADDRESS，客户端收到异常码 2。复现：基线 100% 失败。
    """
    install_server_shims(monkeypatch)
    commander = FakeCommanderService()
    runtime = await start_gateway(
        build_config(reserve_port(), reserve_port(), tmp_path / "d3.json"),
        commander,
    )
    try:
        collector = await runtime.client(COLLECTOR_IP)
        try:
            result = await collector.write_register(FEEDBACK_UINT, 4242, device_id=1)
            code = _exc(result)
            result = await collector.read_holding_registers(FEEDBACK_UINT, count=1, device_id=1)
            committed = result.registers[0]
        finally:
            collector.close()
        # 分裂证据：镜像已提交新值，但写响应是异常
        assert committed == 4242
        assert code is None, "FC06 写已提交却向客户端返回异常"
    finally:
        await runtime.stop()


async def test_d3_fc06_command_rpc_must_match_client_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D3（控制安全）：FC06 命令写。

    现场 Commander RPC 已发出并成功（可能已执行），客户端却收到
    ILLEGAL_ADDRESS —— 上层会把它当失败重试，导致重复执行。
    正确行为：响应与 RPC 终局一致（成功）。
    """
    install_server_shims(monkeypatch)
    commander = FakeCommanderService()
    runtime = await start_gateway(
        build_config(reserve_port(), reserve_port(), tmp_path / "d3b.json"),
        commander,
    )
    try:
        dcems = await runtime.client(COMMANDER_IP)
        try:
            result = await dcems.write_register(COMMAND_UINT, 77, device_id=1)
            code = _exc(result)
        finally:
            dcems.close()
        # 分裂证据第一半：Commander RPC 已经发出并成功（现场可能已执行）
        assert len(commander.calls) == 1
        assert commander.calls[0].value == 77
        # 分裂证据第二半：客户端却收到拒绝，会把已执行命令当失败重试
        assert code is None, "Commander 已成功执行却向 DC-EMS 返回异常，会诱发重复命令"
    finally:
        await runtime.stop()


async def test_d3_fc05_coil_command_echo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """D3 线圈版：FC05 命令成功执行后，响应同样被回读路径拒绝。"""
    from tests.support.gateway_harness import COIL_CMD

    install_server_shims(monkeypatch)
    commander = FakeCommanderService()
    runtime = await start_gateway(
        build_config(reserve_port(), reserve_port(), tmp_path / "d3c.json"),
        commander,
    )
    try:
        dcems = await runtime.client(COMMANDER_IP)
        try:
            result = await dcems.write_coil(COIL_CMD, True, device_id=1)
            code = _exc(result)
        finally:
            dcems.close()
        assert any(c.point_id == "spb" for c in commander.calls)
        assert code is None
    finally:
        await runtime.stop()


# --- D4 --------------------------------------------------------------------


async def test_d4_pipelined_requests_both_answered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D4：同一 TCP 段携带两个事务时，两者都必须被应答。

    实际：pymodbus requesthandler 一次只解出最后 PDU，剩余字节滞留在
    recv_buffer，直到客户端再发数据才处理；第二事务永久挂起。
    生产代码未做任何缓解。
    """
    install_server_shims(monkeypatch)
    commander = FakeCommanderService()
    runtime = await start_gateway(
        build_config(reserve_port(), reserve_port(), tmp_path / "d4.json"),
        commander,
    )
    try:
        writer_client = await runtime.client(COLLECTOR_IP)
        await writer_client.write_registers(FEEDBACK_FLOAT, float_words(111.0), device_id=1)
        await writer_client.write_registers(FEEDBACK_FLOAT, float_words(222.0), device_id=2)
        writer_client.close()

        reader, writer = await asyncio.open_connection("127.0.0.1", runtime.modbus_port)
        try:
            pdu = bytes([3]) + struct.pack(">HH", FEEDBACK_FLOAT, 2)
            writer.write(_frame(0x1111, 1, pdu) + _frame(0x2222, 2, pdu))
            await writer.drain()
            frames = await _read_frames(reader, 2)
        finally:
            writer.close()
        assert len(frames) == 2, f"流水线第二事务丢失（{len(frames)}/2）"
    finally:
        await runtime.stop()


# --- D5 --------------------------------------------------------------------


async def test_d5_fc15_multi_coil_batch_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D5：FC15 一次写两个相邻反馈线圈应被接受。

    实际：``server.py:111-116`` 的相关映射判定把 ``count`` 当作 16-bit
    寄存器数（位访问恒为 1），而 FC15 的 set_values 是 bit 列表，
    第二个线圈因 ``mapping.address(1) < address(0)+count(1)`` 被判为
    不相关，末尾覆盖长度校验 1 != 2，返回 ILLEGAL_ADDRESS。
    """
    install_server_shims(monkeypatch)
    commander = FakeCommanderService()
    runtime = await start_gateway(
        build_config(reserve_port(), reserve_port(), tmp_path / "d5.json"),
        commander,
    )
    try:
        collector = await runtime.client(COLLECTOR_IP)
        try:
            result = await collector.write_coils(COIL_FB_0, [True, True], device_id=1)
            assert _exc(result) is None
        finally:
            collector.close()
    finally:
        await runtime.stop()
