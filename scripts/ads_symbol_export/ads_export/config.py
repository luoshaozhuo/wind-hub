"""加载并严格校验独立 YAML 配置。"""

from __future__ import annotations

import codecs
import ipaddress
from dataclasses import dataclass, fields
from pathlib import Path

import yaml

DEFAULT_CONFIG = Path("ads_symbol_export_v1.0.0.yaml")


@dataclass
class Settings:
    """单次运行使用的配置；数值单位见 YAML。"""

    PLC_IP: str
    PLC_AMS_NET_ID: str
    TWINCAT_VERSION: int
    PLC_AMS_PORT: int | None
    ADD_LOCAL_ROUTE: bool
    ADD_REMOTE_ROUTE_IF_NEEDED: bool
    ADS_TIMEOUT_MS: int
    RECONNECT_ATTEMPTS: int
    READ_METHOD: str
    TRY_FULL_TABLE_FIRST: bool
    CHUNK_SIZES: tuple[int, ...]
    MAX_SYMBOL_BYTES: int
    OUTPUT_FILENAME: str
    TEXT_ENCODING: str
    LOG_EVERY: int
    CHECK_HOST_ENVIRONMENT: bool
    FILE_SERVICE_ENABLED: bool
    TPY_SEARCH_PATHS: tuple[str, ...]
    FILE_READ_CHUNK: int
    MAX_TPY_FILE_BYTES: int
    FILE_FIND_MAX_ENTRIES: int
    REMOTE_PATH_ENCODING: str


def load_config(path: Path) -> Settings:
    """读取配置；未知、缺失或非法字段均在连接 PLC 前拒绝。"""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("配置必须为 YAML mapping")
    expected = {f.name for f in fields(Settings)} | {"schema_version", "file_version"}
    if set(raw) != expected:
        raise ValueError(f"配置字段不匹配：缺少 {expected - set(raw)}，未知 {set(raw) - expected}")
    if raw.pop("schema_version") != "1.0.0":
        raise ValueError("schema_version 不支持")
    version = raw.pop("file_version")
    if not isinstance(version, str) or len(version.split(".")) != 3 or not all(
        part.isdecimal() for part in version.split(".")
    ):
        raise ValueError("file_version 必须为三段数字版本")
    for key in (
        "ADD_LOCAL_ROUTE", "ADD_REMOTE_ROUTE_IF_NEEDED", "TRY_FULL_TABLE_FIRST",
        "CHECK_HOST_ENVIRONMENT", "FILE_SERVICE_ENABLED",
    ):
        if type(raw[key]) is not bool:
            raise ValueError(f"{key} 必须为布尔值")
    for key in (
        "PLC_IP", "PLC_AMS_NET_ID", "READ_METHOD", "OUTPUT_FILENAME",
        "TEXT_ENCODING", "REMOTE_PATH_ENCODING",
    ):
        if not isinstance(raw[key], str) or not raw[key].strip():
            raise ValueError(f"{key} 必须为非空字符串")
    ipaddress.IPv4Address(raw["PLC_IP"])
    parts = raw["PLC_AMS_NET_ID"].split(".")
    if len(parts) != 6 or not all(p.isdecimal() and 0 <= int(p) <= 255 for p in parts):
        raise ValueError("PLC_AMS_NET_ID 必须为六段 0–255 数字")
    for key in (
        "TWINCAT_VERSION", "ADS_TIMEOUT_MS", "RECONNECT_ATTEMPTS", "MAX_SYMBOL_BYTES",
        "LOG_EVERY", "FILE_READ_CHUNK", "MAX_TPY_FILE_BYTES", "FILE_FIND_MAX_ENTRIES",
    ):
        minimum = 0 if key in ("LOG_EVERY", "RECONNECT_ATTEMPTS") else 1
        if type(raw[key]) is not int or raw[key] < minimum:
            raise ValueError(f"{key} 数值非法")
    if raw["TWINCAT_VERSION"] not in (2, 3) or raw["RECONNECT_ATTEMPTS"] > 10:
        raise ValueError("TwinCAT 版本或重连次数非法")
    port = raw["PLC_AMS_PORT"]
    if port is not None and (type(port) is not int or not 1 <= port <= 65535):
        raise ValueError("PLC_AMS_PORT 非法")
    chunks = raw["CHUNK_SIZES"]
    if not isinstance(chunks, list) or any(type(n) is not int or n < 33 for n in chunks):
        raise ValueError("CHUNK_SIZES 必须为至少 33 字节的整数列表")
    paths = raw["TPY_SEARCH_PATHS"]
    if not isinstance(paths, list) or any(not isinstance(p, str) or not p.strip() for p in paths):
        raise ValueError("TPY_SEARCH_PATHS 必须为路径字符串列表")
    if raw["FILE_READ_CHUNK"] > raw["MAX_TPY_FILE_BYTES"]:
        raise ValueError("文件读取块超过文件长度上限")
    for key in ("TEXT_ENCODING", "REMOTE_PATH_ENCODING"):
        try:
            codecs.lookup(raw[key])
        except LookupError as exc:
            raise ValueError(f"{key} 编码不支持：{raw[key]}") from exc
    raw["CHUNK_SIZES"] = tuple(chunks)
    raw["TPY_SEARCH_PATHS"] = tuple(paths)
    result = Settings(**raw)
    validate_method(result, result.READ_METHOD)
    return result


def validate_method(settings: Settings, method: str) -> None:
    """显式读取方法不得回退到其他方法。"""
    if method not in ("auto", "full", "chunk", "file"):
        raise ValueError("READ_METHOD 必须为 auto/full/chunk/file")
    if method == "chunk" and not settings.CHUNK_SIZES:
        raise ValueError("chunk 方法要求 CHUNK_SIZES 非空")
    if method == "auto" and not (
        settings.TRY_FULL_TABLE_FIRST or settings.CHUNK_SIZES or settings.FILE_SERVICE_ENABLED
    ):
        raise ValueError("至少启用一种枚举方案")
