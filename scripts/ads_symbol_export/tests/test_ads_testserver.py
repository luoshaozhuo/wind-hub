"""通过 pyads.testserver 的真实 ADS/TCP 通信验证符号枚举。

运行：python -m pytest tests/test_ads_testserver.py -v
需要本机可用的 pyads ADS 路由；不连接现场 PLC。
"""

from __future__ import annotations

import importlib.util
import struct
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pyads
import pytest
from pyads.testserver import AdsTestServer
from pyads.testserver.handler import AmsResponseData

# ==================== 测试配置 ====================
SERVER_IP = "127.0.0.1"
SERVER_AMS_NET_ID = "127.0.0.1.1.1"
SERVER_ADS_PORT = 801
SYMBOL_NAMES = ("MAIN.temperature", "MAIN.pressure", "GVL.状态")
# ==============================================

ADS_READ = 2
ADS_READ_STATE = 4
SYMBOL_INFO = 0xF00F
SYMBOL_DATA = 0xF00B
ADS_OK = 0
ADS_REJECT = 0x701


def _err_bytes(code: int) -> bytes:
    """AmsResponseData.error_code 需要 4 字节小端 bytes，而非 int。"""
    return struct.pack("<I", code)


def _load_exporter() -> Any:
    path = Path(__file__).resolve().parents[1] / "export.py"
    spec = importlib.util.spec_from_file_location("_ads_export_integration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _encode_symbol(name: str) -> bytes:
    """生成与 TwinCAT AdsSymbolEntry 格式一致的符号记录。"""
    encoded_name = name.encode("utf-8")
    type_name = b"INT"
    header = struct.pack(
        "<IIIIIIHHH",
        30 + len(encoded_name) + 1 + len(type_name) + 1 + 1,
        0x4020,
        0,
        2,
        2,
        0,
        len(encoded_name),
        len(type_name),
        0,
    )
    return header + encoded_name + b"\0" + type_name + b"\0\0"


class SymbolTableHandler:
    """可注入故障的 ADS Read 服务，仅模拟符号表相关命令。"""

    def __init__(self, names: tuple[str, ...], mode: str) -> None:
        self.symbols = b"".join(_encode_symbol(name) for name in names)
        self.count = len(names)
        self.mode = mode
        self.full_requests = 0
        self.data_reads: list[tuple[int, int]] = []

    def handle_request(self, request: Any) -> AmsResponseData:
        command = int.from_bytes(request.ams_header.command_id, "little")
        # error_code 必须为 4 字节小端 bytes，testserver 会原样拼进响应包。
        if command == ADS_READ_STATE:
            return AmsResponseData(
                b"\x05\x00", _err_bytes(ADS_OK), struct.pack("<IHH", ADS_OK, 5, 0)
            )
        if command != ADS_READ:
            return AmsResponseData(
                b"\x05\x00", _err_bytes(ADS_OK), struct.pack("<I", ADS_REJECT)
            )

        group, offset, size = struct.unpack_from("<III", request.ams_header.data)
        if group == SYMBOL_INFO:
            # 同时兼容仅需前 8 字节和更长的 upload-info 请求。
            info = struct.pack("<II", self.count, len(self.symbols))
            info += bytes(max(0, size - len(info)))
            return self._read_reply(info[:size])
        if group != SYMBOL_DATA:
            return self._error_reply()

        self.data_reads.append((offset, size))
        if offset == 0 and size >= len(self.symbols):
            self.full_requests += 1
            if self.mode in {"full_error", "reject_offset"}:
                return self._error_reply()

        if self.mode == "reject_offset" and offset > 0:
            return self._error_reply()
        if self.mode == "ignore_offset":
            offset = 0
        if offset + size > len(self.symbols):
            return self._error_reply()
        return self._read_reply(self.symbols[offset : offset + size])

    @staticmethod
    def _read_reply(payload: bytes) -> AmsResponseData:
        data = struct.pack("<II", ADS_OK, len(payload)) + payload
        return AmsResponseData(b"\x05\x00", _err_bytes(ADS_OK), data)

    @staticmethod
    def _error_reply() -> AmsResponseData:
        return AmsResponseData(
            b"\x05\x00", _err_bytes(ADS_OK), struct.pack("<II", ADS_REJECT, 0)
        )


@pytest.fixture
def server_and_connection() -> Iterator[tuple[SymbolTableHandler, pyads.Connection]]:
    # 由测试按需指定 mode，避免复用生产 PLC 配置。
    handler = SymbolTableHandler(SYMBOL_NAMES, "full_error")
    try:
        server = AdsTestServer(handler=handler, ip_address=SERVER_IP, logging=False)
    except OSError as exc:
        pytest.skip(f"本地 ADS 测试服务端口 48898 不可用：{exc}")

    try:
        # Linux 下 pyads 自带的 AdsLib 以 IP:48898 直连测试服务，
        # add_route 会因本地无 TwinCAT 路由服务而报 error 6，故不调用。
        server.start()
        connection = pyads.Connection(SERVER_AMS_NET_ID, SERVER_ADS_PORT, SERVER_IP)
        connection.open()
        connection.set_timeout(3000)
        yield handler, connection
    finally:
        if "connection" in locals():
            connection.close()
        server.close()
        if server.ident is not None:  # start() 失败时线程未启动，不能 join
            server.join(timeout=3)


def test_full_table_error_then_chunk_fallback(
    server_and_connection: tuple[SymbolTableHandler, pyads.Connection],
    tmp_path: Path,
) -> None:
    """整表请求返回 ADS 错误后，实际 ADS/TCP 分段请求成功。"""
    handler, plc = server_and_connection
    exporter = _load_exporter()
    exporter.TRY_FULL_TABLE_FIRST = True
    exporter.CHUNK_SIZES = (64,)
    output = tmp_path / "symbols.txt"

    result = exporter.export_symbols(plc, output)

    assert result == len(SYMBOL_NAMES)
    assert output.read_text(encoding="utf-8").splitlines() == list(SYMBOL_NAMES)
    assert handler.full_requests >= 1
    assert any(offset > 0 for offset, _ in handler.data_reads)


def test_offset_unsupported_does_not_overwrite_file(
    server_and_connection: tuple[SymbolTableHandler, pyads.Connection],
    tmp_path: Path,
) -> None:
    """PLC 不支持偏移时，应报告失败并保留原文件。"""
    handler, plc = server_and_connection
    handler.mode = "reject_offset"
    exporter = _load_exporter()
    exporter.TRY_FULL_TABLE_FIRST = False
    exporter.CHUNK_SIZES = (64, 40)
    output = tmp_path / "symbols.txt"
    output.write_text("previous\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="所有已启用的枚举策略"):
        exporter.export_symbols(plc, output)

    assert output.read_text(encoding="utf-8") == "previous\n"
    assert not output.with_name(output.name + ".partial").exists()


def test_server_ignores_offset_is_detected(
    server_and_connection: tuple[SymbolTableHandler, pyads.Connection],
    tmp_path: Path,
) -> None:
    handler, plc = server_and_connection
    handler.mode = "ignore_offset"
    exporter = _load_exporter()
    exporter.TRY_FULL_TABLE_FIRST = False
    exporter.CHUNK_SIZES = (64,)

    with pytest.raises(RuntimeError, match="字节偏移"):
        exporter.export_symbols(plc, tmp_path / "symbols.txt")


def test_full_table_success_skips_chunk_fallback(
    server_and_connection: tuple[SymbolTableHandler, pyads.Connection],
    tmp_path: Path,
) -> None:
    """验证成功时不会继续尝试分段策略。"""
    handler, plc = server_and_connection
    handler.mode = "normal"
    exporter = _load_exporter()
    exporter.TRY_FULL_TABLE_FIRST = True
    exporter.CHUNK_SIZES = (64,)
    output = tmp_path / "symbols.txt"

    assert exporter.export_symbols(plc, output) == len(SYMBOL_NAMES)
    assert output.read_text(encoding="utf-8").splitlines() == list(SYMBOL_NAMES)
    assert handler.full_requests >= 1
    assert not any(offset > 0 for offset, _ in handler.data_reads)
