"""ADS 流式点表工具单元测试（无需真实 PLC）。"""

from __future__ import annotations

import ctypes
import importlib.util
import struct
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def exporter(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    pyads = types.ModuleType("pyads")
    pyads.PLCTYPE_BYTE = ctypes.c_ubyte
    pyads.Connection = MagicMock()  # type: ignore[attr-defined]
    pyads.add_route = MagicMock()  # type: ignore[attr-defined]
    pyads.add_route_to_plc = MagicMock()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pyads", pyads)
    path = Path(__file__).resolve().parents[1] / "export.py"
    spec = importlib.util.spec_from_file_location("_ads_export_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclass 解析字符串注解时需要在 sys.modules 中找到模块
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    module.TRY_FULL_TABLE_FIRST = False
    return module


def _entry(name: str) -> bytes:
    raw = name.encode()
    body = raw + b"\0" + b"INT\0" + b"\0"
    header = bytearray(30)
    struct.pack_into("<I", header, 0, len(header) + len(body))
    struct.pack_into("<HHH", header, 24, len(raw), 3, 0)
    return bytes(header) + body


class FakePLC:
    def __init__(self, payload: bytes, count: int) -> None:
        self.payload = payload
        self.count = count
        self.requests: list[tuple[int, int, int]] = []

    def read(self, group: int, offset: int, datatype: object, *, return_ctypes: bool) -> object:
        assert return_ctypes
        length = ctypes.sizeof(datatype)
        self.requests.append((group, offset, length))
        if group == 0xF00F:
            result = struct.pack("<II", self.count, len(self.payload))
        else:
            assert group == 0xF00B
            result = self.payload[offset : offset + length]
        return datatype.from_buffer_copy(result)


def test_streams_across_record_boundaries(exporter: types.ModuleType, tmp_path: Path) -> None:
    payload = _entry("A.x") + _entry("B.中文") + _entry("C.z")
    plc = FakePLC(payload, 3)
    exporter.CHUNK_SIZES = (35,)
    output = tmp_path / "symbols.txt"
    assert exporter.export_symbols(plc, output) == 3
    assert output.read_text(encoding="utf-8") == "A.x\nB.中文\nC.z\n"
    assert all(length <= 35 for group, _, length in plc.requests if group == 0xF00B)


def test_inconsistent_count_preserves_existing_file(
    exporter: types.ModuleType, tmp_path: Path
) -> None:
    plc = FakePLC(_entry("A." + "x" * 30) + _entry("B." + "y" * 30), 3)
    output = tmp_path / "symbols.txt"
    output.write_text("previous\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="不完整"):
        exporter.export_symbols(plc, output)
    assert output.read_text(encoding="utf-8") == "previous\n"
    assert not (tmp_path / "symbols.txt.partial").exists()


def test_corrupt_symbol_header_fails(exporter: types.ModuleType, tmp_path: Path) -> None:
    payload = bytearray(_entry("A.x"))
    struct.pack_into("<I", payload, 0, 10)
    with pytest.raises(RuntimeError, match="非法"):
        exporter.export_symbols(FakePLC(bytes(payload), 1), tmp_path / "bad.txt")


def test_successful_connection_does_not_add_remote_route(exporter: types.ModuleType) -> None:
    exporter.ADD_REMOTE_ROUTE_IF_NEEDED = True
    exporter.ADD_LOCAL_ROUTE = False
    plc = exporter.pyads.Connection.return_value
    plc.read_state.return_value = (5, 0)
    assert exporter._open_connection() is plc
    exporter.pyads.add_route_to_plc.assert_not_called()
    plc.close.assert_not_called()


def test_remote_route_disabled_on_failed_connection(exporter: types.ModuleType) -> None:
    exporter.ADD_REMOTE_ROUTE_IF_NEEDED = False
    plc = exporter.pyads.Connection.return_value
    plc.read_state.side_effect = RuntimeError("no route")
    with pytest.raises(RuntimeError, match="no route"):
        exporter._open_connection()
    plc.close.assert_called_once()
    exporter.pyads.add_route_to_plc.assert_not_called()


def test_fallback_to_smaller_chunk(exporter: types.ModuleType, tmp_path: Path) -> None:
    payload = _entry("First.x") + _entry("Second.y")
    original = exporter._read_bytes

    def limited_read(plc: object, group: int, offset: int, length: int) -> bytes:
        if group == 0xF00B and length > 70:
            raise RuntimeError("request too large")
        return original(plc, group, offset, length)

    exporter._read_bytes = limited_read
    exporter.CHUNK_SIZES = (4096, 64)
    output = tmp_path / "symbols.txt"
    assert exporter.export_symbols(FakePLC(payload, 2), output) == 2
    assert output.read_text(encoding="utf-8").splitlines() == ["First.x", "Second.y"]


def test_all_strategies_fail_without_replacing_output(
    exporter: types.ModuleType, tmp_path: Path
) -> None:
    exporter.CHUNK_SIZES = (64, 35)
    output = tmp_path / "symbols.txt"
    output.write_text("old", encoding="utf-8")
    plc = FakePLC(_entry("First.x") + _entry("Second.y"), 2)

    def reject_offset(group: int, offset: int, datatype: object, *, return_ctypes: bool) -> object:
        if group == 0xF00B and offset > 0:
            raise RuntimeError("nonzero offset unsupported")
        return FakePLC.read(plc, group, offset, datatype, return_ctypes=return_ctypes)

    plc.read = reject_offset  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="所有已启用的枚举策略"):
        exporter.export_symbols(plc, output)
    assert output.read_text(encoding="utf-8") == "old"


@pytest.mark.parametrize(("version", "expected_port"), [(2, 801), (3, 851)])
def test_twincat_version_port(
    exporter: types.ModuleType, version: int, expected_port: int
) -> None:
    exporter.TWINCAT_VERSION = version
    exporter.PLC_AMS_PORT = None
    exporter.ADD_LOCAL_ROUTE = False
    exporter._open_connection()
    assert exporter.pyads.Connection.call_args.args[1] == expected_port


def test_invalid_twincat_version(exporter: types.ModuleType) -> None:
    exporter.TWINCAT_VERSION = 4
    with pytest.raises(ValueError, match="只能为"):
        exporter._open_connection()


def test_full_table_is_first_and_skips_chunks(
    exporter: types.ModuleType, tmp_path: Path
) -> None:
    payload = _entry("A.x") + _entry("B.y")
    plc = FakePLC(payload, 2)
    exporter.TRY_FULL_TABLE_FIRST = True
    exporter._export_in_chunks = MagicMock(
        side_effect=AssertionError("chunk path should not run")
    )
    output = tmp_path / "symbols.txt"
    assert exporter.export_symbols(plc, output) == 2
    assert output.read_text(encoding="utf-8").splitlines() == ["A.x", "B.y"]
    # 整表方案直接一次性读取 0xF00B 全量字节
    assert (0xF00B, 0, len(payload)) in plc.requests
    exporter._export_in_chunks.assert_not_called()


def test_full_table_failure_falls_back_to_chunks(
    exporter: types.ModuleType, tmp_path: Path
) -> None:
    payload = _entry("A.x")
    plc = FakePLC(payload, 1)

    def reject_full_read(
        group: int, offset: int, datatype: object, *, return_ctypes: bool
    ) -> object:
        if group == 0xF00B and ctypes.sizeof(datatype) >= len(payload):
            plc.requests.append((group, offset, ctypes.sizeof(datatype)))
            raise RuntimeError("PLC out of memory")
        return FakePLC.read(plc, group, offset, datatype, return_ctypes=return_ctypes)

    plc.read = reject_full_read  # type: ignore[method-assign]
    exporter.TRY_FULL_TABLE_FIRST = True
    exporter._export_in_chunks = MagicMock(return_value=1)
    assert exporter.export_symbols(plc, tmp_path / "symbols.txt") == 1
    # 整表读取确实尝试过并失败后转入分段方案
    assert any(group == 0xF00B for group, _, _ in plc.requests)
    exporter._export_in_chunks.assert_called_once()


@pytest.mark.parametrize("try_full_first", [True, False])
def test_windows1252_symbol_names_via_configurable_encoding(
    exporter: types.ModuleType, tmp_path: Path, try_full_first: bool
) -> None:
    """部分 TwinCAT 设备的符号名为 Windows-1252，通过 TEXT_ENCODING 配置解码。"""
    name = "GVL.Temperatur_°C"
    raw = name.encode("cp1252")
    body = raw + b"\0" + b"INT\0" + b"\0"
    header = bytearray(30)
    struct.pack_into("<I", header, 0, 30 + len(body))
    struct.pack_into("<HHH", header, 24, len(raw), 3, 0)
    exporter.TEXT_ENCODING = "cp1252"
    exporter.TRY_FULL_TABLE_FIRST = try_full_first
    exporter.CHUNK_SIZES = (64,)
    output = tmp_path / "symbols.txt"
    assert exporter.export_symbols(FakePLC(bytes(header) + body, 1), output) == 1
    assert output.read_text(encoding="utf-8") == name + "\n"


def test_disable_full_table(exporter: types.ModuleType, tmp_path: Path) -> None:
    exporter.TRY_FULL_TABLE_FIRST = False
    exporter._export_full_table = MagicMock(
        side_effect=AssertionError("full-table path should not run")
    )
    exporter._export_in_chunks = MagicMock(return_value=1)
    assert exporter.export_symbols(MagicMock(), tmp_path / "symbols.txt") == 1
    exporter._export_full_table.assert_not_called()


def test_environment_detection_does_not_modify_plc(
    exporter: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(exporter.platform, "system", lambda: "Linux")
    monkeypatch.setattr(exporter.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(exporter.shutil, "which", lambda name: None)
    exporter._inspect_environment()
    exporter.pyads.Connection.assert_not_called()
    exporter.pyads.add_route_to_plc.assert_not_called()
