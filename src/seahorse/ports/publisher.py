"""仿真快照发布端口，与 Gateway 解耦。"""
from typing import Protocol

from seahorse.domain.models import FarmSnapshot


class PublisherPort(Protocol):
    def publish(self, snapshot: FarmSnapshot) -> None: ...
