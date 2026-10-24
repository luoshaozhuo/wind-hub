"""一期内存快照输出，不接工业网关。"""

from seahorse.domain.models import FarmSnapshot


class MemoryPublisher:
    def __init__(self) -> None:
        self.latest: FarmSnapshot | None = None

    def publish(self, snapshot: FarmSnapshot) -> None:
        self.latest = snapshot
