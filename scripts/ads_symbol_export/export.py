"""独立 TwinCAT ADS 符号名导出工具。

按优先级依次尝试四种方案，成功即停：
1. ADS 整表读取（运行时符号表）
2. ADS Chunk 分段读取（运行时符号表）
3. ADS 文件服务读取 PLC 上已有 TPY/TMC（source=file，非运行时表）
4. ADS OCX 枚举（实验性，仅 Windows 客户端）
"""

from __future__ import annotations

import ctypes
import importlib.util
import logging
import platform
import shutil
import struct
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pyads

# ==================== 全部用户配置 ====================
PLC_IP = "192.168.1.10"
PLC_AMS_NET_ID = "192.168.1.10.1.1"
TWINCAT_VERSION = 2  # 2: ADS 801；3: ADS 851
PLC_AMS_PORT = None  # None 自动按版本选择；也可显式填写端口

# 本机 ADS 路由。True 仅表示"允许在确有必要时修复本地路由"，
# 不表示每次启动强制添加：先尝试连接，仅当失败原因明确是本地路由
# 缺失（且当前平台需要）时才修复一次。Linux AdsLib 按 IP 直连，
# 通常无需显式路由，该配置在 Linux 下不产生任何修改。
ADD_LOCAL_ROUTE = True

# 远端（PLC 端）路由属于高风险配置修改。pyads/AdsLib 没有可靠的远端
# 路由查询接口，无法证明远端路由确实缺失，因此本工具绝不自动修改 PLC
# 路由表；开启后仅在最可能缺失时给出人工确认提示。
ADD_REMOTE_ROUTE_IF_NEEDED = False
LOCAL_AMS_NET_ID = ""  # 需要远端路由时填写本机 AMS Net ID，如 "192.168.1.20.1.1"
LOCAL_HOST_NAME = ""  # PLC 路由表中显示的本机名称
PLC_USERNAME = "Administrator"
PLC_PASSWORD = ""  # 可在运行前填写，勿提交真实密码
REMOTE_ROUTE_NAME = "ads-symbol-export"

ADS_TIMEOUT_MS = 5000
RECONNECT_ATTEMPTS = 1  # 连接失效后的最大重连次数；禁止无限重试
TRY_FULL_TABLE_FIRST = True  # PLC 内存风险：部分机型会溢出；可设 False 禁用
CHUNK_SIZES = (4096, 1024, 256)  # 整表失败后按块大小依次尝试
# 若 PLC 拒绝非零 IndexOffset，所有方案均会失败并报告，而非伪造成功
MAX_SYMBOL_BYTES = 8 * 1024 * 1024 * 1024  # 仅用于防范异常长度，不会一次性分配
OUTPUT_FILENAME = "ads_symbols.txt"  # 输出到运行命令时的当前工作目录
TEXT_ENCODING = "utf-8"  # 符号名字节解码；老设备可改 "cp1252"
LOG_EVERY = 1000
CHECK_HOST_ENVIRONMENT = True  # 启动时检测客户端环境并决定方案可用性

# 方案 3：ADS 文件服务（TwinCAT System Service，AMS port 10000）。
# 只读取 PLC 上已存在的文件，绝不写入。
FILE_SERVICE_ENABLED = True
TPY_SEARCH_PATHS: tuple[str, ...] = ()  # 追加远端目录或 .tpy/.tmc 绝对路径
FILE_READ_CHUNK = 64 * 1024  # 远端文件分段读取块大小
MAX_TPY_FILE_BYTES = 512 * 1024 * 1024  # 远端文件长度安全上限
FILE_FIND_MAX_ENTRIES = 4096  # 远端目录枚举条数上限，防止异常服务端无限返回
REMOTE_PATH_ENCODING = "cp1252"  # 远端 Windows 文件路径字节编码

# 方案 4：ADS OCX 枚举（实验性，仅 Windows 客户端；Linux 自动跳过）。
# 注意：OCX 内部可能同样是整表读取，且公开文档未能证实 AdsEnumSymbols 的
# 精确调用约定；本方案标记为实验性，真实设备使用前需用本机类型库核对。
OCX_ENABLED = True
OCX_PROGID = "TcAdsOcx.AdsOcxCtrl"  # 须与本机注册的 OCX ProgID 一致
OCX_MAX_SYMBOLS = 200000  # 枚举数量安全上限，防止异常组件无限返回
# ====================================================

_LOG = logging.getLogger(__name__)
_SYMBOL_INFO_GROUP = 0xF00F
_SYMBOL_DATA_GROUP = 0xF00B
_SYMBOL_HEADER_SIZE = 30

# TwinCAT System Service（文件服务），独立于 PLC Runtime 端口。
_SYSSERV_PORT = 10000
_FOPEN = 120
_FCLOSE = 121
_FREAD = 122
_FFILEFIND = 133
_FOPEN_READ = 0x01
_FOPEN_BINARY = 0x10
_FOPEN_ENSURE_DIR = 0x40
# 与官方 adstool "file read" 一致：READ | BINARY | ENSURE_DIR
_FOPEN_READ_FLAGS = _FOPEN_READ | _FOPEN_BINARY | _FOPEN_ENSURE_DIR
_FFILEFIND_GENERIC = 1
_FFIND_END = 1804
_FFIND_DATA_SIZE = 322
_DIR_ATTR = 0x10


def _fread_exact(conn: Any, handle: int, size: int) -> bytes:
    """FREAD 适配：返回实际读取的字节；b"" 表示 EOF。

    依据 Beckhoff 官方 adstool（ADS 仓库 AdsTool/main.cpp, RunFile）：
    读取循环为 do { Read(...); } while (bytesRead > 0)，即 EOF 以"成功
    且 bytesRead == 0"表示；短读属正常；任何 ADS 错误都是失败而非 EOF。

    pyads 高层 Connection.read 不暴露 bytes_read，因此真实连接走
    pyads 已加载 AdsLib 的底层 AdsSyncReadWriteReqEx2（同一通信栈，
    仅补充 bytes_read 出口参数）；测试桩可实现 fread(handle, size)。
    """
    adr = getattr(conn, "_adr", None)
    port = getattr(conn, "_port", None)
    if port is not None and hasattr(adr, "amsAddrStruct"):
        from pyads import pyads_ex

        buf = (pyads.PLCTYPE_BYTE * size)()
        bytes_read = ctypes.c_ulong(0)
        err = pyads_ex._adsDLL.AdsSyncReadWriteReqEx2(
            port,
            ctypes.pointer(adr.amsAddrStruct()),
            ctypes.c_ulong(_FREAD),
            ctypes.c_ulong(handle),
            ctypes.c_ulong(size),
            ctypes.pointer(buf),
            ctypes.c_ulong(0),
            None,
            ctypes.pointer(bytes_read),
        )
        if err:
            raise pyads.ADSError(err)
        return bytes(buf[: bytes_read.value])
    fread = getattr(conn, "fread", None)
    if callable(fread):
        return bytes(fread(handle, size))
    raise RuntimeError("无法获取 FREAD 实际字节数：连接对象缺少底层接口")


# ==================== 环境检测 ====================
@dataclass
class StaticEnvironment:
    """不连接 PLC 即可确定的客户端环境；动态能力见 _probe_* 函数。"""

    os_name: str
    arch: str
    python_bits: int
    pyads_version: str | None
    adslib_available: bool
    adstool: str | None
    twincat_version: int
    ams_net_id: str
    ads_port: int
    com_module_available: bool
    ocx_progid_registered: bool | None  # None 表示当前平台无法检测

    def ocx_possible(self) -> tuple[bool, str]:
        """判断 OCX 方案在当前客户端是否值得尝试。"""
        if self.os_name != "Windows":
            return False, f"客户端系统 {self.os_name} 不支持 ADS OCX，跳过"
        if not self.com_module_available:
            return False, "缺少 pywin32（pip install pywin32）"
        if self.ocx_progid_registered is not True:
            return False, f"OCX ProgID {OCX_PROGID!r} 未在本机注册"
        return True, ""


def _detect_ocx_registration() -> bool | None:
    """仅 Windows 查询注册表；不执行任何注册或写操作。"""
    if platform.system() != "Windows":
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, OCX_PROGID):
            return True
    except OSError:
        return False
    except ImportError:
        return None


def _com_module_available() -> bool:
    if platform.system() != "Windows":
        return False
    return importlib.util.find_spec("win32com.client") is not None


def _default_ads_port() -> int:
    return PLC_AMS_PORT if PLC_AMS_PORT is not None else (801 if TWINCAT_VERSION == 2 else 851)


def _inspect_environment() -> StaticEnvironment:
    """静态环境检测：不连接 PLC、不修改系统，只读取客户端事实。"""
    host = platform.system()
    env = StaticEnvironment(
        os_name=host,
        arch=platform.machine(),
        python_bits=struct.calcsize("P") * 8,
        pyads_version=getattr(pyads, "__version__", None),
        adslib_available=getattr(pyads, "Connection", None) is not None,
        adstool=shutil.which("adstool"),
        twincat_version=TWINCAT_VERSION,
        ams_net_id=PLC_AMS_NET_ID,
        ads_port=_default_ads_port(),
        com_module_available=_com_module_available(),
        ocx_progid_registered=_detect_ocx_registration(),
    )
    _LOG.info("客户端系统：%s %s, Python %d 位", env.os_name, env.arch, env.python_bits)
    _LOG.info("pyads=%s, adstool=%s", env.pyads_version or "缺失", env.adstool or "未安装")
    _LOG.info("目标：TwinCAT %s, NetID %s, ADS port %s",
              env.twincat_version, env.ams_net_id, env.ads_port)
    possible, reason = env.ocx_possible()
    _LOG.info("ADS OCX：%s", "可尝试（实验性）" if possible else reason)
    # 不用客户端系统推断 PLC 操作系统；远端信息只来自 ADS 动态探测。
    return env


def _probe_runtime(plc: pyads.Connection) -> dict[str, Any]:
    """动态检测：需要已建立的 PLC 连接。"""
    info: dict[str, Any] = {}
    try:
        name, version = plc.read_device_info()
        info["device_name"] = name
        info["device_version"] = getattr(version, "version", version)
    except Exception as exc:
        info["device_info_error"] = f"{type(exc).__name__}: {exc}"
    try:
        info["symbol_count"], info["symbol_bytes"] = _read_symbol_table_info(plc)
    except Exception as exc:
        info["symbol_info_error"] = f"{type(exc).__name__}: {exc}"
    return info


def _probe_file_service(factory: Callable[[], Any]) -> tuple[bool, str]:
    """动态检测：ADS 文件服务是否可访问；成功建立即关闭。"""
    conn = None
    try:
        conn = factory()
        conn.read_state()
        return True, ""
    except Exception as exc:
        return False, f"ADS 文件服务不可达：{type(exc).__name__}: {exc}"
    finally:
        _safe_close(conn)


# ==================== 方案结果建模 ====================
@dataclass
class StrategyOutcome:
    name: str
    ok: bool
    source: str = "runtime"  # runtime | file | ocx-experimental
    count: int = 0
    # 验证等级：runtime_verified | content_matched | count_matched | unverified
    verification: str = "runtime_verified"
    reason: str = ""

    def describe(self) -> str:
        if self.ok:
            return (f"{self.name}: 成功, source={self.source}, count={self.count}, "
                    f"verification={self.verification}")
        return f"{self.name}: {self.reason}"


class _ConnectionManager:
    """统一管理当前实际 ADS 连接的所有权与重连。

    - 外部传入的初始连接默认归调用方所有，本管理器不在结束时关闭；
      但连接失效被替换时会先安全关闭旧连接（失效连接不存在"保留"价值）。
    - 由 reconnect 工厂创建的新连接归本管理器所有，结束时必须释放。
    - owns_initial=True（main 入口）时初始连接同样由管理器负责释放，
      保证成功、失败或异常路径下当前实际连接都被正确关闭。
    - 重连次数受 RECONNECT_ATTEMPTS 限制，重连后必须 read_state 验证。
    """

    def __init__(
        self,
        plc: Any,
        reconnect: Callable[[], Any] | None = None,
        owns_initial: bool = False,
    ) -> None:
        self.current = plc
        self._reconnect = reconnect
        self._owned = owns_initial
        self._attempts_left = RECONNECT_ATTEMPTS

    def usable(self) -> bool:
        return _connection_usable(self.current)

    def recover(self) -> bool:
        """连接失效时尝试恢复；不无休止重试。"""
        if self.usable():
            return True
        _LOG.warning("ADS 连接已断开")
        if self._reconnect is None or self._attempts_left <= 0:
            return False
        self._attempts_left -= 1
        _safe_close(self.current)  # 失效连接先关闭，避免泄漏
        try:
            candidate = self._reconnect()
        except Exception as exc:
            _LOG.warning("重连失败: %s", exc)
            return False
        self.current = candidate
        self._owned = True  # 工厂创建的连接归管理器所有
        return self.usable()

    def close(self) -> None:
        """释放归本管理器所有的连接；调用方所有的连接不触碰。"""
        if self._owned:
            _safe_close(self.current)
            self._owned = False


@dataclass
class _RunContext:
    conn: _ConnectionManager
    output: Path
    env: StaticEnvironment
    file_service_factory: Callable[[], Any] | None = None
    ocx_factory: Callable[[], Any] | None = None
    outcomes: list[StrategyOutcome] = field(default_factory=list)


# ==================== ADS 符号表基础读取 ====================
def _read_bytes(plc: pyads.Connection, group: int, offset: int, length: int) -> bytes:
    """有界读取 ADS 字节数组，不调用 get_all_symbols。"""
    if length <= 0:
        return b""
    raw = plc.read(group, offset, pyads.PLCTYPE_BYTE * length, return_ctypes=True)
    data = bytes(raw)
    if len(data) != length:
        raise ValueError(f"ADS 响应长度异常: 期望 {length}, 实际 {len(data)}")
    return data


def _read_symbol_table_info(plc: pyads.Connection) -> tuple[int, int]:
    data = _read_bytes(plc, _SYMBOL_INFO_GROUP, 0, 8)
    count, total = struct.unpack_from("<II", data)
    if count == 0 or total < count * (_SYMBOL_HEADER_SIZE + 3):
        raise ValueError(f"无效点表信息: count={count}, size={total}")
    if total > MAX_SYMBOL_BYTES:
        raise ValueError(f"点表长度 {total} 超过安全上限 {MAX_SYMBOL_BYTES}")
    return count, total


def _parse_symbol_entry(data: bytearray) -> tuple[str, int] | None:
    """解析一个完整的 AdsSymbolEntry；不足一条则保留缓冲区。"""
    if len(data) < _SYMBOL_HEADER_SIZE:
        return None
    length = struct.unpack_from("<I", data)[0]
    if length < _SYMBOL_HEADER_SIZE + 3:
        raise ValueError(f"非法符号记录长度: {length}")
    if length > MAX_SYMBOL_BYTES:
        raise ValueError(f"符号记录长度超限: {length}")
    if length > len(data):
        return None
    # AdsSymbolEntry: entryLength(4) + indexGroup/indexOffset/size/dataType/flags(20)。
    name_len, type_len, comment_len = struct.unpack_from("<HHH", data, 24)
    required = _SYMBOL_HEADER_SIZE + name_len + 1 + type_len + 1 + comment_len + 1
    if required > length:
        raise ValueError(f"符号记录字段长度超过记录总长度: {length}")
    name_raw = bytes(data[_SYMBOL_HEADER_SIZE : _SYMBOL_HEADER_SIZE + name_len])
    name = name_raw.decode(TEXT_ENCODING)
    if not name:
        raise ValueError("收到空符号名")
    if data[_SYMBOL_HEADER_SIZE + name_len] != 0:
        raise ValueError("符号名未以 NUL 结尾")
    return name, length


def _atomic_write_lines(output: Path, lines: list[str], header: tuple[str, ...] = ()) -> None:
    """写入临时文件后原子替换；失败不触碰既有输出。"""
    temp = output.with_name(output.name + ".partial")
    try:
        with temp.open("w", encoding="utf-8", newline="\n") as handle:
            for line in header:
                handle.write("# " + line + "\n")
            for line in lines:
                handle.write(line + "\n")
        temp.replace(output)
    except Exception:
        temp.unlink(missing_ok=True)
        raise


# ==================== 方案 1：ADS 整表 ====================
def _export_full_table(plc: pyads.Connection, output: Path) -> int:
    """一次性读取完整符号表并解析；不使用 get_all_symbols。

    pyads.get_all_symbols 按 Windows-1252 解码符号名，无法处理 UTF-8 变量名，
    这里直接读取 0xF00B 原始字节，用与分段方案一致的解析器按 TEXT_ENCODING 解码。
    注意：整表读取可能导致部分 PLC 内存溢出并使 ADS 连接失效，
    调用方必须在失败后重新检查连接，不能假定原连接仍然可用。
    """
    expected, total = _read_symbol_table_info(plc)
    buffer = bytearray(_read_bytes(plc, _SYMBOL_DATA_GROUP, 0, total))
    names: list[str] = []
    while buffer:
        entry = _parse_symbol_entry(buffer)
        if entry is None:
            break
        name, used = entry
        names.append(name)
        del buffer[:used]
    if len(names) != expected or buffer:
        raise ValueError(
            f"整表数据不完整：声明 {expected} 条，解析 {len(names)} 条，剩余 {len(buffer)} 字节"
        )
    _atomic_write_lines(output, names)
    return expected


# ==================== 方案 2：ADS Chunk 分段 ====================
def _export_in_chunks(plc: pyads.Connection, output: Path, chunk_size: int) -> int:
    """使用指定小块大小逐次读取符号表，失败时丢弃当前结果。

    注意：客户端发送小块请求不代表 PLC 内部不构造全表；
    本方案不能声称避免 PLC 端内存溢出。
    """
    count, total = _read_symbol_table_info(plc)
    _LOG.info("PLC symbols: %d, upload bytes: %d", count, total)
    if chunk_size < _SYMBOL_HEADER_SIZE + 3:
        raise ValueError("chunk_size 太小")
    # 以重叠窗口验证 IndexOffset 真的是字节偏移，不接受返回首块的伪支持。
    probe_length = min(64, total - 1, chunk_size - 1)
    if probe_length < 16:
        raise ValueError("点表太短，无法可靠验证分段偏移")
    probe_first = _read_bytes(plc, _SYMBOL_DATA_GROUP, 0, probe_length + 1)
    probe_shifted = _read_bytes(plc, _SYMBOL_DATA_GROUP, 1, probe_length)
    if probe_first[1:] != probe_shifted or probe_first[:probe_length] == probe_shifted:
        raise ValueError("PLC 不支持可靠的 0xF00B 字节偏移读取")
    buffer = bytearray()
    offset = 0
    exported = 0
    # 使用临时文件：未完成或失败时不会覆盖既有输出。
    temp = output.with_name(output.name + ".partial")
    try:
        with temp.open("w", encoding="utf-8", newline="\n") as handle:
            while offset < total:
                length = min(chunk_size, total - offset)
                chunk = _read_bytes(plc, _SYMBOL_DATA_GROUP, offset, length)
                buffer.extend(chunk)
                offset += length
                while len(buffer) >= _SYMBOL_HEADER_SIZE:
                    entry = _parse_symbol_entry(buffer)
                    if entry is None:
                        # 单个记录超过 CHUNK_SIZE 也能跨多次读取。
                        break
                    name, used = entry
                    handle.write(name + "\n")
                    del buffer[:used]
                    exported += 1
                    if exported > count:
                        raise ValueError("符号记录数超过声明数量：设备可能不支持分段偏移读取")
                    if LOG_EVERY > 0 and exported % LOG_EVERY == 0:
                        _LOG.info("已导出 %d / %d", exported, count)
        if exported != count or buffer:
            raise ValueError(
                f"符号流不完整：声明 {count} 条，解析 {exported} 条，剩余 {len(buffer)} 字节；"
                "可能是 PLC 不支持 ADSIGRP_SYM_UPLOAD 分段偏移读取"
            )
        temp.replace(output)
    except Exception:
        temp.unlink(missing_ok=True)
        raise
    return exported


# ==================== 方案 3：ADS 文件服务 ====================
class AdsFileService:
    """TwinCAT System Service (AMS port 10000) 只读文件客户端。

    协议依据 Beckhoff 官方 adslib（github.com/Beckhoff/ADS, AdsLib/AdsFile.cpp）：
    FOPEN=120 / FCLOSE=121 / FREAD=122 / FFILEFIND=133；FFILEFIND 返回 1804 表示枚举结束。
    仅使用打开/读取/关闭/查找，绝不调用 FWRITE/FDELETE/FRENAME。
    """

    def __init__(self, conn: Any) -> None:
        self._conn = conn
        self._open_handles: list[int] = []

    def open(self, path: str) -> int:
        payload = path.encode(REMOTE_PATH_ENCODING)
        raw = self._conn.read_write(
            _FOPEN, _FOPEN_READ_FLAGS,
            pyads.PLCTYPE_BYTE * 4, payload, pyads.PLCTYPE_BYTE * len(payload),
            return_ctypes=True,
        )
        handle = struct.unpack("<I", bytes(raw))[0]
        self._open_handles.append(handle)
        return handle

    def close(self, handle: int) -> None:
        try:
            self._conn.read_write(_FCLOSE, handle, None, b"", pyads.PLCTYPE_BYTE * 0)
        finally:
            if handle in self._open_handles:
                self._open_handles.remove(handle)

    def read_chunk(self, handle: int, size: int) -> bytes:
        """返回实际读取的字节；b"" 是唯一合法的 EOF 信号（见 _fread_exact）。"""
        return _fread_exact(self._conn, handle, size)

    def find(self, pattern: str) -> list[tuple[str, bool]]:
        """枚举匹配远端路径的文件；返回 (名称, 是否目录)，条数受上限约束。"""
        entries: list[tuple[str, bool]] = []
        payload = pattern.encode(REMOTE_PATH_ENCODING)
        offset = _FFILEFIND_GENERIC
        write: tuple[bytes, Any] = (payload, pyads.PLCTYPE_BYTE * len(payload))
        while True:
            if len(entries) >= FILE_FIND_MAX_ENTRIES:
                raise ValueError(f"远端目录枚举超过上限 {FILE_FIND_MAX_ENTRIES} 条")
            try:
                raw = self._conn.read_write(
                    _FFILEFIND, offset,
                    pyads.PLCTYPE_BYTE * _FFIND_DATA_SIZE,
                    write[0], write[1],
                    return_ctypes=True,
                )
            except Exception as exc:
                if getattr(exc, "err_code", None) == _FFIND_END:
                    return entries
                raise
            data = bytes(raw)
            if len(data) != _FFIND_DATA_SIZE:
                raise ValueError(f"FFILEFIND 响应长度异常: {len(data)}")
            offset, attrs = struct.unpack_from("<II", data)
            name_raw = data[48:308].split(b"\0", 1)[0]
            entries.append((name_raw.decode(REMOTE_PATH_ENCODING), bool(attrs & _DIR_ATTR)))
            write = (b"", pyads.PLCTYPE_BYTE * 0)

    def close_all(self) -> None:
        for handle in list(self._open_handles):
            try:
                self.close(handle)
            except Exception as exc:  # 清理阶段只记录
                _LOG.warning("关闭远端文件句柄 %d 失败: %s", handle, exc)


def _default_search_dirs() -> tuple[str, ...]:
    if TWINCAT_VERSION == 3:
        # TC3 PLC 实例的运行时 TMC 位于 Boot\Plc\port_<adsport>.tmc。
        return (r"C:\TwinCAT\3.1\Boot\Plc", r"C:\TwinCAT\3.1\Boot")
    return (r"C:\TwinCAT\Boot",)


def _is_symbol_file(name: str) -> bool:
    return name.lower().endswith((".tpy", ".tmc"))


def _find_remote_symbol_files(fs: AdsFileService) -> list[str]:
    """按默认目录与配置路径查找 TPY/TMC；服务不可用或目录不存在时记录原因。"""
    candidates: list[str] = []
    problems: list[str] = []
    for entry in (*_default_search_dirs(), *TPY_SEARCH_PATHS):
        if _is_symbol_file(entry):
            candidates.append(entry)
            continue
        try:
            for name, is_dir in fs.find(entry + "\\*"):
                if not is_dir and _is_symbol_file(name):
                    candidates.append(entry + "\\" + name)
        except Exception as exc:
            problems.append(f"{entry}: {type(exc).__name__}: {exc}")
    if problems:
        _LOG.info("部分远端目录不可枚举：%s", "; ".join(problems))
    return candidates


def _download_remote_file(fs: AdsFileService, path: str, dest_dir: Path) -> Path:
    """分段下载远端文件到本地临时文件，不一次性加载全部内容。

    EOF 仅以 read_chunk 返回 b"" 判定（adstool 官方语义）；
    通信错误、句柄失效、权限错误等异常一律向上抛出使当前文件失败，
    绝不允许把任意异常当作 EOF。
    """
    handle = fs.open(path)
    downloaded = 0
    temp = tempfile.NamedTemporaryFile(  # noqa: SIM115 需要明确删除时机
        mode="wb", prefix="ads_remote_", suffix=".tmp", dir=dest_dir, delete=False
    )
    try:
        with temp:
            while True:
                if downloaded + FILE_READ_CHUNK > MAX_TPY_FILE_BYTES:
                    raise ValueError(f"远端文件超过安全上限 {MAX_TPY_FILE_BYTES} 字节")
                chunk = fs.read_chunk(handle, FILE_READ_CHUNK)
                if not chunk:
                    break  # 正常 EOF
                temp.write(chunk)
                downloaded += len(chunk)
    except Exception:
        Path(temp.name).unlink(missing_ok=True)
        raise
    finally:
        fs.close(handle)
    if downloaded == 0:
        Path(temp.name).unlink(missing_ok=True)
        raise ValueError(f"远端文件为空或不可读: {path}")
    return Path(temp.name)


def _parse_tpy_tmc_names(path: Path) -> list[str]:
    """真正流式解析 TPY/TMC，提取 <Symbol> 的全限定名称。

    ET.iterparse 直接从文件对象增量读取，不整表加载；XML 声明编码与
    命名空间由解析器处理（命名空间按 local-name 匹配）。名称取 <Name>
    子元素文本，兼容 name 属性形式。elem.clear() 及时释放已解析元素。
    文件不完整、编码异常必然抛错，绝不靠删尾部字节掩盖传输问题。
    """
    import xml.etree.ElementTree as ET

    names: list[str] = []
    try:
        for _event, elem in ET.iterparse(str(path), events=("end",)):
            if elem.tag.rsplit("}", 1)[-1] != "Symbol":
                continue
            name = elem.get("Name") or elem.get("name")
            if not name:
                for child in elem:
                    if child.tag.rsplit("}", 1)[-1] == "Name" and child.text:
                        name = child.text
                        break
            if name and name.strip():
                names.append(name.strip())
            elem.clear()
    except ET.ParseError as exc:
        raise ValueError(f"TPY/TMC 解析失败（文件不完整或编码异常）: {exc}") from exc
    if not names:
        raise ValueError("文件中未找到任何 <Symbol> 变量名")
    # 保序去重（复杂类型的成员展开可能重复）。
    return list(dict.fromkeys(names))


# 文件来源结果的验证等级（语义严格区分，数量一致不代表内容一致）。
VERIFICATION_RUNTIME = "runtime_verified"  # 运行时 ADS 符号枚举完整性校验通过
VERIFICATION_CONTENT_MATCHED = "content_matched"  # 有名称集合一致的实际证据
VERIFICATION_COUNT_MATCHED = "count_matched"  # 仅数量一致，不声称内容一致
VERIFICATION_UNVERIFIED = "unverified"  # 无法证明文件与运行时一致


def _verify_file_names(
    names: list[str],
    runtime_count: int | None,
    runtime_names: list[str] | None,
) -> str:
    """评估文件符号与运行时的一致性等级。

    content_matched 需要调用方提供运行时名称集合的证据；本工具不为提升
    验证等级再次触发高风险的运行时整表读取，因此通常只能达到
    count_matched 或 unverified。
    """
    if runtime_names is not None:
        if set(names) == set(runtime_names):
            return VERIFICATION_CONTENT_MATCHED
        return VERIFICATION_UNVERIFIED
    if runtime_count is not None and len(names) == runtime_count:
        return VERIFICATION_COUNT_MATCHED
    return VERIFICATION_UNVERIFIED


def _export_via_file_service(
    factory: Callable[[], Any],
    output: Path,
    runtime_count: int | None = None,
    runtime_names: list[str] | None = None,
) -> tuple[int, str]:
    """通过 ADS 文件服务读取 PLC 上已有 TPY/TMC 并解析变量名。

    返回 (数量, 验证等级)。文件来源结果绝不宣称为运行时完整符号表；
    验证等级语义见 _verify_file_names。某个候选文件失败只记录原因并
    尝试下一个，不提前终止整个方案。
    """
    conn = factory()
    try:
        fs = AdsFileService(conn)
        try:
            candidates = _find_remote_symbol_files(fs)
            if not candidates:
                raise FileNotFoundError("远端未发现 TPY/TMC 文件")
            last_error: Exception | None = None
            for remote in candidates:
                try:
                    local = _download_remote_file(fs, remote, output.parent)
                    try:
                        names = _parse_tpy_tmc_names(local)
                    finally:
                        local.unlink(missing_ok=True)
                except Exception as exc:
                    last_error = exc
                    _LOG.info("远端文件不可用 %s: %s: %s", remote, type(exc).__name__, exc)
                    continue
                verification = _verify_file_names(names, runtime_count, runtime_names)
                _atomic_write_lines(
                    output,
                    names,
                    header=(
                        "source=file (ADS 文件服务读取的 TPY/TMC，非运行时符号表)",
                        f"remote={remote}",
                        f"verification={verification}"
                        + ("" if runtime_count is None else f" (runtime_count={runtime_count})"),
                    ),
                )
                if verification != VERIFICATION_CONTENT_MATCHED:
                    _LOG.warning("文件来源结果验证等级：%s", verification)
                return len(names), verification
            raise RuntimeError(f"所有候选文件均不可用：{last_error}")
        finally:
            fs.close_all()
    finally:
        _safe_close(conn)


# ==================== 方案 4：ADS OCX（实验性） ====================
def _ocx_default_factory() -> Any:
    """创建本机注册的 ADS OCX COM 对象；仅 Windows 调用。"""
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    return win32com.client.Dispatch(OCX_PROGID)


def _export_via_ocx(factory: Callable[[], Any], output: Path) -> int:
    """通过 ADS OCX 的 AdsEnumSymbols 枚举变量名（实验性）。

    公开文档未能证实 AdsEnumSymbols 的精确签名；这里按“首次 True / 后续
    False，返回空串或 None 表示结束”的常见枚举约定实现，并在枚举数量超过
    OCX_MAX_SYMBOLS 时中止而不是产出截断结果。真实设备使用前必须用本机
    OCX 类型库（win32com makepy）核对签名。OCX 内部是否整表读取未知，
    因此客户端逐条收到结果不代表 PLC 端逐条发送。
    """
    obj = factory()
    try:
        enum = getattr(obj, "AdsEnumSymbols", None)
        if not callable(enum):
            raise RuntimeError(f"OCX 对象 {OCX_PROGID!r} 不提供 AdsEnumSymbols")
        names: list[str] = []
        first = True
        while True:
            if len(names) >= OCX_MAX_SYMBOLS:
                raise ValueError(f"OCX 枚举超过安全上限 {OCX_MAX_SYMBOLS}，结果不可信")
            raw = enum(first)
            first = False
            if raw is None:
                break
            name = str(raw).strip()
            if not name:
                break  # 空串表示枚举结束
            names.append(name)
        if not names:
            raise ValueError("OCX 未枚举到任何符号")
        _atomic_write_lines(
            output,
            names,
            header=(
                "source=ocx-experimental (ADS OCX 枚举，完整性未经运行时校验)",
                "verified=False",
            ),
        )
        return len(names)
    finally:
        _release_com(obj)


def _release_com(obj: Any) -> None:
    try:
        close = getattr(obj, "close", None) or getattr(obj, "Close", None)
        if callable(close):
            close()
    except Exception as exc:
        _LOG.warning("关闭 OCX 连接失败: %s", exc)
    try:
        import pythoncom

        pythoncom.CoUninitialize()
    except Exception:
        pass


# ==================== 连接生命周期 ====================
def _safe_close(conn: Any) -> None:
    if conn is None:
        return
    try:
        conn.close()
    except Exception as exc:
        _LOG.warning("关闭 ADS 连接失败: %s", exc)


def _connection_usable(plc: Any) -> bool:
    read_state = getattr(plc, "read_state", None)
    if not callable(read_state):
        return True  # 无状态接口的测试桩视为可用
    try:
        read_state()
        return True
    except Exception:
        return False


def _default_file_service_factory(plc: Any) -> Callable[[], Any] | None:
    """从运行时连接推导 System Service(10000) 连接工厂；测试桩返回 None。"""
    net_id = getattr(plc, "ams_net_id", None)
    ip = getattr(plc, "ip_address", None)
    if not isinstance(net_id, str) or not isinstance(ip, str):
        return None

    def factory() -> pyads.Connection:
        # 文件服务在 System Service 端口，不能复用 PLC Runtime 端口。
        conn = pyads.Connection(net_id, _SYSSERV_PORT, ip)
        conn.open()
        conn.set_timeout(ADS_TIMEOUT_MS)
        return conn

    return factory


# ==================== 统一方案调度 ====================
def _run_strategies(ctx: _RunContext) -> int:
    """按优先级执行方案，成功即停；每个方案独立管理连接与资源。"""
    outcomes = ctx.outcomes
    env = ctx.env

    def record(name: str, source: str, run: Callable[[], int], available: str = "") -> int | None:
        if available:
            outcomes.append(StrategyOutcome(name, False, source, reason=available))
            return None
        try:
            count = run()
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
            outcomes.append(StrategyOutcome(name, False, source, reason=reason))
            _LOG.warning("方案失败：%s: %s", name, reason)
            return None
        outcomes.append(StrategyOutcome(name, True, source, count=count))
        _LOG.info("方案成功：%s", name)
        return count

    # 方案 1：ADS 整表
    if TRY_FULL_TABLE_FIRST:
        _LOG.warning("先尝试读取完整符号表；已知部分 PLC 存在内存溢出风险")
        result = record("full-table", "runtime",
                        lambda: _export_full_table(ctx.conn.current, ctx.output))
        if result is not None:
            return result
        if not ctx.conn.recover():
            outcomes.append(StrategyOutcome(
                "runtime-reconnect", False, reason="连接断开且无法恢复，跳过运行时方案"))

    # 方案 2：ADS Chunk 分段
    if ctx.conn.usable():
        for chunk_size in CHUNK_SIZES:
            if chunk_size < _SYMBOL_HEADER_SIZE + 3:
                outcomes.append(StrategyOutcome(
                    f"chunk={chunk_size}", False, reason="配置值过小"))
                continue
            result = record(f"chunk={chunk_size}", "runtime",
                            lambda s=chunk_size: _export_in_chunks(ctx.conn.current, ctx.output, s))
            if result is not None:
                return result
            # 区分协议拒绝与连接失效；失效连接不得继续发送请求
            if not ctx.conn.recover():
                outcomes.append(StrategyOutcome(
                    "runtime-reconnect", False, reason="连接断开且无法恢复，停止分段尝试"))
                break
    else:
        outcomes.append(StrategyOutcome("chunk", False, reason="连接不可用，跳过分段方案"))

    # 方案 3：ADS 文件服务（独立 System Service 端口，PLC Runtime 失效也可尝试）
    runtime_count: int | None = None
    if ctx.conn.usable():
        try:
            runtime_count, _ = _read_symbol_table_info(ctx.conn.current)
        except Exception:
            runtime_count = None
    factory = ctx.file_service_factory or _default_file_service_factory(ctx.conn.current)
    unavailable = ""
    if not FILE_SERVICE_ENABLED:
        unavailable = "文件服务方案已在配置中禁用"
    elif factory is None:
        unavailable = "无法从当前连接推导 System Service 地址"
    if not unavailable:
        assert factory is not None
        reachable, unavailable = _probe_file_service(factory)
        if reachable:
            unavailable = ""
    if unavailable:
        outcomes.append(StrategyOutcome("ads-file", False, "file", reason=unavailable))
    else:
        assert factory is not None
        def run_file() -> int:
            count, verification = _export_via_file_service(
                factory, ctx.output, runtime_count=runtime_count)
            outcomes.append(StrategyOutcome(
                "ads-file", True, "file", count=count, verification=verification))
            return count
        try:
            return run_file()
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
            outcomes.append(StrategyOutcome("ads-file", False, "file", reason=reason))
            _LOG.warning("方案失败：ads-file: %s", reason)

    # 方案 4：ADS OCX（实验性，仅 Windows）
    ocx_ok, ocx_reason = env.ocx_possible() if OCX_ENABLED else (False, "OCX 方案已在配置中禁用")
    if not ocx_ok:
        outcomes.append(StrategyOutcome("ocx", False, "ocx-experimental", reason=ocx_reason))
    else:
        ocx_factory = ctx.ocx_factory or _ocx_default_factory
        result = record("ocx", "ocx-experimental",
                        lambda: _export_via_ocx(ocx_factory, ctx.output))
        if result is not None:
            return result

    raise RuntimeError(
        "所有已启用的枚举策略均失败：\n" + "\n".join(o.describe() for o in outcomes)
    )


def export_symbols(
    plc: Any,
    output: Path,
    *,
    env: StaticEnvironment | None = None,
    reconnect: Callable[[], Any] | None = None,
    file_service_factory: Callable[[], Any] | None = None,
    ocx_factory: Callable[[], Any] | None = None,
    outcomes: list[StrategyOutcome] | None = None,
    owns_connection: bool = False,
) -> int:
    """先整表、后分段、再文件服务、最后 OCX；成功方案的结果必须校验完整。

    连接所有权：外部传入的 plc 默认归调用方所有，本函数不关闭；
    owns_connection=True 时由本函数负责最终释放当前实际连接。
    reconnect 工厂创建的连接始终归本函数所有并保证释放。
    """
    if not TRY_FULL_TABLE_FIRST and not CHUNK_SIZES \
            and not FILE_SERVICE_ENABLED and not OCX_ENABLED:
        raise ValueError("至少启用一种枚举方案")
    manager = _ConnectionManager(plc, reconnect, owns_initial=owns_connection)
    ctx = _RunContext(
        conn=manager,
        output=output,
        env=env or _inspect_environment(),
        file_service_factory=file_service_factory,
        ocx_factory=ocx_factory,
        outcomes=outcomes if outcomes is not None else [],
    )
    try:
        return _run_strategies(ctx)
    finally:
        manager.close()


# ==================== 连接与入口 ====================
def _is_local_route_error(exc: Exception) -> bool:
    """保守识别"本地路由缺失"：仅明确的端口/路由缺失信号。

    ADS error 6 = Target port not found（本地路由表无目标 NetID）。
    网络不可达、超时、拒绝访问、认证错误等均不匹配，绝不盲目改路由。
    """
    if getattr(exc, "err_code", None) == 6:
        return True
    text = str(exc).lower()
    return "target port not found" in text or "no route" in text


def _try_open_plc(port: int) -> pyads.Connection:
    """按当前已有路由配置建立连接并验证 ADS 服务实际可用。"""
    plc = pyads.Connection(PLC_AMS_NET_ID, port, PLC_IP)
    plc.open()
    try:
        plc.set_timeout(ADS_TIMEOUT_MS)
        plc.read_state()
    except Exception:
        plc.close()
        raise
    return plc


def _open_connection(env: StaticEnvironment | None = None) -> pyads.Connection:
    """先连接、后按需修复本地路由；远端路由绝不自动修改。

    流程：直接连接 → read_state 验证 → 成功立即返回（绝不调用 add_route）。
    仅当失败明确属于本地路由缺失、ADD_LOCAL_ROUTE=True 且当前平台确实
    依赖本地路由（Windows TwinCAT Router；Linux AdsLib 按 IP 直连，跳过）
    时，修复一次本地路由并重试；重试失败同时报告原始与重试错误。
    """
    if TWINCAT_VERSION not in (2, 3):
        raise ValueError("TWINCAT_VERSION 只能为 2 或 3")
    port = _default_ads_port()
    os_name = env.os_name if env is not None else platform.system()
    try:
        return _try_open_plc(port)
    except Exception as first_error:
        if not _is_local_route_error(first_error):
            raise
        if os_name != "Windows":
            # Linux AdsLib 按 IP 直连，本地无需显式路由；路由类错误只能报告。
            raise RuntimeError(
                f"连接失败且疑似路由问题，但 {os_name} 下本工具不修改本地路由："
                f"{type(first_error).__name__}: {first_error}"
            ) from first_error
        if not ADD_LOCAL_ROUTE:
            raise RuntimeError(
                f"连接失败，疑似本地路由缺失；ADD_LOCAL_ROUTE=False，未自动修复："
                f"{type(first_error).__name__}: {first_error}"
            ) from first_error
        _LOG.warning("疑似本地路由缺失，按需添加一次本地路由")
        pyads.add_route(PLC_AMS_NET_ID, PLC_IP)  # 最多一次，绝不重复
        try:
            return _try_open_plc(port)
        except Exception as retry_error:
            hint = (
                "本地路由修复后仍无法连接："
                f"原始错误 {type(first_error).__name__}: {first_error}；"
                f"重试错误 {type(retry_error).__name__}: {retry_error}"
            )
            if ADD_REMOTE_ROUTE_IF_NEEDED and _is_local_route_error(retry_error):
                # pyads/AdsLib 无远端路由查询接口，无法证明 PLC 端路由缺失，
                # 只能提示人工确认，绝不自动修改 PLC 路由表。
                hint += (
                    "；仍疑似路由问题，可能是 PLC 端缺少指向本机的路由。"
                    "请在 TwinCAT 工程或 PLC 上人工确认后手动配置，"
                    "本工具不会自动修改 PLC 远端路由"
                )
            raise RuntimeError(hint) from retry_error


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    env = _inspect_environment() if CHECK_HOST_ENVIRONMENT else None
    destination = Path.cwd() / OUTPUT_FILENAME
    plc = _open_connection(env)
    outcomes: list[StrategyOutcome] = []
    _LOG.info("PLC 动态信息：%s", _probe_runtime(plc))
    # owns_connection=True：当前实际连接（含重连后的新连接）统一由此释放。
    total = export_symbols(
        plc, destination,
        env=env,
        reconnect=lambda: _open_connection(env),
        outcomes=outcomes,
        owns_connection=True,
    )
    for outcome in outcomes:
        _LOG.info("%s", outcome.describe())
    _LOG.info("已写入 %d 个变量名: %s", total, destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
