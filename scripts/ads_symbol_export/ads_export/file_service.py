"""只读 TwinCAT System Service 文件传输和 TPY/TMC 解析。"""

from __future__ import annotations

import ctypes
import logging
import struct
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pyads

from .config import Settings
from .connection import _safe_close
from .runtime import _atomic_write_lines

_LOG = logging.getLogger(__name__)

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
# TcFileFindData：hFile(4) + dwFileAttributes(4) + nReserved1(40) +
# cFileName[260] + unused[14] + nReserved2(2)，与 Beckhoff AdsLib 的
# sizeof(TcFileFindData) 一致。
_FFIND_DATA_SIZE = 324
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
    if port is not None and adr is not None and hasattr(adr, "amsAddrStruct"):
        from pyads import pyads_ex

        # ABI 依据（pyads 3.6.0 锁定版本）：amsAddrStruct() 返回 AmsAddr 持有的
        # 持久 SAmsAddr（非临时副本），与 pyads 自身 adsSyncReadWriteReqEx2
        # 的调用方式一致；仍绑定局部变量保证调用期间引用明确有效。
        ams_struct = adr.amsAddrStruct()
        buf = (pyads.PLCTYPE_BYTE * size)()
        bytes_read = ctypes.c_ulong(0)
        err = pyads_ex._adsDLL.AdsSyncReadWriteReqEx2(
            port,  # adsPortOpenEx 返回的 long
            ctypes.pointer(ams_struct),
            ctypes.c_ulong(_FREAD),
            ctypes.c_ulong(handle),
            ctypes.c_ulong(size),  # readLength
            ctypes.pointer(buf),
            ctypes.c_ulong(0),  # writeLength
            None,
            ctypes.pointer(bytes_read),  # bytesRead 输出
        )
        if err:
            raise pyads.ADSError(err)
        actual = bytes_read.value
        if actual > size:
            # 缓冲区溢出迹象：结果不可信，拒绝而不是截断。
            raise ValueError(f"FREAD bytesRead {actual} 超过缓冲区 {size}")
        return bytes(buf[:actual])
    fread = getattr(conn, "fread", None)
    if callable(fread):
        return bytes(fread(handle, size))
    raise RuntimeError("无法获取 FREAD 实际字节数：连接对象缺少底层接口")


class AdsFileService:
    """TwinCAT System Service (AMS port 10000) 只读文件客户端。

    协议依据 Beckhoff 官方 adslib（github.com/Beckhoff/ADS, AdsLib/AdsFile.cpp）：
    FOPEN=120 / FCLOSE=121 / FREAD=122 / FFILEFIND=133；FFILEFIND 返回 1804 表示枚举结束。
    仅使用打开/读取/关闭/查找，绝不调用 FWRITE/FDELETE/FRENAME。
    """

    def __init__(self, conn: Any, settings: Settings) -> None:
        self._conn = conn
        self._settings = settings
        self._open_handles: list[int] = []

    def open(self, path: str) -> int:
        payload = path.encode(self._settings.REMOTE_PATH_ENCODING)
        raw = self._conn.read_write(
            _FOPEN,
            _FOPEN_READ_FLAGS,
            pyads.PLCTYPE_BYTE * 4,
            payload,
            pyads.PLCTYPE_BYTE * len(payload),
            return_ctypes=True,
        )
        handle = int(struct.unpack("<I", bytes(raw))[0])
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
        payload = pattern.encode(self._settings.REMOTE_PATH_ENCODING)
        offset = _FFILEFIND_GENERIC
        write: tuple[bytes, Any] = (payload, pyads.PLCTYPE_BYTE * len(payload))
        while True:
            if len(entries) >= self._settings.FILE_FIND_MAX_ENTRIES:
                raise ValueError(f"远端目录枚举超过上限 {self._settings.FILE_FIND_MAX_ENTRIES} 条")
            try:
                raw = self._conn.read_write(
                    _FFILEFIND,
                    offset,
                    pyads.PLCTYPE_BYTE * _FFIND_DATA_SIZE,
                    write[0],
                    write[1],
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
            entries.append(
                (name_raw.decode(self._settings.REMOTE_PATH_ENCODING), bool(attrs & _DIR_ATTR))
            )
            write = (b"", pyads.PLCTYPE_BYTE * 0)

    def close_all(self) -> None:
        for handle in list(self._open_handles):
            try:
                self.close(handle)
            except Exception as exc:  # 清理阶段只记录
                _LOG.warning("关闭远端文件句柄 %d 失败: %s", handle, exc)


def _default_search_dirs(settings: Settings) -> tuple[str, ...]:
    if settings.TWINCAT_VERSION == 3:
        # TC3 PLC 实例的运行时 TMC 位于 Boot\Plc\port_<adsport>.tmc。
        return (r"C:\TwinCAT\3.1\Boot\Plc", r"C:\TwinCAT\3.1\Boot")
    return (r"C:\TwinCAT\Boot",)


def _is_symbol_file(name: str) -> bool:
    return name.lower().endswith((".tpy", ".tmc"))


def _find_remote_symbol_files(
    fs: AdsFileService, settings: Settings
) -> tuple[list[str], list[str]]:
    """按默认目录与配置路径查找 TPY/TMC；服务不可用或目录不存在时记录原因。

    返回 (候选文件, 枚举失败原因)，使上层在无候选时仍能保留实际 ADS 错误诊断。
    """
    candidates: list[str] = []
    problems: list[str] = []
    for entry in (*_default_search_dirs(settings), *settings.TPY_SEARCH_PATHS):
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
    return candidates, problems


def _download_remote_file(
    fs: AdsFileService, path: str, dest_dir: Path, settings: Settings
) -> Path:
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
                # 多读 1 字节仅用于确认是否还有数据：恰好等于上限的文件合法，
                # 超过上限一个字节即失败，避免误拒边界值。
                remaining = settings.MAX_TPY_FILE_BYTES + 1 - downloaded
                request = min(settings.FILE_READ_CHUNK, remaining)
                chunk = fs.read_chunk(handle, request)
                if not chunk:
                    break  # 正常 EOF
                temp.write(chunk)
                downloaded += len(chunk)
                if downloaded > settings.MAX_TPY_FILE_BYTES:
                    raise ValueError(f"远端文件超过安全上限 {settings.MAX_TPY_FILE_BYTES} 字节")
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
                # 子元素（如 <Name>）须保留到 Symbol 结束事件再读取，不能提前清理。
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
    *,
    settings: Settings,
) -> tuple[int, str]:
    """通过 ADS 文件服务读取 PLC 上已有 TPY/TMC 并解析变量名。

    返回 (数量, 验证等级)。文件来源结果绝不宣称为运行时完整符号表；
    验证等级语义见 _verify_file_names。某个候选文件失败只记录原因并
    尝试下一个，不提前终止整个方案。
    """
    conn = factory()
    try:
        fs = AdsFileService(conn, settings)
        try:
            candidates, problems = _find_remote_symbol_files(fs, settings)
            if not candidates:
                detail = "" if not problems else "；目录枚举失败：" + "; ".join(problems)
                raise FileNotFoundError(f"远端未发现 TPY/TMC 文件{detail}")
            last_error: Exception | None = None
            for remote in candidates:
                try:
                    local = _download_remote_file(fs, remote, output.parent, settings)
                    try:
                        names = _parse_tpy_tmc_names(local)
                    finally:
                        local.unlink(missing_ok=True)
                except Exception as exc:
                    last_error = exc
                    _LOG.info("远端文件不可用 %s: %s: %s", remote, type(exc).__name__, exc)
                    continue
                verification = _verify_file_names(names, runtime_count, runtime_names)
                _atomic_write_lines(output, names)
                _LOG.info(
                    "source=file, remote=%s, verification=%s, runtime_count=%s",
                    remote,
                    verification,
                    runtime_count,
                )
                if verification != VERIFICATION_CONTENT_MATCHED:
                    _LOG.warning("文件来源结果验证等级：%s", verification)
                return len(names), verification
            raise RuntimeError(f"所有候选文件均不可用：{last_error}")
        finally:
            fs.close_all()
    finally:
        _safe_close(conn)
