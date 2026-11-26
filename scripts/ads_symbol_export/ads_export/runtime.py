"""ADS Runtime 符号表读取与原子输出。"""

from __future__ import annotations

import logging
import struct
from pathlib import Path

import pyads

from .config import Settings

_LOG = logging.getLogger(__name__)
_SYMBOL_INFO_GROUP = 0xF00F
_SYMBOL_DATA_GROUP = 0xF00B
_SYMBOL_HEADER_SIZE = 30


def _read_bytes(plc: pyads.Connection, group: int, offset: int, length: int) -> bytes:
    """有界读取 ADS 字节数组，不调用 get_all_symbols。"""
    if length <= 0:
        return b""
    raw = plc.read(group, offset, pyads.PLCTYPE_BYTE * length, return_ctypes=True)
    data = bytes(raw)
    if len(data) != length:
        raise ValueError(f"ADS 响应长度异常: 期望 {length}, 实际 {len(data)}")
    return data


def _read_symbol_table_info(plc: pyads.Connection, settings: Settings) -> tuple[int, int]:
    data = _read_bytes(plc, _SYMBOL_INFO_GROUP, 0, 8)
    count, total = struct.unpack_from("<II", data)
    if count == 0 or total < count * (_SYMBOL_HEADER_SIZE + 3):
        raise ValueError(f"无效点表信息: count={count}, size={total}")
    if total > settings.MAX_SYMBOL_BYTES:
        raise ValueError(f"点表长度 {total} 超过安全上限 {settings.MAX_SYMBOL_BYTES}")
    return count, total


def _parse_symbol_entry(data: bytearray, settings: Settings) -> tuple[str, int] | None:
    """解析一个完整的 AdsSymbolEntry；不足一条则保留缓冲区。"""
    if len(data) < _SYMBOL_HEADER_SIZE:
        return None
    length = struct.unpack_from("<I", data)[0]
    if length < _SYMBOL_HEADER_SIZE + 3:
        raise ValueError(f"非法符号记录长度: {length}")
    if length > settings.MAX_SYMBOL_BYTES:
        raise ValueError(f"符号记录长度超限: {length}")
    if length > len(data):
        return None
    # AdsSymbolEntry: entryLength(4) + indexGroup/indexOffset/size/dataType/flags(20)。
    name_len, type_len, comment_len = struct.unpack_from("<HHH", data, 24)
    required = _SYMBOL_HEADER_SIZE + name_len + 1 + type_len + 1 + comment_len + 1
    if required > length:
        raise ValueError(f"符号记录字段长度超过记录总长度: {length}")
    name_raw = bytes(data[_SYMBOL_HEADER_SIZE : _SYMBOL_HEADER_SIZE + name_len])
    name = name_raw.decode(settings.TEXT_ENCODING)
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
def _export_full_table(plc: pyads.Connection, output: Path, settings: Settings) -> int:
    """一次性读取完整符号表并解析；不使用 get_all_symbols。

    pyads.get_all_symbols 按 Windows-1252 解码符号名，无法处理 UTF-8 变量名，
    这里直接读取 0xF00B 原始字节，用与分段方案一致的解析器按 settings.TEXT_ENCODING 解码。
    注意：整表读取可能导致部分 PLC 内存溢出并使 ADS 连接失效，
    调用方必须在失败后重新检查连接，不能假定原连接仍然可用。
    """
    expected, total = _read_symbol_table_info(plc, settings)
    buffer = bytearray(_read_bytes(plc, _SYMBOL_DATA_GROUP, 0, total))
    names: list[str] = []
    while buffer:
        entry = _parse_symbol_entry(buffer, settings)
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
def _export_in_chunks(
    plc: pyads.Connection, output: Path, chunk_size: int, settings: Settings
) -> int:
    """使用指定小块大小逐次读取符号表，失败时丢弃当前结果。

    注意：客户端发送小块请求不代表 PLC 内部不构造全表；
    本方案不能声称避免 PLC 端内存溢出。
    """
    count, total = _read_symbol_table_info(plc, settings)
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
                    entry = _parse_symbol_entry(buffer, settings)
                    if entry is None:
                        # 单个记录超过 CHUNK_SIZE 也能跨多次读取。
                        break
                    name, used = entry
                    handle.write(name + "\n")
                    del buffer[:used]
                    exported += 1
                    if exported > count:
                        raise ValueError("符号记录数超过声明数量：设备可能不支持分段偏移读取")
                    if settings.LOG_EVERY > 0 and exported % settings.LOG_EVERY == 0:
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
