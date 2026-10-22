"""验证连续寄存器合并写入且不跨越地址空洞。"""
from seahorse.domain.models import DeviceSnapshot, FarmSnapshot, SimulationTime
from seahorse.infrastructure.modbus_publisher import (
    GatewayModbusPublisher,
    PublishConfig,
    PublishPoint,
)


class Response:
    def isError(self) -> bool:
        return False


class Client:
    def __init__(self) -> None:
        self.connected = 0
        self.writes: list[tuple[int, list[int], int]] = []

    def connect(self) -> bool:
        self.connected += 1
        return True

    def close(self) -> None:
        pass

    def write_registers(self, address: int, values: list[int], *, device_id: int) -> Response:
        self.writes.append((address, values, device_id))
        return Response()

    def write_coil(self, address: int, value: bool, *, device_id: int) -> Response:
        raise AssertionError("not a coil scenario")


def test_contiguous_registers_share_write() -> None:
    points = [
        PublishPoint(
            device_id="wt1", point_id=name, host="127.0.0.1", port=15020,
            unit_id=1, address=address,
        )
        for name, address in (("p1", 100), ("p2", 102), ("p3", 200))
    ]
    client = Client()
    publisher = GatewayModbusPublisher(
        PublishConfig(points=points), client_factory=lambda _host, _port: client
    )
    publisher.publish(
        FarmSnapshot(
            SimulationTime(1, 0.5, 0.5),
            (DeviceSnapshot("wt1", {"p1": 1.0, "p2": 2.0, "p3": 3.0}),),
        )
    )
    assert client.connected == 1
    assert [(address, len(words)) for address, words, _ in client.writes] == [
        (100, 4), (200, 2)
    ]
