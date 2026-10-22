"""经 Gateway 白名单地址发布测量反馈，失败即中断当前步。

仅允许显式映射的反馈点，不提供命令写入或绕过 ACL 的通路。
"""
from __future__ import annotations

import math
import struct
from collections.abc import Callable
from pathlib import Path
from typing import Literal, Protocol

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pymodbus.client import ModbusTcpClient

from seahorse.domain.models import FarmSnapshot, Scalar


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PublishPoint(_Strict):
    device_id: str = Field(min_length=1)
    point_id: str = Field(min_length=1)
    host: str = Field(min_length=1)
    port: int = Field(ge=1, le=65535)
    unit_id: int = Field(ge=1, le=247)
    address: int = Field(ge=0, le=65535)
    area: Literal["holding", "coil"] = "holding"
    data_type: Literal["float32", "int32", "bool"] = "float32"
    word_order: Literal["big_endian", "little_endian"] = "big_endian"

    @model_validator(mode="after")
    def validate_mapping(self) -> PublishPoint:
        if (self.area == "coil") != (self.data_type == "bool"):
            raise ValueError("coil requires bool, registers require numeric")
        if self.area == "holding" and self.address == 65535:
            raise ValueError("two-register point exceeds address space")
        return self


class PublishConfig(_Strict):
    points: list[PublishPoint] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_points(self) -> PublishConfig:
        logical: set[tuple[str, str]] = set()
        addresses: set[tuple[str, int, int, str, int]] = set()
        for point in self.points:
            key = (point.device_id, point.point_id)
            if key in logical:
                raise ValueError(f"duplicate publish mapping: {key}")
            logical.add(key)
            count = 1 if point.area == "coil" else 2
            for offset in range(count):
                address = (
                    point.host, point.port, point.unit_id, point.area, point.address + offset
                )
                if address in addresses:
                    raise ValueError(f"overlapping publish registers: {address}")
                addresses.add(address)
        return self


def load_publish_config(path: Path) -> PublishConfig:
    raw: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    return PublishConfig.model_validate(raw)


def _encode(point: PublishPoint, value: Scalar) -> tuple[int, ...]:
    if point.data_type == "bool":
        if type(value) is not bool:
            raise TypeError("coil value must be bool")
        return (int(value),)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError("register value must be numeric")
    if not math.isfinite(float(value)):
        raise ValueError("non-finite register value")
    if point.data_type == "int32":
        if type(value) is not int:
            raise TypeError("int32 requires int")
        packed = struct.pack(">i", value)
    else:
        packed = struct.pack(">f", value)
    words = (int.from_bytes(packed[:2], "big"), int.from_bytes(packed[2:], "big"))
    return words[::-1] if point.word_order == "little_endian" else words


class _ModbusResponse(Protocol):
    def isError(self) -> bool: ...


class _ModbusClient(Protocol):
    def connect(self) -> bool: ...
    def close(self) -> None: ...
    def write_registers(
        self, address: int, values: list[int], *, device_id: int
    ) -> _ModbusResponse: ...
    def write_coil(
        self, address: int, value: bool, *, device_id: int
    ) -> _ModbusResponse: ...


ClientFactory = Callable[[str, int], _ModbusClient]


def _client(host: str, port: int) -> _ModbusClient:
    return ModbusTcpClient(host, port=port, timeout=2.0)


class GatewayModbusPublisher:
    """每个仿真步按点发布；任意写入失败会向上抛异常。"""

    def __init__(
        self, config: PublishConfig, client_factory: ClientFactory = _client
    ) -> None:
        self._config = config
        self._client_factory = client_factory

    def publish(self, snapshot: FarmSnapshot) -> None:
        devices = {device.device_id: device.points for device in snapshot.devices}
        prepared: list[tuple[PublishPoint, tuple[int, ...]]] = []
        for point in self._config.points:
            if point.device_id not in devices:
                raise KeyError(f"unknown device: {point.device_id}")
            points = devices[point.device_id]
            if point.point_id not in points:
                raise KeyError(f"missing feedback: {point.device_id}.{point.point_id}")
            prepared.append((point, _encode(point, points[point.point_id])))
        for point, registers in prepared:
            client = self._client_factory(point.host, point.port)
            try:
                if not client.connect():
                    raise ConnectionError(f"gateway unavailable: {point.host}:{point.port}")
                if point.area == "coil":
                    response = client.write_coil(
                        point.address, bool(registers[0]), device_id=point.unit_id
                    )
                else:
                    response = client.write_registers(
                        point.address, list(registers), device_id=point.unit_id
                    )
                if response.isError():
                    raise RuntimeError(
                        f"gateway rejected feedback: {point.device_id}.{point.point_id}"
                    )
            finally:
                client.close()
