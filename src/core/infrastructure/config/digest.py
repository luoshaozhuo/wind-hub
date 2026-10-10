"""配置目录内容摘要——跨进程配置版本协调的确定性摘要。

Server 与各 Worker（Collector/Commander）经 RPC ``config_hash`` 字段确认
双方持有同一份磁盘配置集；本函数是 Worker 侧的摘要实现，算法与 Server
端契约逐字节一致（SHA-256 over 排序后的相对路径与文件字节，排除
``.history``），保证跨进程可比对。

该能力属于运行时进程协调，不属于 ConfigPort，也不参与任何配置语义比较
——进程内的配置变化判断应直接比较已加载的配置 VO。
"""

from __future__ import annotations

import hashlib
from pathlib import Path


def config_dir_digest(config_dir: str | Path) -> str:
    """计算现场配置目录的稳定 SHA-256 摘要（RPC config_hash 契约）。"""
    site_dir = Path(config_dir).resolve()
    digest = hashlib.sha256()
    files: list[tuple[str, Path]] = []
    for path in site_dir.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in {".yaml", ".yml"}:
            continue
        relative_path = path.relative_to(site_dir)
        if ".history" in relative_path.parts:
            continue
        files.append((relative_path.as_posix(), path))

    for relative, path in sorted(files):
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


__all__ = ["config_dir_digest"]
