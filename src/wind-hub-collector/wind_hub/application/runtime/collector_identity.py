"""Collector 进程身份与当前配置指纹。

该模块只描述 Collector 自身事实，不承担 Server 配置版本管理。Server 尚未
下发显式 revision 时，config_revision 保持 None，不能用本地 hash 冒充 revision。
"""

from __future__ import annotations

import hashlib
import os
import socket
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4


@dataclass(frozen=True, slots=True)
class CollectorIdentity:
    """单个 Collector 进程实例的稳定/启动期身份。

    Attributes:
        collector_id: 部署侧稳定 Collector 标识。
        boot_id: 单次进程启动唯一标识。
        config_hash: 启动时配置集指纹。
        config_revision: Server 管理的显式配置版本；未接入时为 None。
    """

    collector_id: str
    boot_id: str
    config_hash: str
    config_revision: str | None = None


def default_collector_id() -> str:
    """返回默认 Collector ID。

    环境变量优先；未配置时使用主机名。单机多 Collector 部署必须显式设置
   不同的 WIND_HUB_COLLECTOR_ID 或 --collector-id。
    """
    configured = os.getenv("WIND_HUB_COLLECTOR_ID")
    if configured:
        return configured
    return socket.gethostname()


def fingerprint_config_set(config_dir: str | Path) -> str:
    """计算当前 YAML 配置集的稳定 SHA-256 指纹。

    现场目录以及同级 common/（若存在）均纳入指纹；文件按稳定路径排序，
    路径和内容共同参与 hash，避免同内容文件互换位置后指纹不变。

    Args:
        config_dir: 现场配置目录。

    Returns:
        十六进制 SHA-256 字符串。
    """
    site_dir = Path(config_dir).resolve()
    roots = [site_dir]
    common_dir = site_dir.parent / "common"
    if common_dir.is_dir() and common_dir != site_dir:
        roots.append(common_dir)

    digest = hashlib.sha256()
    files: list[tuple[str, Path]] = []
    for root in roots:
        prefix = root.name
        for path in root.rglob("*"):
            if path.is_file() and path.suffix.lower() in {".yaml", ".yml"}:
                relative = path.relative_to(root).as_posix()
                files.append((f"{prefix}/{relative}", path))

    for relative, path in sorted(files):
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def build_collector_identity(
    config_dir: str | Path,
    *,
    collector_id: str | None = None,
) -> CollectorIdentity:
    """构造本次 Collector 进程身份快照。

    Args:
        config_dir: 用于计算启动配置指纹的现场配置目录。
        collector_id: 显式 Collector ID；为空时使用环境变量或主机名。

    Returns:
        本次进程启动的 CollectorIdentity。
    """
    return CollectorIdentity(
        collector_id=collector_id or default_collector_id(),
        boot_id=uuid4().hex,
        config_hash=fingerprint_config_set(config_dir),
    )
