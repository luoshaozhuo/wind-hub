"""四方案调度、环境检测、文件服务与 OCX 策略的单元测试（无需真实 PLC）。

测试边界：文件服务与 OCX 均使用 Fake/Mock 对象，仅验证客户端逻辑与异常
处理；真实 TwinCAT 兼容性需要现场设备验证，不能由这些测试证明。
"""

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
    spec = importlib.util.spec_from_file_location("_ads_strategies_test", path)
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

    def read(self, group: int, offset: int, datatype: object, *, return_ctypes: bool) -> object:
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
        read_type: object,
        value: bytes,
        write_type: object,
        return_ctypes: bool = False,
    ) -> object:
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
            data = bytearray(322)
            struct.pack_into("<II", data, 0, offset + 1, 0x10 if is_dir else 0)
            encoded = name.encode("cp1252")
            data[48 : 48 + len(encoded)] = encoded
            assert read_type is not None
            return read_type.from_buffer_copy(bytes(data))
        raise AssertionError(f"unexpected group {group}")

    def read(
        self,
        group: int,
        offset: int,
        datatype: object,
        return_ctypes: bool = False,
        check_length: bool = True,
    ) -> object:
        assert group == 122  # FREAD
        assert not check_length  # 客户端必须容忍末段短读
        self.read_calls += 1
        path, pos = self.handles[offset]
        content = self.files[path]
        if pos >= len(content):
            raise FakeAdsError(0x701)  # EOF
        size = ctypes.sizeof(datatype)
        chunk = content[pos : pos + size]
        self.handles[offset] = (path, pos + len(chunk))
        if len(chunk) < size:
            chunk = chunk + bytes(size - len(chunk))  # 末段补零，由 XML 解析兜底
        return datatype.from_buffer_copy(chunk)


def _file_factory(files: dict[str, bytes], conns: list[FakeFileServiceConn]):
    def factory() -> FakeFileServiceConn:
        conn = FakeFileServiceConn(files)
        conns.append(conn)
        return conn

    return factory


def _env(exporter: types.ModuleType, os_name: str = "Linux", **kw: object):
    defaults: dict[str, object] = {
        "os_name": os_name, "arch": "x86_64", "python_bits": 64,
        "pyads_version": "3.6.0",
        "adslib_available": True, "adstool": None, "twincat_version": 2,
        "ams_net_id": "127.0.0.1.1.1", "ads_port": 801,
        "com_module_available": False, "ocx_progid_registered": None,
    }
    defaults.update(kw)
    return exporter.StaticEnvironment(**defaults)


# ---------- 1/2. 环境检测影响方案选择、Windows/Linux 可用性 ----------
def test_ocx_availability_by_platform(exporter: types.ModuleType) -> None:
    linux = _env(exporter, "Linux")
    ok, reason = linux.ocx_possible()
    assert not ok and "不支持" in reason

    win_no_com = _env(exporter, "Windows", com_module_available=False)
    ok, reason = win_no_com.ocx_possible()
    assert not ok and "pywin32" in reason

    win_no_reg = _env(
        exporter, "Windows", com_module_available=True, ocx_progid_registered=False
    )
    ok, reason = win_no_reg.ocx_possible()
    assert not ok and "未在本机注册" in reason

    win_ready = _env(
        exporter, "Windows", com_module_available=True, ocx_progid_registered=True
    )
    assert win_ready.ocx_possible() == (True, "")


def test_env_detection_drives_strategy_selection(
    exporter: types.ModuleType, tmp_path: Path
) -> None:
    """Linux 环境下 OCX 直接跳过，原因记录在 outcomes 中。"""
    exporter.CHUNK_SIZES = ()  # 让所有方案都被评估到
    plc = FakePLC(_entry("A.x"), 1)
    outcomes: list = []
    with pytest.raises(RuntimeError, match="所有已启用的枚举策略"):
        exporter.export_symbols(
            plc, tmp_path / "s.txt", env=_env(exporter, "Linux"), outcomes=outcomes
        )
    ocx = [o for o in outcomes if o.name == "ocx"]
    assert ocx and not ocx[0].ok and "不支持" in ocx[0].reason


# ---------- 3. 文件服务正常读取 ----------
def test_file_service_reads_tpy(exporter: types.ModuleType, tmp_path: Path) -> None:
    exporter.CHUNK_SIZES = ()  # 禁用运行时方案，直达文件服务
    names = ["MAIN.temperature", "GVL.状态", "MAIN.pressure"]
    payload = _entry("MAIN.temperature") + _entry("GVL.状态") + _entry("MAIN.pressure")
    plc = FakePLC(payload, 3)  # 运行时声明 3 个符号
    files = {r"C:\TwinCAT\Boot\port_801.tmc": _tpy(names)}
    conns: list[FakeFileServiceConn] = []
    outcomes: list = []
    output = tmp_path / "symbols.txt"

    count = exporter.export_symbols(
        plc, output,
        env=_env(exporter),
        file_service_factory=_file_factory(files, conns),
        outcomes=outcomes,
    )

    assert count == 3
    text = output.read_text(encoding="utf-8")
    assert "source=file" in text and "verified=True" in text
    assert text.splitlines()[-3:] == names
    outcome = [o for o in outcomes if o.name == "ads-file"][0]
    assert outcome.ok and outcome.source == "file" and outcome.verified
    # 所有打开的连接与远端句柄均释放
    assert conns and all(c.conn_closed for c in conns)
    used = [c for c in conns if c.handles or c.closed_handles]
    assert used and not used[0].handles and used[0].closed_handles


# ---------- 4. 文件缺失与解析失败 ----------
def test_file_service_missing_and_malformed(
    exporter: types.ModuleType, tmp_path: Path
) -> None:
    exporter.CHUNK_SIZES = ()
    files = {r"C:\TwinCAT\Boot\broken.tmc": b"<TcModuleClass><Symbols><Symbol>"}
    conns: list[FakeFileServiceConn] = []
    output = tmp_path / "symbols.txt"
    output.write_text("previous\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="所有已启用的枚举策略") as err:
        exporter.export_symbols(
            FakePLC(_entry("A.x"), 1), output,
            env=_env(exporter),
            file_service_factory=_file_factory(files, conns),
        )
    assert "ads-file" in str(err.value)
    assert output.read_text(encoding="utf-8") == "previous\n"

    conns.clear()
    with pytest.raises(RuntimeError, match="未发现 TPY/TMC"):
        exporter.export_symbols(
            FakePLC(_entry("A.x"), 1), tmp_path / "empty.txt",
            env=_env(exporter),
            file_service_factory=_file_factory({}, conns),
        )


# ---------- 5. 大文件分段读取 ----------
def test_large_file_read_in_chunks(exporter: types.ModuleType, tmp_path: Path) -> None:
    exporter.FILE_READ_CHUNK = 128
    exporter.CHUNK_SIZES = ()
    names = [f"GVL.var_{i}" for i in range(50)]
    content = _tpy(names)
    assert len(content) > 5 * 128
    files = {r"C:\TwinCAT\Boot\big.tmc": content}
    conns: list[FakeFileServiceConn] = []
    output = tmp_path / "symbols.txt"

    count = exporter.export_symbols(
        FakePLC(_entry("A.x"), 1), output,
        env=_env(exporter),
        file_service_factory=_file_factory(files, conns),
    )

    assert count == 50
    reader = [c for c in conns if c.read_calls][0]
    assert reader.read_calls > 1  # 确实分段读取
    assert output.read_text(encoding="utf-8").splitlines()[-50:] == names


# ---------- 6. 文件符号与运行时不一致 ----------
def test_file_runtime_mismatch_marks_unverified(
    exporter: types.ModuleType, tmp_path: Path
) -> None:
    exporter.CHUNK_SIZES = ()
    files = {r"C:\TwinCAT\Boot\port_801.tmc": _tpy(["MAIN.a", "MAIN.b"])}
    plc = FakePLC(_entry("A.x") * 3, 3)  # 运行时声明 3 个，文件只有 2 个
    outcomes: list = []
    output = tmp_path / "symbols.txt"

    count = exporter.export_symbols(
        plc, output,
        env=_env(exporter),
        file_service_factory=_file_factory(files, []),
        outcomes=outcomes,
    )

    assert count == 2
    outcome = [o for o in outcomes if o.name == "ads-file"][0]
    assert outcome.ok and not outcome.verified
    assert "verified=False" in output.read_text(encoding="utf-8")


# ---------- 7/8. OCX 跳过与 Mock COM 枚举 ----------
def test_ocx_mock_com_enumeration(exporter: types.ModuleType, tmp_path: Path) -> None:
    exporter.CHUNK_SIZES = ()
    com = MagicMock()
    calls: list[bool] = []

    def enum(first: bool) -> str:
        calls.append(first)
        return {0: "MAIN.a", 1: "GVL.b", 2: ""}[len(calls) - 1]

    com.AdsEnumSymbols = enum
    env = _env(exporter, "Windows", com_module_available=True, ocx_progid_registered=True)
    outcomes: list = []
    output = tmp_path / "symbols.txt"

    count = exporter.export_symbols(
        FakePLC(_entry("A.x"), 1), output,
        env=env, ocx_factory=lambda: com, outcomes=outcomes,
    )

    assert count == 2
    assert calls[0] is True and calls[1:] == [False, False]  # 首次与后续参数不同
    text = output.read_text(encoding="utf-8")
    assert "source=ocx-experimental" in text
    assert text.splitlines()[-2:] == ["MAIN.a", "GVL.b"]
    com.close.assert_called_once()  # COM 资源释放


def test_ocx_without_enum_method_fails_clearly(
    exporter: types.ModuleType, tmp_path: Path
) -> None:
    exporter.CHUNK_SIZES = ()
    com = object()  # 无 AdsEnumSymbols
    env = _env(exporter, "Windows", com_module_available=True, ocx_progid_registered=True)
    with pytest.raises(RuntimeError, match="不提供 AdsEnumSymbols"):
        exporter.export_symbols(
            FakePLC(_entry("A.x"), 1), tmp_path / "s.txt",
            env=env, ocx_factory=lambda: com,
        )


# ---------- 9. 顺序执行与提前停止 ----------
def test_strategies_run_in_order_and_stop_early(
    exporter: types.ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    called: list[str] = []

    def spy(name: str, result: int | None = None, error: str | None = None):
        def fn(*args: object) -> int:
            called.append(name)
            if error:
                raise RuntimeError(error)
            assert result is not None
            return result

        return fn

    monkeypatch.setattr(exporter, "_export_full_table", spy("full", result=3))
    monkeypatch.setattr(exporter, "_export_in_chunks", spy("chunk", result=3))
    monkeypatch.setattr(exporter, "_export_via_file_service", spy("file", result=(3, True)))
    monkeypatch.setattr(exporter, "_export_via_ocx", spy("ocx", result=3))
    exporter.TRY_FULL_TABLE_FIRST = True
    exporter.CHUNK_SIZES = (64,)

    assert exporter.export_symbols(MagicMock(), tmp_path / "s.txt", env=_env(exporter)) == 3
    assert called == ["full"]  # 成功即停

    called.clear()
    monkeypatch.setattr(exporter, "_export_full_table", spy("full", error="oom"))
    monkeypatch.setattr(exporter, "_export_in_chunks", spy("chunk", error="bad"))
    # MagicMock 桩无法推导 System Service 地址，显式提供文件服务工厂
    assert exporter.export_symbols(
        MagicMock(), tmp_path / "s.txt", env=_env(exporter),
        file_service_factory=MagicMock,
    ) == 3
    assert called == ["full", "chunk", "file"]  # ocx 未被调用


# ---------- 10. 断连后重连 ----------
def test_reconnect_after_connection_loss(
    exporter: types.ModuleType, tmp_path: Path
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

    exporter.TRY_FULL_TABLE_FIRST = True
    exporter.CHUNK_SIZES = (64,)
    output = tmp_path / "symbols.txt"

    assert exporter.export_symbols(
        dead_plc, output, env=_env(exporter), reconnect=reconnect
    ) == 2
    assert reconnected == [True]
    assert output.read_text(encoding="utf-8").splitlines() == ["A.x", "B.y"]


def test_unrecoverable_disconnect_stops_runtime_strategies(
    exporter: types.ModuleType, tmp_path: Path
) -> None:
    dead_plc = MagicMock()
    dead_plc.read.side_effect = RuntimeError("connection lost")
    dead_plc.read_state.side_effect = RuntimeError("dead")
    exporter.TRY_FULL_TABLE_FIRST = True
    exporter.CHUNK_SIZES = (64, 32)

    def reconnect() -> MagicMock:
        raise RuntimeError("plc unreachable")

    with pytest.raises(RuntimeError, match="所有已启用的枚举策略") as err:
        exporter.export_symbols(
            dead_plc, tmp_path / "s.txt", env=_env(exporter), reconnect=reconnect
        )
    text = str(err.value)
    assert "runtime-reconnect" in text  # 明确记录连接不可恢复
    assert "chunk=32" not in text  # 不做无谓重试


# ---------- 11. 全部失败汇总 ----------
def test_all_strategies_fail_summary(
    exporter: types.ModuleType, tmp_path: Path
) -> None:
    exporter.TRY_FULL_TABLE_FIRST = True
    exporter.CHUNK_SIZES = (64,)

    def reject(group: int, offset: int, datatype: object, *, return_ctypes: bool) -> object:
        if group == 0xF00B:
            raise RuntimeError("upload unsupported")
        return FakePLC.read(plc, group, offset, datatype, return_ctypes=return_ctypes)

    plc = FakePLC(_entry("A.x"), 1)
    plc.read = reject  # type: ignore[method-assign]
    conns: list[FakeFileServiceConn] = []

    with pytest.raises(RuntimeError, match="所有已启用的枚举策略") as err:
        exporter.export_symbols(
            plc, tmp_path / "s.txt",
            env=_env(exporter),
            file_service_factory=_file_factory({}, conns),
        )
    text = str(err.value)
    assert "full-table" in text and "chunk=64" in text
    assert "ads-file" in text and "ocx" in text


# ---------- 12. TPY 中 UTF-8 名称 ----------
def test_tpy_utf8_symbol_names(exporter: types.ModuleType, tmp_path: Path) -> None:
    exporter.CHUNK_SIZES = ()
    names = ["GVL.温度", "GVL.压力"]
    files = {r"C:\TwinCAT\Boot\port_801.tmc": _tpy(names)}
    output = tmp_path / "symbols.txt"
    count = exporter.export_symbols(
        FakePLC(_entry("A.x") * 2, 2), output,
        env=_env(exporter),
        file_service_factory=_file_factory(files, []),
    )
    assert count == 2
    assert output.read_text(encoding="utf-8").splitlines()[-2:] == names
