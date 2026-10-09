"""独立 ADS 点表导出工具：分段读取，逐条写出变量名。"""

from __future__ import annotations

import ctypes
import logging
import struct
from pathlib import Path

import pyads

# ==================== 全部用户配置 ====================
PLC_IP = "192.168.1.10"
PLC_AMS_NET_ID = "192.168.1.10.1.1"
PLC_AMS_PORT = 851

# 本机 ADS 路由。Linux 通常需要指定 PLC 的 AMS Net ID 与 IP。
ADD_LOCAL_ROUTE = True

# 远端路由仅在明确需要时开启；已能连接则绝不重复添加。
# pyads 无法可靠查询 PLC 远端路由表，因此默认关闭，避免盲目重复添加。
ADD_REMOTE_ROUTE_IF_NEEDED = False
LOCAL_AMS_NET_ID = ""  # 需要远端路由时填写本机 AMS Net ID，如 "192.168.1.20.1.1"
LOCAL_HOST_NAME = ""  # PLC 路由表中显示的本机名称
PLC_USERNAME = "Administrator"
PLC_PASSWORD = ""  # 可在运行前填写，勿提交真实密码
REMOTE_ROUTE_NAME = "ads-symbol-export"

ADS_TIMEOUT_MS = 5000
CHUNK_SIZE = 4096  # 单次 ADS 响应上限；应小于目标设备允许的响应大小
MAX_SYMBOL_BYTES = 8 * 1024 * 1024 * 1024  # 仅用于防范异常长度，不会一次性分配
OUTPUT_FILENAME = "ads_symbols.txt"  # 输出到运行命令时的当前工作目录
TEXT_ENCODING = "utf-8"
LOG_EVERY = 1000
# ====================================================

_LOG = logging.getLogger(__name__)
_UPLOAD_INFO2 = 0xF00F
_UPLOAD = 0xF00B
_HEADER_SIZE = 30


def _read_bytes(plc: pyads.Connection, group: int, offset: int, length: int) -> bytes:
    """有界读取 ADS 字节数组，不调用 get_all_symbols。"""
    if length <= 0:
        return b""
    raw = plc.read(group, offset, pyads.PLCTYPE_BYTE * length, return_ctypes=True)
    data = bytes(raw)
    if len(data) != length:
        raise ValueError(f"ADS 响应长度异常: 期望 {length}, 实际 {len(data)}")
    return data


def _symbol_info(plc: pyads.Connection) -> tuple[int, int]:
    data = _read_bytes(plc, _UPLOAD_INFO2, 0, 8)
    count, total = struct.unpack_from("<II", data)
    if not count or total < count * (_HEADER_SIZE + 3):
        raise ValueError(f"无效点表信息: count={count}, size={total}")
    if total > MAX_SYMBOL_BYTES:
        raise ValueError(f"点表长度 {total} 超过安全上限 {MAX_SYMBOL_BYTES}")
    return count, total


def _parse_entry(data: bytearray) -> tuple[str, int] | None:
    """解析一个完整的 AdsSymbolEntry；不足一条则保留缓冲区。"""
    if len(data) < _HEADER_SIZE:
        return None
    length = struct.unpack_from("<I", data)[0]
    if length < _HEADER_SIZE + 3:
        raise ValueError(f"非法符号记录长度: {length}")
    if length > len(data):
        return None
    name_len, type_len, comment_len = struct.unpack_from("<HHH", data, 24)
    required = _HEADER_SIZE + name_len + 1 + type_len + 1 + comment_len + 1
    if required > length:
        raise ValueError(f"符号记录字段长度超过记录总长度: {length}")
    name_raw = bytes(data[_HEADER_SIZE : _HEADER_SIZE + name_len])
    name = name_raw.decode(TEXT_ENCODING)
    if not name:
        raise ValueError("收到空符号名")
    if data[_HEADER_SIZE + name_len] != 0:
        raise ValueError("符号名未以 NUL 结尾")
    return name, length


def export_symbols(plc: pyads.Connection, output: Path) -> int:
    """顺序消费符号上传数据，流式写文件；不保存全表。"""
    count, total = _symbol_info(plc)
    _LOG.info("PLC symbols: %d, upload bytes: %d", count, total)
    if CHUNK_SIZE < _HEADER_SIZE + 3:
        raise ValueError("CHUNK_SIZE 太小")
    buffer = bytearray()
    offset = 0
    exported = 0
    # 使用临时文件：未完成或失败时不会覆盖既有输出。
    temp = output.with_name(output.name + ".partial")
    try:
        with temp.open("w", encoding="utf-8", newline="\n") as handle:
            while offset < total:
                length = min(CHUNK_SIZE, total - offset)
                chunk = _read_bytes(plc, _UPLOAD, offset, length)
                buffer.extend(chunk)
                offset += length
                while len(buffer) >= _HEADER_SIZE:
                    entry = _parse_entry(buffer)
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


def _open_connection() -> pyads.Connection:
    if ADD_LOCAL_ROUTE:
        pyads.add_route(PLC_AMS_NET_ID, PLC_IP)
    plc = pyads.Connection(PLC_AMS_NET_ID, PLC_AMS_PORT, PLC_IP)
    plc.open()
    try:
        plc.set_timeout(ADS_TIMEOUT_MS)
        plc.read_state()  # 先验证链路；正常则绝不修改 PLC 远端路由
        return plc
    except Exception:
        plc.close()
        if not ADD_REMOTE_ROUTE_IF_NEEDED:
            raise
    if not all((LOCAL_AMS_NET_ID, LOCAL_HOST_NAME, PLC_USERNAME, PLC_PASSWORD)):
        raise ValueError("远端路由所需参数未填写")
    # PLC 不提供通用的远端路由查询 API；此操作应仅在确认目标尚无该路由时开启。
    pyads.add_route_to_plc(
        LOCAL_AMS_NET_ID,
        LOCAL_HOST_NAME,
        PLC_IP,
        PLC_USERNAME,
        PLC_PASSWORD,
        route_name=REMOTE_ROUTE_NAME,
    )
    plc = pyads.Connection(PLC_AMS_NET_ID, PLC_AMS_PORT, PLC_IP)
    plc.open()
    try:
        plc.set_timeout(ADS_TIMEOUT_MS)
        plc.read_state()
    except Exception:
        plc.close()
        raise
    return plc


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    destination = Path.cwd() / OUTPUT_FILENAME
    plc = _open_connection()
    try:
        total = export_symbols(plc, destination)
    finally:
        plc.close()
    _LOG.info("已写入 %d 个变量名: %s", total, destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
