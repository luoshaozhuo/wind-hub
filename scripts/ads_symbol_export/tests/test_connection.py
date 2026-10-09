"""连接生命周期、本地/远端路由策略与 FREAD 可靠性测试（无需真实 PLC）。

测试边界：路由与连接行为基于 MagicMock pyads；真实 TwinCAT Router 行为
需要现场设备验证。
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
    spec = importlib.util.spec_from_file_location("_ads_connection_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclass 解析字符串注解时需要在 sys.modules 中找到模块
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    module.TRY_FULL_TABLE_FIRST = False
    return module


def _env(exporter: types.ModuleType, os_name: str):
    return exporter.StaticEnvironment(
        os_name=os_name, arch="x86_64", python_bits=64, pyads_version="3.6.0",
        adslib_available=True, adstool=None, twincat_version=2,
        ams_net_id="192.168.1.10.1.1", ads_port=801,
        com_module_available=False, ocx_progid_registered=None,
    )


def _route_error() -> Exception:
    exc = RuntimeError("target port not found")
    exc.err_code = 6  # type: ignore[attr-defined]
    return exc


def _ok_plc() -> MagicMock:
    plc = MagicMock()
    plc.read_state.return_value = (5, 0)
    return plc


def _failing_plc(error: Exception) -> MagicMock:
    plc = MagicMock()
    plc.read_state.side_effect = error
    return plc


# ---------- 1. 已能连接：ADD_LOCAL_ROUTE=True 也不调用 add_route ----------
@pytest.mark.parametrize("os_name", ["Windows", "Linux"])
def test_existing_connection_never_adds_local_route(
    exporter: types.ModuleType, os_name: str
) -> None:
    exporter.ADD_LOCAL_ROUTE = True
    plc = _ok_plc()
    exporter.pyads.Connection.return_value = plc
    assert exporter._open_connection(_env(exporter, os_name)) is plc
    exporter.pyads.add_route.assert_not_called()
    exporter.pyads.add_route_to_plc.assert_not_called()


# ---------- 2. 明确本地路由缺失：Windows 下只修复一次后重连成功 ----------
def test_local_route_added_once_on_windows(exporter: types.ModuleType) -> None:
    exporter.ADD_LOCAL_ROUTE = True
    good = _ok_plc()
    exporter.pyads.Connection.side_effect = [_failing_plc(_route_error()), good]
    assert exporter._open_connection(_env(exporter, "Windows")) is good
    exporter.pyads.add_route.assert_called_once_with(
        exporter.PLC_AMS_NET_ID, exporter.PLC_IP
    )


# ---------- 3. 路由修复后仍失败：不重复添加，报告两类错误 ----------
def test_route_retry_failure_not_repeated(exporter: types.ModuleType) -> None:
    exporter.ADD_LOCAL_ROUTE = True
    exporter.pyads.Connection.side_effect = [
        _failing_plc(_route_error()),
        _failing_plc(RuntimeError("still no route")),
    ]
    with pytest.raises(RuntimeError, match="原始错误") as err:
        exporter._open_connection(_env(exporter, "Windows"))
    assert "重试错误" in str(err.value)
    exporter.pyads.add_route.assert_called_once()


# ---------- 4. PLC 离线/超时：不得盲目修改路由 ----------
@pytest.mark.parametrize("os_name", ["Windows", "Linux"])
def test_offline_or_timeout_never_touches_route(
    exporter: types.ModuleType, os_name: str
) -> None:
    exporter.ADD_LOCAL_ROUTE = True
    exporter.pyads.Connection.return_value = _failing_plc(
        TimeoutError("tcp connect timed out")
    )
    with pytest.raises(TimeoutError):
        exporter._open_connection(_env(exporter, os_name))
    exporter.pyads.add_route.assert_not_called()
    exporter.pyads.add_route_to_plc.assert_not_called()


# ---------- 5. Linux 下即使疑似路由缺失也不修改本地路由 ----------
def test_linux_never_adds_local_route(exporter: types.ModuleType) -> None:
    exporter.ADD_LOCAL_ROUTE = True
    exporter.pyads.Connection.return_value = _failing_plc(_route_error())
    with pytest.raises(RuntimeError, match="不修改本地路由"):
        exporter._open_connection(_env(exporter, "Linux"))
    exporter.pyads.add_route.assert_not_called()


# ---------- 6. ADD_LOCAL_ROUTE=False 时只报告不修复 ----------
def test_local_route_disabled_only_reports(exporter: types.ModuleType) -> None:
    exporter.ADD_LOCAL_ROUTE = False
    exporter.pyads.Connection.return_value = _failing_plc(_route_error())
    with pytest.raises(RuntimeError, match="未自动修复"):
        exporter._open_connection(_env(exporter, "Windows"))
    exporter.pyads.add_route.assert_not_called()


# ---------- 7. 远端路由：绝不自动修改，仅提示人工确认 ----------
def test_remote_route_never_auto_modified(exporter: types.ModuleType) -> None:
    exporter.ADD_LOCAL_ROUTE = True
    exporter.ADD_REMOTE_ROUTE_IF_NEEDED = True
    exporter.pyads.Connection.side_effect = [
        _failing_plc(_route_error()),
        _failing_plc(_route_error()),  # 本地修复后仍是路由类错误
    ]
    with pytest.raises(RuntimeError, match="人工确认"):
        exporter._open_connection(_env(exporter, "Windows"))
    exporter.pyads.add_route_to_plc.assert_not_called()


def test_remote_route_disabled_no_hint(exporter: types.ModuleType) -> None:
    exporter.ADD_LOCAL_ROUTE = True
    exporter.ADD_REMOTE_ROUTE_IF_NEEDED = False
    exporter.pyads.Connection.side_effect = [
        _failing_plc(_route_error()),
        _failing_plc(_route_error()),
    ]
    with pytest.raises(RuntimeError, match="重试错误") as err:
        exporter._open_connection(_env(exporter, "Windows"))
    assert "人工确认" not in str(err.value)
    exporter.pyads.add_route_to_plc.assert_not_called()


# ---------- 8. 重连后的新连接必须被释放 ----------
class CloseablePLC:
    def __init__(self, payload: bytes, count: int) -> None:
        self.payload = payload
        self.count = count
        self.closed = False

    def read(self, group: int, offset: int, datatype: object, *, return_ctypes: bool) -> object:
        assert return_ctypes
        length = ctypes.sizeof(datatype)
        if group == 0xF00F:
            result = struct.pack("<II", self.count, len(self.payload))
        else:
            result = self.payload[offset : offset + length]
        return datatype.from_buffer_copy(result)

    def read_state(self) -> tuple[int, int]:
        return (5, 0)

    def close(self) -> None:
        self.closed = True


def _entry(name: str) -> bytes:
    raw = name.encode()
    body = raw + b"\0" + b"INT\0" + b"\0"
    header = bytearray(30)
    struct.pack_into("<I", header, 0, len(header) + len(body))
    struct.pack_into("<HHH", header, 24, len(raw), 3, 0)
    return bytes(header) + body


def test_reconnected_connection_is_released(
    exporter: types.ModuleType, tmp_path: Path
) -> None:
    payload = _entry("A.x")
    dead = MagicMock()
    dead.read.side_effect = RuntimeError("connection lost")
    dead.read_state.side_effect = RuntimeError("dead")
    fresh = CloseablePLC(payload, 1)
    exporter.TRY_FULL_TABLE_FIRST = True
    exporter.CHUNK_SIZES = (64,)

    assert exporter.export_symbols(
        dead, tmp_path / "s.txt", env=_env(exporter, "Linux"), reconnect=lambda: fresh
    ) == 1
    assert fresh.closed  # 重连创建的连接归工具所有，结束后释放
    dead.close.assert_called()  # 失效旧连接在替换时被关闭


def test_caller_connection_not_closed_by_default(
    exporter: types.ModuleType, tmp_path: Path
) -> None:
    plc = CloseablePLC(_entry("A.x"), 1)
    exporter.CHUNK_SIZES = (64,)
    assert exporter.export_symbols(
        plc, tmp_path / "s.txt", env=_env(exporter, "Linux")
    ) == 1
    assert not plc.closed  # 外部传入连接归调用方所有

    plc2 = CloseablePLC(_entry("A.x"), 1)
    assert exporter.export_symbols(
        plc2, tmp_path / "s2.txt", env=_env(exporter, "Linux"), owns_connection=True
    ) == 1
    assert plc2.closed  # 显式移交所有权后才由工具释放


def test_failed_reconnect_stops_using_dead_connection(
    exporter: types.ModuleType, tmp_path: Path
) -> None:
    dead = MagicMock()
    dead.read.side_effect = RuntimeError("connection lost")
    dead.read_state.side_effect = RuntimeError("dead")
    exporter.TRY_FULL_TABLE_FIRST = True
    exporter.CHUNK_SIZES = (64, 32)

    def reconnect() -> MagicMock:
        raise RuntimeError("plc unreachable")

    with pytest.raises(RuntimeError, match="所有已启用的枚举策略"):
        exporter.export_symbols(
            dead, tmp_path / "s.txt", env=_env(exporter, "Linux"), reconnect=reconnect
        )
    # 失效连接上只发生过整表方案的首次读取，chunk 两档均未再发送请求
    assert dead.read.call_count == 1


# ---------- 9/10. FREAD 短读、EOF 与中途错误 ----------
class ScriptedFileConn:
    """按脚本返回 FREAD 数据的文件服务连接。"""

    def __init__(self, script: list[bytes | Exception], content: bytes) -> None:
        self.script = script
        self.content = content
        self.handles: dict[int, int] = {}
        self.closed_handles: list[int] = []
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
            self.handles[1] = 0
            assert read_type is not None
            return read_type.from_buffer_copy(struct.pack("<I", 1))
        if group == 121:  # FCLOSE
            self.handles.pop(offset, None)
            self.closed_handles.append(offset)
            return None
        if group == 133:  # FFILEFIND：报告一个候选文件后结束
            if offset == 1:
                self._pending = ["port_801.tmc"]
            if not self._pending:
                err = RuntimeError("no more files")
                err.err_code = 1804  # type: ignore[attr-defined]
                raise err
            name = self._pending.pop(0).encode("cp1252")
            data = bytearray(322)
            struct.pack_into("<II", data, 0, offset + 1, 0)
            data[48 : 48 + len(name)] = name
            assert read_type is not None
            return read_type.from_buffer_copy(bytes(data))
        raise AssertionError(f"unexpected group {group}")

    def fread(self, handle: int, size: int) -> bytes:
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _tpy(names: list[str]) -> bytes:
    symbols = "".join(f"<Symbol><Name>{n}</Name></Symbol>" for n in names)
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        f"<TcModuleClass><Symbols>{symbols}</Symbols></TcModuleClass>"
    ).encode()


def test_fread_short_read_then_clean_eof(
    exporter: types.ModuleType, tmp_path: Path
) -> None:
    exporter.CHUNK_SIZES = ()
    content = _tpy(["MAIN.a", "MAIN.b"])
    # 三次短读 + b"" EOF；任何一段都不能被当作错误
    conn = ScriptedFileConn(
        [content[:5], content[5:20], content[20:], b""], content
    )
    output = tmp_path / "symbols.txt"
    count = exporter.export_symbols(
        MagicMock(), output, env=_env(exporter, "Linux"),
        file_service_factory=lambda: conn,
    )
    assert count == 2
    assert output.read_text(encoding="utf-8").splitlines()[-2:] == ["MAIN.a", "MAIN.b"]
    assert conn.closed_handles == [1] and conn.conn_closed


def test_fread_mid_error_is_not_eof(
    exporter: types.ModuleType, tmp_path: Path
) -> None:
    exporter.CHUNK_SIZES = ()
    content = _tpy(["MAIN.a", "MAIN.b"])
    err = RuntimeError("ads timeout")
    err.err_code = 1861  # type: ignore[attr-defined]
    conn = ScriptedFileConn([content[:10], err], content)
    output = tmp_path / "symbols.txt"
    output.write_text("previous\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="所有已启用的枚举策略") as exc:
        exporter.export_symbols(
            MagicMock(), output, env=_env(exporter, "Linux"),
            file_service_factory=lambda: conn,
        )
    assert "ads timeout" in str(exc.value)  # 错误如实上报，不当作 EOF
    assert output.read_text(encoding="utf-8") == "previous\n"  # 不覆盖旧文件
    assert conn.closed_handles == [1]  # 句柄在异常路径同样释放
    assert not list(tmp_path.glob("ads_remote_*"))  # 临时文件已删除


def test_fread_invalid_handle_fails(exporter: types.ModuleType, tmp_path: Path) -> None:
    exporter.CHUNK_SIZES = ()
    err = RuntimeError("invalid handle")
    err.err_code = 0x706  # type: ignore[attr-defined]
    conn = ScriptedFileConn([err], b"")
    with pytest.raises(RuntimeError, match="invalid handle"):
        exporter.export_symbols(
            MagicMock(), tmp_path / "s.txt", env=_env(exporter, "Linux"),
            file_service_factory=lambda: conn,
        )


# ---------- 11. 真流式解析：禁止整表加载 ----------
def test_parse_is_streaming_without_full_load(
    exporter: types.ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "big.tmc"
    path.write_bytes(_tpy([f"GVL.v{i}" for i in range(100)]))

    def no_full_load(self: object, *args: object) -> bytes:
        raise AssertionError("不允许整表加载")

    monkeypatch.setattr(Path, "read_bytes", no_full_load)
    names = exporter._parse_tpy_tmc_names(path)
    assert names == [f"GVL.v{i}" for i in range(100)]


def test_parse_respects_size_limit_config(exporter: types.ModuleType) -> None:
    assert exporter.MAX_TPY_FILE_BYTES > 0  # 大小限制配置保留


# ---------- 12. 数量一致但名称不同：不得标记内容一致 ----------
def test_same_count_different_names_not_content_matched(
    exporter: types.ModuleType, tmp_path: Path
) -> None:
    verify = exporter._verify_file_names
    # 仅数量一致 → count_matched，不是 content_matched
    assert verify(["A", "B"], 2, None) == "count_matched"
    # 有运行时名称证据且集合不同 → unverified
    assert verify(["A", "B"], 2, ["A", "C"]) == "unverified"
    # 集合一致 → content_matched
    assert verify(["A", "B"], 2, ["B", "A"]) == "content_matched"
    # 无运行时信息 → unverified
    assert verify(["A"], None, None) == "unverified"


def test_count_matched_header_in_output(
    exporter: types.ModuleType, tmp_path: Path
) -> None:
    exporter.CHUNK_SIZES = ()
    content = _tpy(["MAIN.x", "MAIN.y"])  # 与运行时数量一致但名称无法证实
    conn = ScriptedFileConn([content, b""], content)

    class CountPLC:
        def read(self, group: int, offset: int, datatype: object, *, return_ctypes: bool) -> object:
            assert group == 0xF00F
            return datatype.from_buffer_copy(struct.pack("<II", 2, 100))

        def read_state(self) -> tuple[int, int]:
            return (5, 0)

    output = tmp_path / "symbols.txt"
    outcomes: list = []
    assert exporter.export_symbols(
        CountPLC(), output, env=_env(exporter, "Linux"),
        file_service_factory=lambda: conn, outcomes=outcomes,
    ) == 2
    text = output.read_text(encoding="utf-8")
    assert "verification=count_matched" in text
    assert "content_matched" not in text
