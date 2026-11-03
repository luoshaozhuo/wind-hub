"""Realtime Redis, Kafka and Modbus TCP Server outputs.

These adapters do not parse NATS messages, select source points, or run schedulers.
"""

from __future__ import annotations

import asyncio
import json
import struct
from collections.abc import Mapping
from datetime import UTC, timedelta
from typing import Any

from contracts.sample_message import SampleMessage
from sink_service.domain.config import ConfigurationError


def _redis_command(*parts: str) -> bytes:
    chunks = [f"*{len(parts)}\r\n".encode("ascii")]
    for part in parts:
        raw = part.encode("utf-8")
        chunks.extend((f"${len(raw)}\r\n".encode("ascii"), raw, b"\r\n"))
    return b"".join(chunks)


_REDIS_CAS = (
    "local cur=redis.call('GET',KEYS[1]);"
    "if cur then local old=cjson.decode(cur);"
    "if old.sample_time>ARGV[2] then return 0 end;"
    "if old.sample_time==ARGV[2] then "
    "if old.sample_id==ARGV[3] then return 0 end;"
    "return -1 end end;"
    "redis.call('SET',KEYS[1],ARGV[1]);return 1"
)


class RedisOutput:
    """One serial RESP2 connection with atomic per-point monotonic writes."""

    def __init__(self, *, host: str, port: int, namespace: str, timeout_ms: int) -> None:
        if not namespace or any(char.isspace() for char in namespace):
            raise ConfigurationError("invalid Redis namespace")
        self._host, self._port = host, port
        self._namespace = namespace
        self._timeout = timeout_ms / 1000
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()

    async def open(self) -> None:
        async with self._lock:
            if self._writer is None:
                self._reader, self._writer = await asyncio.wait_for(
                    asyncio.open_connection(self._host, self._port),
                    timeout=self._timeout,
                )

    async def close(self) -> None:
        async with self._lock:
            writer, self._writer = self._writer, None
            self._reader = None
            if writer is not None:
                writer.close()
                await writer.wait_closed()

    async def _integer(self) -> int:
        if self._reader is None:
            raise ConnectionError("Redis is disconnected")
        response = await asyncio.wait_for(self._reader.readline(), timeout=self._timeout)
        if not response.endswith(b"\r\n"):
            raise ConnectionError("invalid Redis response")
        if response.startswith(b"-"):
            raise ConnectionError(response.decode("utf-8", errors="replace"))
        if not response.startswith(b":"):
            raise ConnectionError("unexpected Redis response type")
        return int(response[1:-2])

    async def write_batch(self, samples: tuple[SampleMessage, ...]) -> None:
        if not samples:
            return
        async with self._lock:
            if self._writer is None:
                raise ConnectionError("Redis output is not connected")
            try:
                commands: list[bytes] = []
                for sample in samples:
                    key = ":".join(
                        (
                            self._namespace,
                            sample.wind_farm_id,
                            sample.collector_id,
                            sample.device_id,
                            sample.point_id,
                        )
                    )
                    payload = sample.model_dump(
                        mode="json",
                        include={
                            "sample_id", "value", "quality", "sample_time", "publish_time",
                            "collector_id", "device_id", "point_id", "wind_farm_id",
                        },
                    )
                    data = json.dumps(
                        payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False
                    )
                    timestamp = sample.sample_time.astimezone(UTC).isoformat(
                        timespec="microseconds"
                    )
                    commands.append(
                        _redis_command(
                            "EVAL", _REDIS_CAS, "1", key, data, timestamp, sample.sample_id
                        )
                    )
                self._writer.write(b"".join(commands))
                await asyncio.wait_for(self._writer.drain(), timeout=self._timeout)
                results = [await self._integer() for _ in samples]
                if any(code == -1 for code in results):
                    raise ConnectionError("Redis equal-time conflicting sample identities")
                if any(code not in (0, 1) for code in results):
                    raise ConnectionError("unexpected Redis write result")
            except Exception:
                self._writer.close()
                self._writer = None
                self._reader = None
                raise


class KafkaOutput:
    """Broker-confirmed messages, using the unchanged upstream SampleMessage JSON."""

    def __init__(
        self, *, servers: list[str], topic: str, timeout_ms: int,
    ) -> None:
        if not servers or not topic:
            raise ConfigurationError("Kafka brokers and topic must be configured")
        self._servers, self._topic = servers, topic
        self._timeout_ms = timeout_ms
        self._producer: Any = None
        self._lock = asyncio.Lock()

    async def open(self) -> None:
        if self._producer is not None:
            return
        from aiokafka import AIOKafkaProducer

        producer = AIOKafkaProducer(
            bootstrap_servers=self._servers,
            acks="all",
            enable_idempotence=True,
            request_timeout_ms=self._timeout_ms,
        )
        await producer.start()
        self._producer = producer

    async def close(self) -> None:
        producer, self._producer = self._producer, None
        if producer is not None:
            await producer.stop()

    async def write_batch(self, samples: tuple[SampleMessage, ...]) -> None:
        async with self._lock:
            if self._producer is None:
                raise ConnectionError("Kafka output is not connected")
            for sample in samples:
                key = ":".join(
                    (sample.wind_farm_id, sample.collector_id, sample.device_id)
                ).encode("utf-8")
                await self._producer.send_and_wait(
                    self._topic, key=key, value=sample.encode()
                )


class _RegisterImage:
    """Single event-loop-owned register image, atomically replaced per batch."""

    def __init__(
        self,
        *,
        points: tuple[tuple[str, str, str, str], ...],
        value_start: int,
        quality_start: int,
    ) -> None:
        self._positions = {key: idx for idx, key in enumerate(points)}
        if len(self._positions) != len(points):
            raise ConfigurationError("duplicate Modbus source point")
        self._values: dict[int, int] = {}
        self._valid: set[int] = set()
        self._value_start, self._quality_start = value_start, quality_start
        self._width = len(points)

    def update(self, samples: tuple[SampleMessage, ...]) -> None:
        pending: dict[int, int] = {}
        for sample in samples:
            key = (
                sample.wind_farm_id,
                sample.collector_id,
                sample.device_id,
                sample.point_id,
            )
            index = self._positions.get(key)
            if index is None:
                raise ConfigurationError(f"Modbus sample is not mapped: {key}")
            if isinstance(sample.value, bool) or not isinstance(sample.value, (int, float)):
                raise ConfigurationError(f"Modbus float32 requires numeric sample: {key}")
            try:
                binary = struct.pack(">f", sample.value)
            except (ValueError, OverflowError, struct.error) as exc:
                raise ConfigurationError(f"Modbus float32 overflow: {key}") from exc
            high, low = struct.unpack(">HH", binary)
            offset = self._value_start + index * 2
            pending[offset] = high
            pending[offset + 1] = low
            pending[self._quality_start + index] = {
                "good": 0, "bad": 1, "uncertain": 2,
            }[sample.quality]
        self._values.update(pending)
        self._valid.update(pending)

    def read(self, address: int, count: int) -> list[int] | None:
        if count <= 0 or not (
            self._value_start <= address
            and address + count <= self._value_start + self._width * 2
            or self._quality_start <= address
            and address + count <= self._quality_start + self._width
        ):
            return None
        if any(offset not in self._valid for offset in range(address, address + count)):
            return None
        return [self._values[offset] for offset in range(address, address + count)]


class _ModbusContext:
    """A pymodbus-compatible context that refuses every external write."""

    def __init__(self, image: _RegisterImage, unit_id: int) -> None:
        self.simdevices: list[Any] = []
        self.old_simulator = True
        self._image = image
        self._unit_id = unit_id

    async def async_getValues(
        self, device_id: int, func_code: int, address: int, count: int = 1,
    ) -> Any:
        from pymodbus.constants import ExcCodes

        if device_id != self._unit_id:
            return ExcCodes.ILLEGAL_ADDRESS
        if func_code != 3:
            return ExcCodes.ILLEGAL_FUNCTION
        values = self._image.read(address, count)
        return values if values is not None else ExcCodes.ILLEGAL_ADDRESS

    async def async_setValues(
        self, device_id: int, func_code: int, address: int, values: list[int],
    ) -> Any:
        from pymodbus.constants import ExcCodes

        return ExcCodes.ILLEGAL_FUNCTION

    def device_ids(self) -> list[int]:
        return [self._unit_id]


class ModbusOutput:
    """Read-only Modbus TCP Server: confirmed delivery means image update only."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        unit_id: int,
        points: tuple[tuple[str, str, str, str], ...],
        value_start: int,
        quality_start: int,
    ) -> None:
        self._address = host, port
        self._image = _RegisterImage(
            points=points, value_start=value_start, quality_start=quality_start,
        )
        self._context = _ModbusContext(self._image, unit_id)
        self._server: Any = None

    async def open(self) -> None:
        if self._server is not None:
            return
        from pymodbus.server import ModbusTcpServer

        server = ModbusTcpServer(self._context, address=self._address)
        try:
            await server.serve_forever(background=True)
        except Exception:
            await server.shutdown()
            raise
        self._server = server

    async def close(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            await server.shutdown()

    async def write_batch(self, samples: tuple[SampleMessage, ...]) -> None:
        self._image.update(samples)
