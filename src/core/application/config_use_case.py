"""完整配置读取、比较与保存的应用用例。"""

from __future__ import annotations

from .config_diff import ConfigSnapshotDiff, diff_config_snapshots
from .config_snapshot import ConfigSnapshot
from .port.config import ConfigPort


class ConfigUseCase:
    """仅编排 ConfigPort 与纯快照比较，不持有隐式配置缓存。"""

    def __init__(self, port: ConfigPort) -> None:
        self._port = port

    def load(self) -> ConfigSnapshot:
        return self._port.load()

    def diff(self, previous: ConfigSnapshot, current: ConfigSnapshot) -> ConfigSnapshotDiff:
        return diff_config_snapshots(previous, current)

    def save(self, snapshot: ConfigSnapshot) -> None:
        self._port.save(snapshot)
