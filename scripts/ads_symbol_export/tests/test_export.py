"""ADS 流式点表工具单元测试（无需真实 PLC）。"""

from __future__ import annotations

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
    pyads.PLCTYPE_BYTE = __import__("ctypes").c_ubyte
    pyads.Connection = MagicMock()  # type: ignore[attr-defined]
    pyads.add_route = MagicMock()  # type: ignore[attr-defined]
    pyads.add_route_to_plc = MagicMock()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pyads", pyads)
    path = Path(__file__).resolve().parents[1] / "export.py"
    spec = importlib.util.spec_from_file_location("_ads_export_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
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
        length = __import__("ctypes").sizeof(datatype)
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
    exporter.CHUNK_SIZE = 35
    output = tmp_path / "symbols.txt"
    assert exporter.export_symbols(plc, output) == 3
    assert output.read_text(encoding="utf-8") == "A.x\nB.中文\nC.z\n"
    assert all(length <= 35 for group, _, length in plc.requests if group == 0xF00B)


def test_inconsistent_count_preserves_existing_file(
    exporter: types.ModuleType, tmp_path: Path
) -> None:
    plc = FakePLC(_entry("A.x") + _entry("B.y"), 3)
    output = tmp_path / "symbols.txt"
    output.write_text("previous\n", encoding="utf-8")
    with pytest.raises(ValueError, match="不完整"):
        exporter.export_symbols(plc, output)
    assert output.read_text(encoding="utf-8") == "previous\n"
    assert not (tmp_path / "symbols.txt.partial").exists()


def test_corrupt_symbol_header_fails(exporter: types.ModuleType, tmp_path: Path) -> None:
    payload = bytearray(_entry("A.x"))
    struct.pack_into("<I", payload, 0, 10)
    with pytest.raises(ValueError, match="非法"):
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
