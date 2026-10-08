"""File sink 压缩 —— 滚动后对旧分片做后台压缩。

压缩在独立 task 里执行（不阻塞写入热路径）；压缩失败只记录日志、不抛异常。
``GzipCompressor`` 把 ``{path}`` 压缩成 ``{path}.gz`` 并删除原文件；
``NoCompressor`` 是禁用压缩时的空实现，原样返回路径。
"""

from __future__ import annotations

import gzip
import shutil
from pathlib import Path
from typing import Protocol


class Compressor(Protocol):
    """压缩器接口。"""

    def compress(self, path: Path) -> Path:
        """压缩文件，返回压缩后的路径。"""
        ...


class GzipCompressor:
    """gzip 压缩：``{path}`` → ``{path}.gz``，成功后删除原文件。"""

    def __init__(self, level: int = 6) -> None:
        self._level = level

    def compress(self, path: Path) -> Path:
        target = Path(f"{path}.gz")
        with path.open("rb") as src, gzip.open(target, "wb", compresslevel=self._level) as dst:
            shutil.copyfileobj(src, dst)
        path.unlink()
        return target


class NoCompressor:
    """不压缩——原样返回路径。"""

    def compress(self, path: Path) -> Path:
        return path
