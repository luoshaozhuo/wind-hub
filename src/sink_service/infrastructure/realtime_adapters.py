"""Realtime Redis, Kafka and Modbus TCP Server outputs.

These adapters do not parse NATS messages, select source points, or run schedulers.
"""

from __future__ import annotations

import asyncio
import json
import struct
from importlib import import_module
from datetime import UTC, datetime
from typing import Any

from pymodbus.datastore import ModbusServerContext

from contracts.sample_message import SampleMessage
from sink_service.application.realtime import BatchOutcome
from sink_service.domain.config import ConfigurationError
from sink_service.infrastructure.secrets import resolve_secret


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
    "redis.call('SET',KEYS[1],ARGV[1],'PXAT',ARGV[4]);return 1"
)


class RedisOutput:
    """One serial RESP2 connection with atomic per-point monotonic writes."""

    def __init__(
        self, *, host: str, port: int, namespace: str, timeout_ms: int,
        credential_ref: str = "",
    ) -> None:
        if not namespace or any(char.isspace() for char in namespace):
            raise ConfigurationError("invalid Redis namespace")
        self._host, self._port = host, port
        self._namespace = namespace
        self._credential_ref = credential_ref
        self._timeout = timeout_ms / 1000
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()

    async def _connect(self) -> None:
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self._host, self._port),
            timeout=self._timeout,
        )
        try:
            credential = resolve_secret(self._credential_ref)
            if credential is not None:
                try:
                    identity = json.loads(credential)
                except json.JSONDecodeError:
                    identity = credential
                if isinstance(identity, str):
                    arguments = ("AUTH", identity)
                elif (
                    isinstance(identity, dict)
                    and isinstance(identity.get("username"), str)
                    and isinstance(identity.get("password"), str)
                ):
                    arguments = ("AUTH", identity["username"], identity["password"])
                else:
                    raise ConfigurationError("Redis Secret must be password or ACL JSON")
                self._writer.write(_redis_command(*arguments))
                await asyncio.wait_for(self._writer.drain(), timeout=self._timeout)
                assert self._reader is not None
                answer = await asyncio.wait_for(
                    self._reader.readline(), timeout=self._timeout
                )
                if answer != b"+OK\r\n":
                    raise ConnectionError("Redis authentication failed")
        except BaseException:
            writer, self._writer = self._writer, None
            self._reader = None
            if writer is not None:
                writer.close()
            raise

    async def open(self) -> None:
        async with self._lock:
            if self._writer is None:
                await self._connect()

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

    async def write_batch(self, samples: tuple[SampleMessage, ...]) -> BatchOutcome:
        if not samples:
            return BatchOutcome((), ())
        async with self._lock:
            if self._writer is None:
                await self._connect()
            delivered: list[int] = []
            settled: list[int] = []
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
                    timestamp = sample.sample_time.astimezone(UTC).isoformat(
                        timespec="microseconds"
                    )
                    payload = sample.model_dump(
                        mode="json",
                        include={
                            "sample_id", "value", "quality", "sample_time", "publish_time",
                            "collector_id", "device_id", "point_id", "wind_farm_id",
                        },
                    )
                    payload["sample_time"] = timestamp
                    encoded = json.dumps(
                        payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
                    )
                    expiry_ms = (
                        int(sample.sample_time.timestamp() * 1000)
                        + (sample.max_staleness_ms or 1)
                    )
                    commands.append(
                        _redis_command(
                            "EVAL", _REDIS_CAS, "1", key, encoded,
                            timestamp, sample.sample_id, str(expiry_ms),
                        )
                    )
                self._writer.write(b"".join(commands))
                await asyncio.wait_for(self._writer.drain(), timeout=self._timeout)
                errors: list[str] = []
                for index in range(len(samples)):
                    result = await self._integer()
                    if result == 1:
                        delivered.append(index)
                        settled.append(index)
                    elif result == 0:
                        settled.append(index)
                    else:
                        errors.append(f"Redis CAS conflict at sample {index}")
                return BatchOutcome(
                    delivered=tuple(delivered),
                    settled=tuple(settled),
                    error="; ".join(errors),
                )
            except Exception as exc:
                # In-flight commands may have executed; retry by stable sample ID.
                writer, self._writer = self._writer, None
                self._reader = None
                if writer is not None:
                    writer.close()
                return BatchOutcome(
                    delivered=tuple(delivered), settled=tuple(settled), error=str(exc),
                )


class KafkaOutput:
    """Broker-confirmed messages, using the unchanged upstream SampleMessage JSON."""

    def __init__(
        self, *, servers: list[str], topic: str, timeout_ms: int,
        credential_ref: str = "",
    ) -> None:
        if not servers or not topic:
            raise ConfigurationError("Kafka brokers and topic must be configured")
        self._servers, self._topic = servers, topic
        self._timeout_ms = timeout_ms
        self._credential_ref = credential_ref
        self._producer: Any = None
        self._lock = asyncio.Lock()

    async def open(self) -> None:
        if self._producer is not None:
            return
        producer_class = import_module("aiokafka").AIOKafkaProducer
        options: dict[str, Any] = {
            "bootstrap_servers": self._servers,
            "acks": "all",
            "enable_idempotence": True,
            "request_timeout_ms": self._timeout_ms,
        }
        credential = resolve_secret(self._credential_ref)
        if credential is not None:
            try:
                secret = json.loads(credential)
            except json.JSONDecodeError as exc:
                raise ConfigurationError("Kafka Secret must contain a JSON object") from exc
            if not isinstance(secret, dict) or not {
                "username", "password",
            }.issubset(secret):
                raise ConfigurationError("Kafka Secret requires username and password")
            options.update(
                security_protocol="SASL_SSL",
                sasl_mechanism="PLAIN",
                sasl_plain_username=secret["username"],
                sasl_plain_password=secret["password"],
            )
        producer = producer_class(**options)
        await producer.start()
        self._producer = producer

    async def close(self) -> None:
        producer = self._producer
        if producer is not None:
            await producer.stop()
            self._producer = None

    async def write_batch(self, samples: tuple[SampleMessage, ...]) -> BatchOutcome:
        async with self._lock:
            if self._producer is None:
                return BatchOutcome((), (), "Kafka output is not connected")
            pending: list[tuple[int, Any]] = []
            failures: list[str] = []
            for index, sample in enumerate(samples):
                key = ":".join(
                    (sample.wind_farm_id, sample.collector_id, sample.device_id)
                ).encode("utf-8")
                try:
                    confirmation = await self._producer.send(
                        self._topic, key=key, value=sample.encode()
                    )
                except Exception as exc:
                    failures.append(f"Kafka sample {index} not queued: {exc}")
                    break
                pending.append((index, confirmation))
            delivered: list[int] = []
            # Every submitted future remains owned until the broker resolves it.
            for index, confirmation in pending:
                try:
                    await confirmation
                except Exception as exc:
                    failures.append(f"Kafka sample {index} not confirmed: {exc}")
                else:
                    delivered.append(index)
            result = tuple(delivered)
            return BatchOutcome(
                delivered=result,
                settled=result,
                error="; ".join(failures),
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
        self._last: dict[int, tuple[Any, str]] = {}
        self._expires: dict[int, float] = {}
        self._value_start, self._quality_start = value_start, quality_start
        self._width = len(points)

    def update(self, samples: tuple[SampleMessage, ...]) -> BatchOutcome:
        pending: dict[int, int] = {}
        confirmed_versions: dict[int, tuple[Any, str]] = {}
        expiration_times: dict[int, float] = {}
        delivered: list[int] = []
        settled: list[int] = []
        errors: list[str] = []
        for offset, sample in enumerate(samples):
            key = (
                sample.wind_farm_id,
                sample.collector_id,
                sample.device_id,
                sample.point_id,
            )
            index = self._positions.get(key)
            if index is None:
                errors.append(f"Modbus point not mapped: {key}")
                continue
            prior = self._last.get(index)
            if prior is not None:
                old_time, old_id = prior
                if sample.sample_time < old_time or (
                    sample.sample_time == old_time and sample.sample_id == old_id
                ):
                    # An older or already confirmed sample must not roll back the image.
                    settled.append(offset)
                    continue
                if sample.sample_time == old_time:
                    errors.append(f"Modbus conflicting identity for sample time: {key}")
                    continue
            if isinstance(sample.value, bool) or not isinstance(
                sample.value, (int, float)
            ):
                errors.append(f"Modbus float32 requires numeric sample: {key}")
                continue
            try:
                binary = struct.pack(">f", sample.value)
            except (ValueError, OverflowError, struct.error) as exc:
                errors.append(f"Modbus float32 invalid at {key}: {exc}")
                continue
            high, low = struct.unpack(">HH", binary)
            value_address = self._value_start + index * 2
            pending[value_address] = high
            pending[value_address + 1] = low
            pending[self._quality_start + index] = {
                "good": 0,
                "bad": 1,
                "uncertain": 2,
            }[sample.quality]
            confirmed_versions[index] = (sample.sample_time, sample.sample_id)
            expiration_times[index] = (
                sample.sample_time.timestamp() + (sample.max_staleness_ms or 1) / 1000
            )
            delivered.append(offset)
            settled.append(offset)
        # No await within the register image update: readers see one entire batch.
        self._values.update(pending)
        self._valid.update(pending)
        self._last.update(confirmed_versions)
        self._expires.update(expiration_times)
        confirmed = tuple(delivered)
        return BatchOutcome(
            delivered=confirmed, settled=tuple(settled), error="; ".join(errors)
        )

    def read(self, address: int, count: int) -> list[int] | None:
        if count <= 0 or not (
            self._value_start <= address
            and address + count <= self._value_start + self._width * 2
            or self._quality_start <= address
            and address + count <= self._quality_start + self._width
        ):
            return None
        now = datetime.now(UTC).timestamp()
        for offset in range(address, address + count):
            if offset not in self._valid:
                return None
            if self._value_start <= offset < self._value_start + self._width * 2:
                index = (offset - self._value_start) // 2
            else:
                index = offset - self._quality_start
            if now >= self._expires.get(index, 0.0):
                return None
        return [self._values[offset] for offset in range(address, address + count)]


class _ModbusContext(ModbusServerContext):
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
        server = self._server
        if server is not None:
            await server.shutdown()
            self._server = None

    async def write_batch(self, samples: tuple[SampleMessage, ...]) -> BatchOutcome:
        return self._image.update(samples)
