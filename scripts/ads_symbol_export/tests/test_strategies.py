"""三方案调度与文件服务策略的单元测试（无需真实 PLC）。

测试边界：文件服务使用 Fake/Mock 对象，仅验证客户端逻辑与异常
处理；真实 TwinCAT 兼容性需要现场设备验证，不能由这些测试证明。
"""

from __future__ import annotations

import ctypes
import struct
import types
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from ads_export import file_service, runtime
from ads_export.config import Settings
from ads_export.strategies import StrategyOutcome, export_symbols


def _entry(name: str) -> bytes:
    raw = name.encode()
    body = raw + b"\0" + b"INT\0" + b"\0"
    header = bytearray(30)
    struct.pack_into("<I", header, 0, len(header) + len(body))
    struct.pack_into("<HHH", header, 24, len(raw), 3, 0)
    return bytes(header) + body


def _tpy(names: list[str]) -> bytes:
    symbols = "".join(f"<Symbol><Name>{n}</Name></Symbol>" for n in names)
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<TcModuleClass xmlns="http://www.beckhoff.com/schemas/2009/05/TcModule">'
        f"<Modules><Module><Symbols>{symbols}</Symbols></Module></Modules>"
        "</TcModuleClass>"
    ).encode()


class FakeAdsError(Exception):
    def __init__(self, err_code: int) -> None:
        super().__init__(f"ADS error {err_code}")
        self.err_code = err_code


class FakePLC:
    def __init__(self, payload: bytes, count: int) -> None:
        self.payload = payload
        self.count = count

    def read(self, group: int, offset: int, datatype: Any, *, return_ctypes: bool) -> Any:
        assert return_ctypes
        length = ctypes.sizeof(datatype)
        if group == 0xF00F:
            result = struct.pack("<II", self.count, len(self.payload))
        else:
            assert group == 0xF00B
            result = self.payload[offset : offset + length]
        return datatype.from_buffer_copy(result)


class FakeFileServiceConn:
    """模拟 System Service(10000) 连接：FOPEN/FREAD/FCLOSE/FFILEFIND。"""

    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files
        self.handles: dict[int, tuple[str, int]] = {}
        self.closed_handles: list[int] = []
        self.read_calls = 0
        self.max_read_size = 0
        self.find_queue: list[tuple[str, bool]] = []
        self.conn_closed = False

    def read_state(self) -> tuple[int, int]:
        return (5, 0)

    def close(self) -> None:
        self.conn_closed = True

    def read_write(
        self,
        group: int,
        offset: int,
        read_type: Any,
        value: bytes,
        write_type: Any,
        return_ctypes: bool = False,
    ) -> Any:
        if group == 120:  # FOPEN
            path = bytes(value).decode("cp1252")
            if path not in self.files:
                raise FakeAdsError(0x702)
            handle = len(self.handles) + 1
            self.handles[handle] = (path, 0)
            assert read_type is not None
            return read_type.from_buffer_copy(struct.pack("<I", handle))
        if group == 121:  # FCLOSE
            self.handles.pop(offset, None)
            self.closed_handles.append(offset)
            return None
        if group == 133:  # FFILEFIND
            if offset == 1:  # GENERIC：首次调用携带 pattern
                pattern = bytes(value).decode("cp1252")
                assert pattern.endswith("\\*")
                directory = pattern[:-2]
                self.find_queue = [
                    (p[len(directory) + 1 :], False)
                    for p in self.files
                    if p.startswith(directory + "\\")
                ]
            if not self.find_queue:
                raise FakeAdsError(1804)
            name, is_dir = self.find_queue.pop(0)
            data = bytearray(324)
            struct.pack_into("<II", data, 0, offset + 1, 0x10 if is_dir else 0)
            encoded = name.encode("cp1252")
            data[48 : 48 + len(encoded)] = encoded
            assert read_type is not None
            return read_type.from_buffer_copy(bytes(data))
        raise AssertionError(f"unexpected group {group}")

    def fread(self, handle: int, size: int) -> bytes:
        """模拟 FREAD：末段正常短读，EOF 返回 b\"\"（adstool 官方语义）。"""
        self.read_calls += 1
        self.max_read_size = max(self.max_read_size, size)
        path, pos = self.handles[handle]
        content = self.files[path]
        if pos >= len(content):
            return b""
        chunk = content[pos : pos + size]
        self.handles[handle] = (path, pos + len(chunk))
        return chunk


def _file_factory(
    files: dict[str, bytes], conns: list[FakeFileServiceConn]
) -> Callable[[], FakeFileServiceConn]:
    def factory() -> FakeFileServiceConn:
        conn = FakeFileServiceConn(files)
        conns.append(conn)
        return conn

    return factory


# ---------- 1/2. 环境检测影响方案选择、Windows/Linux 可用性 ----------
# ---------- 3. 文件服务正常读取 ----------
def test_file_service_reads_tpy(
    settings: Settings, fake_ads: types.ModuleType, tmp_path: Path
) -> None:
    settings.CHUNK_SIZES = ()  # 禁用运行时方案，直达文件服务
    names = ["MAIN.temperature", "GVL.状态", "MAIN.pressure"]
    payload = _entry("MAIN.temperature") + _entry("GVL.状态") + _entry("MAIN.pressure")
    plc = FakePLC(payload, 3)  # 运行时声明 3 个符号
    files = {r"C:\TwinCAT\Boot\port_801.tmc": _tpy(names)}
    conns: list[FakeFileServiceConn] = []
    outcomes: list[StrategyOutcome] = []
    output = tmp_path / "symbols.txt"

    count = export_symbols(
        plc,
        output,
        file_service_factory=_file_factory(files, conns),
        outcomes=outcomes,
        settings=settings,
    )

    assert count == 3
    text = output.read_text(encoding="utf-8")
    assert text.splitlines() == names
    outcome = [o for o in outcomes if o.name == "ads-file"][0]
    assert outcome.ok and outcome.source == "file"
    assert outcome.verification == "count_matched"  # 数量一致，不声称内容一致
    # 所有打开的连接与远端句柄均释放
    assert conns and all(c.conn_closed for c in conns)
    used = [c for c in conns if c.handles or c.closed_handles]
    assert used and not used[0].handles and used[0].closed_handles


# ---------- 4. 文件缺失与解析失败 ----------
def test_file_service_missing_and_malformed(
    settings: Settings, fake_ads: types.ModuleType, tmp_path: Path
) -> None:
    settings.CHUNK_SIZES = ()
    files = {r"C:\TwinCAT\Boot\broken.tmc": b"<TcModuleClass><Symbols><Symbol>"}
    conns: list[FakeFileServiceConn] = []
    output = tmp_path / "symbols.txt"
    output.write_text("previous\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="所有已启用的枚举策略") as err:
        export_symbols(
            FakePLC(_entry("A.x"), 1),
            output,
            file_service_factory=_file_factory(files, conns),
            settings=settings,
        )
    assert "ads-file" in str(err.value)
    assert output.read_text(encoding="utf-8") == "previous\n"

    conns.clear()
    with pytest.raises(RuntimeError, match="未发现 TPY/TMC"):
        export_symbols(
            FakePLC(_entry("A.x"), 1),
            tmp_path / "empty.txt",
            file_service_factory=_file_factory({}, conns),
            settings=settings,
        )


# ---------- 5. 大文件分段读取 ----------
def test_large_file_read_in_chunks(
    settings: Settings, fake_ads: types.ModuleType, tmp_path: Path
) -> None:
    settings.FILE_READ_CHUNK = 128
    settings.CHUNK_SIZES = ()
    names = [f"GVL.var_{i}" for i in range(50)]
    content = _tpy(names)
    assert len(content) > 5 * 128
    files = {r"C:\TwinCAT\Boot\big.tmc": content}
    conns: list[FakeFileServiceConn] = []
    output = tmp_path / "symbols.txt"

    count = export_symbols(
        FakePLC(_entry("A.x"), 1),
        output,
        file_service_factory=_file_factory(files, conns),
        settings=settings,
    )

    assert count == 50
    reader = [c for c in conns if c.read_calls][0]
    assert reader.read_calls > 1  # 确实分段读取
    assert output.read_text(encoding="utf-8").splitlines() == names


# ---------- 6. 文件符号与运行时不一致 ----------
def test_file_runtime_mismatch_marks_unverified(
    settings: Settings, fake_ads: types.ModuleType, tmp_path: Path
) -> None:
    settings.CHUNK_SIZES = ()
    files = {r"C:\TwinCAT\Boot\port_801.tmc": _tpy(["MAIN.a", "MAIN.b"])}
    plc = FakePLC(_entry("A.x") * 3, 3)  # 运行时声明 3 个，文件只有 2 个
    outcomes: list[StrategyOutcome] = []
    output = tmp_path / "symbols.txt"

    count = export_symbols(
        plc,
        output,
        file_service_factory=_file_factory(files, []),
        outcomes=outcomes,
        settings=settings,
    )

    assert count == 2
    outcome = [o for o in outcomes if o.name == "ads-file"][0]
    assert outcome.ok and outcome.verification == "unverified"
    assert output.read_text(encoding="utf-8").splitlines() == ["MAIN.a", "MAIN.b"]


# ---------- 9. 顺序执行与提前停止 ----------
def test_strategies_run_in_order_and_stop_early(
    settings: Settings, fake_ads: types.ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    called: list[str] = []

    def spy(
        name: str, result: int | tuple[int, str] | None = None, error: str | None = None
    ) -> Callable[..., int | tuple[int, str]]:
        def fn(*args: object, **kwargs: object) -> int | tuple[int, str]:
            called.append(name)
            if error:
                raise RuntimeError(error)
            assert result is not None
            return result

        return fn

    monkeypatch.setattr(runtime, "_export_full_table", spy("full", result=3))
    monkeypatch.setattr(runtime, "_export_in_chunks", spy("chunk", result=3))
    monkeypatch.setattr(
        file_service, "_export_via_file_service", spy("file", result=(3, "count_matched"))
    )

    settings.TRY_FULL_TABLE_FIRST = True
    settings.CHUNK_SIZES = (64,)

    assert export_symbols(MagicMock(), tmp_path / "s.txt", settings=settings) == 3
    assert called == ["full"]  # 成功即停

    called.clear()
    monkeypatch.setattr(runtime, "_export_full_table", spy("full", error="oom"))
    monkeypatch.setattr(runtime, "_export_in_chunks", spy("chunk", error="bad"))
    # MagicMock 桩无法推导 System Service 地址，显式提供文件服务工厂
    assert (
        export_symbols(
            MagicMock(), tmp_path / "s.txt", file_service_factory=MagicMock, settings=settings
        )
        == 3
    )
    assert called == ["full", "chunk", "file"]


# ---------- 10. 断连后重连 ----------
def test_reconnect_after_connection_loss(
    settings: Settings, fake_ads: types.ModuleType, tmp_path: Path
) -> None:
    payload = _entry("A.x") + _entry("B.y")
    dead_plc = MagicMock()
    dead_plc.read.side_effect = RuntimeError("connection lost")
    dead_plc.read_state.side_effect = RuntimeError("dead")
    alive_plc = FakePLC(payload, 2)
    alive_plc.read_state = lambda: (5, 0)  # type: ignore[attr-defined]
    reconnected: list[bool] = []

    def reconnect() -> FakePLC:
        reconnected.append(True)
        return alive_plc

    settings.TRY_FULL_TABLE_FIRST = True
    settings.CHUNK_SIZES = (64,)
    output = tmp_path / "symbols.txt"

    assert export_symbols(dead_plc, output, reconnect=reconnect, settings=settings) == 2
    assert reconnected == [True]
    assert output.read_text(encoding="utf-8").splitlines() == ["A.x", "B.y"]


def test_unrecoverable_disconnect_stops_runtime_strategies(
    settings: Settings, fake_ads: types.ModuleType, tmp_path: Path
) -> None:
    dead_plc = MagicMock()
    dead_plc.read.side_effect = RuntimeError("connection lost")
    dead_plc.read_state.side_effect = RuntimeError("dead")
    settings.TRY_FULL_TABLE_FIRST = True
    settings.CHUNK_SIZES = (64, 32)

    def reconnect() -> MagicMock:
        raise RuntimeError("plc unreachable")

    with pytest.raises(RuntimeError, match="所有已启用的枚举策略") as err:
        export_symbols(dead_plc, tmp_path / "s.txt", reconnect=reconnect, settings=settings)
    text = str(err.value)
    assert "runtime-reconnect" in text  # 明确记录连接不可恢复
    assert "chunk=32" not in text  # 不做无谓重试


# ---------- 11. 全部失败汇总 ----------
def test_all_strategies_fail_summary(
    settings: Settings, fake_ads: types.ModuleType, tmp_path: Path
) -> None:
    settings.TRY_FULL_TABLE_FIRST = True
    settings.CHUNK_SIZES = (64,)

    def reject(group: int, offset: int, datatype: object, *, return_ctypes: bool) -> object:
        if group == 0xF00B:
            raise RuntimeError("upload unsupported")
        return FakePLC.read(plc, group, offset, datatype, return_ctypes=return_ctypes)

    plc = FakePLC(_entry("A.x"), 1)
    plc.read = reject  # type: ignore[method-assign]
    conns: list[FakeFileServiceConn] = []

    with pytest.raises(RuntimeError, match="所有已启用的枚举策略") as err:
        export_symbols(
            plc,
            tmp_path / "s.txt",
            file_service_factory=_file_factory({}, conns),
            settings=settings,
        )
    text = str(err.value)
    assert "full-table" in text and "chunk=64" in text
    assert "ads-file" in text


# ---------- 12. TPY 中 UTF-8 名称 ----------
def test_tpy_utf8_symbol_names(
    settings: Settings, fake_ads: types.ModuleType, tmp_path: Path
) -> None:
    settings.CHUNK_SIZES = ()
    names = ["GVL.温度", "GVL.压力"]
    files = {r"C:\TwinCAT\Boot\port_801.tmc": _tpy(names)}
    output = tmp_path / "symbols.txt"
    count = export_symbols(
        FakePLC(_entry("A.x") * 2, 2),
        output,
        file_service_factory=_file_factory(files, []),
        settings=settings,
    )
    assert count == 2
    assert output.read_text(encoding="utf-8").splitlines() == names


# ---------- 显式读取方法：指定模式不允许跨策略回退 ----------
def test_read_method_full_only(
    settings: Settings, fake_ads: types.ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    names = ["MAIN.A", "MAIN.B"]
    plc = FakePLC(b"".join(_entry(name) for name in names), len(names))
    monkeypatch.setattr(
        runtime, "_export_in_chunks", MagicMock(side_effect=AssertionError("unexpected chunk"))
    )
    file_factory = MagicMock(side_effect=AssertionError("unexpected file"))
    result = export_symbols(
        plc, tmp_path / "symbols.txt", read_method="full", settings=settings,
        file_service_factory=file_factory,
    )
    file_factory.assert_not_called()
    assert result == 2
    assert (tmp_path / "symbols.txt").read_text(encoding="utf-8").splitlines() == names


def test_read_method_chunk_only(
    settings: Settings, fake_ads: types.ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    names = ["MAIN.A", "MAIN.B"]
    plc = FakePLC(b"".join(_entry(name) for name in names), len(names))
    monkeypatch.setattr(
        runtime, "_export_full_table", MagicMock(side_effect=AssertionError("unexpected full"))
    )
    file_factory = MagicMock(side_effect=AssertionError("unexpected file"))
    settings.CHUNK_SIZES = (64,)
    assert (
        export_symbols(
            plc, tmp_path / "symbols.txt", read_method="chunk", settings=settings,
            file_service_factory=file_factory,
        ) == 2
    )
    file_factory.assert_not_called()


def test_read_method_invalid_rejected_before_connection(
    settings: Settings,
    fake_ads: types.ModuleType,
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="READ_METHOD"):
        export_symbols(object(), tmp_path / "symbols.txt", read_method="invalid", settings=settings)


def test_read_method_full_failure_does_not_fallback(
    settings: Settings,
    fake_ads: types.ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        runtime, "_export_full_table", MagicMock(side_effect=RuntimeError("read error"))
    )
    chunk = MagicMock()
    file_factory = MagicMock()
    monkeypatch.setattr(runtime, "_export_in_chunks", chunk)
    with pytest.raises(RuntimeError, match="指定 full 方法读取失败"):
        export_symbols(
            object(), tmp_path / "symbols.txt", read_method="full", settings=settings,
            file_service_factory=file_factory,
        )
    chunk.assert_not_called()
    file_factory.assert_not_called()


def test_read_method_file_only_skips_runtime_exports(
    settings: Settings,
    fake_ads: types.ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    full = MagicMock()
    chunks = MagicMock()
    monkeypatch.setattr(runtime, "_export_full_table", full)
    monkeypatch.setattr(runtime, "_export_in_chunks", chunks)
    with pytest.raises(RuntimeError, match="指定 file 方法读取失败"):
        export_symbols(object(), tmp_path / "symbols.txt", read_method="file", settings=settings)
    full.assert_not_called()
    chunks.assert_not_called()


def test_file_size_limit_boundary(
    settings: Settings, fake_ads: types.ModuleType, tmp_path: Path
) -> None:
    """恰好等于上限的文件合法；超出 1 字节失败且不覆盖旧输出。"""
    names = ["MAIN.a", "MAIN.b"]
    content = _tpy(names)
    settings.CHUNK_SIZES = ()
    settings.FILE_READ_CHUNK = 64
    files = {r"C:\TwinCAT\Boot\port_801.tmc": content}
    output = tmp_path / "symbols.txt"

    settings.MAX_TPY_FILE_BYTES = len(content)
    assert (
        export_symbols(
            FakePLC(_entry("A.x"), 1),
            output,
            file_service_factory=_file_factory(files, []),
            settings=settings,
        )
        == 2
    )
    assert output.read_text(encoding="utf-8").splitlines() == names

    settings.MAX_TPY_FILE_BYTES = len(content) - 1
    output.write_text("previous\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="安全上限"):
        export_symbols(
            FakePLC(_entry("A.x"), 1),
            output,
            file_service_factory=_file_factory(files, []),
            settings=settings,
        )
    assert output.read_text(encoding="utf-8") == "previous\n"
    assert not list(tmp_path.glob("ads_remote_*"))


def test_fread_request_observes_read_chunk(
    settings: Settings, fake_ads: types.ModuleType, tmp_path: Path
) -> None:
    """FREAD 请求长度受 FILE_READ_CHUNK 约束，不会一次性请求整个文件。"""
    names = [f"GVL.v{i}" for i in range(40)]
    content = _tpy(names)
    settings.CHUNK_SIZES = ()
    settings.FILE_READ_CHUNK = 32
    assert len(content) > 4 * 32
    files = {r"C:\TwinCAT\Boot\port_801.tmc": content}
    conns: list[FakeFileServiceConn] = []
    output = tmp_path / "symbols.txt"
    assert export_symbols(
        FakePLC(_entry("A.x"), 1),
        output,
        file_service_factory=_file_factory(files, conns),
        settings=settings,
    ) == len(names)
    reader = next(c for c in conns if c.read_calls)
    assert reader.max_read_size <= settings.FILE_READ_CHUNK
