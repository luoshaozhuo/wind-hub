"""Realtime Redis, Kafka and Modbus TCP Server outputs.

These adapters do not parse NATS messages, select source points, or run schedulers.
"""

from __future__ import annotations

import asyncio
import json
import struct
import time
from collections import OrderedDict
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from importlib import import_module
from typing import Any, Protocol, cast

from pymodbus.datastore import ModbusServerContext

from sink_service.application.delivery_point import DeliveryPoint
from sink_service.application.realtime import BatchOutcome
from sink_service.domain.config import ConfigurationError
from sink_service.infrastructure.secrets import resolve_secret


def _redis_command(*parts: str) -> bytes:
    chunks = [f"*{len(parts)}\r\n".encode("ascii")]
    for part in parts:
        raw = part.encode("utf-8")
        chunks.extend((f"${len(raw)}\r\n".encode("ascii"), raw, b"\r\n"))
    return b"".join(chunks)


_REDIS_BATCH_CAS = (
    "local result={};"
    "local clock=redis.call('TIME');"
    "local now=clock[1]*1000+math.floor(clock[2]/1000);"
    "for i=1,#KEYS do "
    "local base=(i-1)*4;"
    "local code=1;"
    "local cur=redis.call('GET',KEYS[i]);"
    "if cur then "
    "local old=cjson.decode(cur);"
    "if old.sample_time>ARGV[base+2] then code=0;"
    "elseif old.sample_time==ARGV[base+2] then "
    "if old.sample_id==ARGV[base+3] then code=2;else code=-1;end;"
    "end;end;"
    "if code==1 and ARGV[base+4]~='' then "
    "if tonumber(ARGV[base+4])<=now then code=0;end;"
    "end;"
    "if code==1 then "
    "if ARGV[base+4]=='' then "
    "redis.call('SET',KEYS[i],ARGV[base+1]);"
    "else "
    "redis.call('SET',KEYS[i],ARGV[base+1],'PXAT',ARGV[base+4]);"
    "end;end;"
    "result[i]=code;"
    "end;return result"
)


class RedisOutput:
    """One serial RESP2 connection with atomic per-point monotonic writes."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        namespace: str,
        timeout_ms: int,
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
                if credential.lstrip().startswith("{"):
                    try:
                        identity = json.loads(credential)
                    except json.JSONDecodeError as exc:
                        raise ConfigurationError("invalid Redis ACL Secret JSON") from exc
                else:
                    identity = credential
                arguments: tuple[str, ...]
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
                writer = self._writer
                reader = self._reader
                if writer is None or reader is None:
                    raise ConnectionError("Redis authentication channel unavailable")
                writer.write(_redis_command(*arguments))
                await asyncio.wait_for(writer.drain(), timeout=self._timeout)
                answer = await asyncio.wait_for(reader.readline(), timeout=self._timeout)
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

    async def _integer_array(self, count: int) -> list[int]:
        reader = self._reader
        if reader is None:
            raise ConnectionError("Redis output is disconnected")
        header = await asyncio.wait_for(reader.readline(), timeout=self._timeout)
        if header.startswith(b"-"):
            raise ConnectionError("Redis batch script failed")
        if not header.startswith(b"*") or not header.endswith(b"\r\n"):
            raise ConnectionError("unexpected Redis batch response")
        if int(header[1:-2]) != count:
            raise ConnectionError("Redis batch response length mismatch")
        return [await self._integer() for _ in range(count)]

    async def write_batch(self, samples: tuple[DeliveryPoint, ...]) -> BatchOutcome:
        if not samples:
            return BatchOutcome((), ())
        if len(samples) > 500:
            raise ConfigurationError("Redis output batch exceeds 500 points")
        async with self._lock:
            if self._writer is None:
                await self._connect()
            writer = self._writer
            if writer is None:
                raise ConnectionError("Redis output is disconnected")
            try:
                keys: list[str] = []
                arguments: list[str] = []
                for sample in samples:
                    keys.append(
                        ":".join(
                            (
                                self._namespace,
                                sample.wind_farm_id,
                                sample.collector_id,
                                sample.device_id,
                                sample.point_id,
                            )
                        )
                    )
                    timestamp = sample.sample_time.astimezone(UTC).isoformat(
                        timespec="microseconds"
                    )
                    output_fields = {
                        "sample_id",
                        "value",
                        "quality",
                        "sample_time",
                        "publish_time",
                        "collector_id",
                        "device_id",
                        "point_id",
                        "wind_farm_id",
                    }
                    payload = {
                        name: value
                        for name, value in sample.output_record().items()
                        if name in output_fields
                    }
                    payload["sample_time"] = timestamp
                    encoded = json.dumps(
                        payload,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        allow_nan=False,
                    )
                    expiry = (
                        str(int(sample.sample_time.timestamp() * 1000) + sample.max_staleness_ms)
                        if sample.max_staleness_ms is not None
                        else ""
                    )
                    arguments.extend((encoded, timestamp, sample.sample_id, expiry))
                command = _redis_command(
                    "EVAL", _REDIS_BATCH_CAS, str(len(keys)), *keys, *arguments
                )
                started = time.perf_counter()
                writer.write(command)
                await asyncio.wait_for(writer.drain(), timeout=self._timeout)
                results = await self._integer_array(len(samples))
                latency_ms = (time.perf_counter() - started) * 1000
                # A deterministic same-timestamp identity conflict is
                # settled (not retryable) but never a successful delivery.
                # Reliable uses the error to transfer it to DLQ.
                if any(status not in (-1, 0, 1, 2) for status in results):
                    # An unexpected script result is not proof of a permanent
                    # business conflict; never send such a message to DLQ.
                    raise ConnectionError("unexpected Redis CAS result code")
                settled = tuple(
                    index for index, status in enumerate(results) if status in (-1, 0, 1, 2)
                )
                delivered = tuple(index for index, status in enumerate(results) if status in (1, 2))
                conflicts = [index for index, status in enumerate(results) if status == -1]
                error = f"Redis CAS conflicts at samples {conflicts}" if conflicts else ""
                return BatchOutcome(
                    delivered=delivered,
                    settled=settled,
                    error=error,
                    # Lua EVAL was acknowledged even when individual CAS
                    # comparisons rejected a business version.
                    successful_attempts=1,
                    failed_attempts=0,
                    successful_latencies_ms=(latency_ms,),
                    permanent=tuple(
                        index for index, status in enumerate(results) if status in (-1, 0)
                    ),
                )
            except Exception as exc:
                # The script may have executed before its reply was lost.
                # Stable sample IDs and versions protect a subsequent retry.
                self._writer = None
                self._reader = None
                writer.close()
                await writer.wait_closed()
                return BatchOutcome((), (), error=str(exc))


class KafkaOutput:
    """Broker-confirmed messages, using the unchanged upstream DeliveryPoint JSON."""

    def __init__(
        self,
        *,
        servers: list[str],
        topic: str,
        timeout_ms: int,
        credential_ref: str = "",
        linger_ms: int = 0,
        max_batch_size: int = 131072,
    ) -> None:
        if not servers or not topic:
            raise ConfigurationError("Kafka brokers and topic must be configured")
        if type(linger_ms) is not int or linger_ms != 0:
            raise ConfigurationError("Kafka linger_ms must be 0")
        if type(max_batch_size) is not int or max_batch_size <= 0:
            raise ConfigurationError("Kafka max_batch_size must be positive")
        self._servers, self._topic = servers, topic
        self._timeout_ms = timeout_ms
        self._credential_ref = credential_ref
        self._max_batch_size = max_batch_size
        self._producer: Any = None
        self._device_locks = tuple(asyncio.Lock() for _ in range(128))
        self._partition_cache: OrderedDict[bytes, int] = OrderedDict()

    def _invalidate_partition_cache(self, _cluster: Any) -> None:
        """Never reuse a partition selected from obsolete broker metadata."""
        self._partition_cache.clear()

    async def open(self) -> None:
        if self._producer is not None:
            return
        producer_class = import_module("aiokafka").AIOKafkaProducer
        options: dict[str, Any] = {
            "bootstrap_servers": self._servers,
            "acks": "all",
            "enable_idempotence": True,
            "request_timeout_ms": self._timeout_ms,
            "linger_ms": 0,
            "max_batch_size": self._max_batch_size,
        }
        credential = resolve_secret(self._credential_ref)
        if credential is not None:
            try:
                secret = json.loads(credential)
            except json.JSONDecodeError as exc:
                raise ConfigurationError("Kafka Secret must contain a JSON object") from exc
            if (
                not isinstance(secret, dict)
                or not isinstance(secret.get("username"), str)
                or not isinstance(secret.get("password"), str)
                or not secret["username"]
                or not secret["password"]
            ):
                raise ConfigurationError("Kafka Secret requires username and password")
            options.update(
                security_protocol="SASL_SSL",
                sasl_mechanism="PLAIN",
                sasl_plain_username=secret["username"],
                sasl_plain_password=secret["password"],
            )
        producer = producer_class(**options)
        try:
            await producer.start()
        except BaseException:
            await producer.stop()
            raise
        self._partition_cache.clear()
        producer.client.cluster.add_listener(self._invalidate_partition_cache)
        self._producer = producer

    async def close(self) -> None:
        producer = self._producer
        if producer is not None:
            producer.client.cluster.remove_listener(self._invalidate_partition_cache)
            self._partition_cache.clear()
            await producer.stop()
            self._producer = None

    async def write_batch(self, samples: tuple[DeliveryPoint, ...]) -> BatchOutcome:
        """Preserve the existing point-object interface for other callers."""
        stripes = tuple(
            sorted(
                {
                    hash((sample.wind_farm_id, sample.collector_id, sample.device_id))
                    % len(self._device_locks)
                    for sample in samples
                }
            )
        )
        return await self._write_records(samples, key=None, stripes=stripes)

    async def write_encoded_batch(
        self, payloads: tuple[bytes, ...], *, key: bytes, stripe: int
    ) -> BatchOutcome:
        """Write pre-encoded Reliable points via bounded Kafka BatchBuilders."""
        async with self._device_locks[stripe]:
            producer = self._producer
            if producer is None:
                return BatchOutcome((), (), "Kafka output is not connected")
            if not payloads:
                return BatchOutcome((), ())

            pending: list[tuple[int, int, Any, float]] = []
            failures: list[str] = []
            failed_attempts = 0
            try:
                partition = self._partition_cache.get(key)
                if partition is None:
                    await asyncio.wait_for(
                        producer.client._wait_on_metadata(self._topic),
                        timeout=self._timeout_ms / 1000,
                    )
                    partition = producer._partition(self._topic, None, key, None, key, None)
                    self._partition_cache[key] = partition
                    if len(self._partition_cache) > 10000:
                        self._partition_cache.popitem(last=False)
                elif key in self._partition_cache:
                    self._partition_cache.move_to_end(key)
            except Exception as exc:
                self._partition_cache.pop(key, None)
                return BatchOutcome((), (), f"Kafka partition unavailable: {exc}")

            builder = producer.create_batch()
            start = 0
            for index, payload in enumerate(payloads):
                try:
                    if builder.append(key=key, value=payload, timestamp=None) is not None:
                        continue
                    if index == start:
                        raise ValueError("Kafka record exceeds configured batch capacity")
                    builder.close()
                    queued_at = time.perf_counter()
                    try:
                        confirmation = await producer.send_batch(
                            builder, self._topic, partition=partition
                        )
                    except Exception:
                        failed_attempts += 1
                        raise
                    pending.append((start, index, confirmation, queued_at))
                    start = index
                    builder = producer.create_batch()
                    if builder.append(key=key, value=payload, timestamp=None) is None:
                        raise ValueError("Kafka record exceeds configured batch capacity")
                except Exception as exc:
                    failures.append(f"Kafka batch at sample {index} not queued: {exc}")
                    self._partition_cache.pop(key, None)
                    break
            else:
                if start < len(payloads):
                    try:
                        builder.close()
                        queued_at = time.perf_counter()
                        confirmation = await producer.send_batch(
                            builder, self._topic, partition=partition
                        )
                        pending.append((start, len(payloads), confirmation, queued_at))
                    except Exception as exc:
                        failed_attempts += 1
                        failures.append(f"Kafka batch at sample {start} not queued: {exc}")
                        self._partition_cache.pop(key, None)

            delivered: list[int] = []
            latency_weights: list[tuple[float, int]] = []
            for begin, end, confirmation, queued_at in pending:
                try:
                    await confirmation
                except Exception as exc:
                    failed_attempts += 1
                    failures.append(f"Kafka batch [{begin}, {end}) not confirmed: {exc}")
                else:
                    delivered.extend(range(begin, end))
                    latency = (time.perf_counter() - queued_at) * 1000
                    latency_weights.append((latency, end - begin))
            indexes = tuple(delivered)
            return BatchOutcome(
                delivered=indexes,
                settled=indexes,
                error="; ".join(failures),
                successful_attempts=len(latency_weights),
                failed_attempts=failed_attempts,
                successful_latency_weights=tuple(latency_weights),
            )

    async def _write_records(
        self,
        records: tuple[DeliveryPoint, ...] | tuple[bytes, ...],
        *,
        key: bytes | None,
        stripes: tuple[int, ...],
    ) -> BatchOutcome:
        # All device locks are acquired in stable order; broker confirmation
        # futures are owned until settled, even after any cache invalidation.
        async with AsyncExitStack() as ownership:
            for stripe in stripes:
                await ownership.enter_async_context(self._device_locks[stripe])
            if self._producer is None:
                return BatchOutcome((), (), "Kafka output is not connected")
            pending: list[tuple[int, Any, float]] = []
            failures: list[str] = []
            failed_attempts = 0
            for index, item in enumerate(records):
                try:
                    started = time.perf_counter()
                    if key is None:
                        sample = cast(DeliveryPoint, item)
                        record_key = ":".join(
                            (sample.wind_farm_id, sample.collector_id, sample.device_id)
                        ).encode("utf-8")
                        payload = sample.encode()
                    else:
                        record_key = key
                        payload = cast(bytes, item)
                    partition = self._partition_cache.get(record_key)
                    confirmation = await self._producer.send(
                        self._topic,
                        key=record_key,
                        value=payload,
                        partition=partition,
                    )
                except Exception as exc:
                    failed_attempts += 1
                    failures.append(f"Kafka sample {index} not queued: {exc}")
                    break
                # No submitted confirmation future may be abandoned because
                # broker metadata invalidated the partition cache.
                pending.append((index, confirmation, started))
                if partition is None:
                    try:
                        resolved = self._producer._partition(
                            self._topic, None, record_key, None, record_key, None
                        )
                    except Exception:
                        self._partition_cache.clear()
                    else:
                        self._partition_cache[record_key] = resolved
                        if len(self._partition_cache) > 10000:
                            self._partition_cache.popitem(last=False)
                elif record_key in self._partition_cache:
                    self._partition_cache.move_to_end(record_key)
            delivered: list[int] = []
            latencies: list[float] = []
            for index, confirmation, started in pending:
                try:
                    await confirmation
                except Exception as exc:
                    failed_attempts += 1
                    failures.append(f"Kafka sample {index} not confirmed: {exc}")
                else:
                    delivered.append(index)
                    latencies.append((time.perf_counter() - started) * 1000)
            result = tuple(delivered)
            return BatchOutcome(
                delivered=result,
                settled=result,
                error="; ".join(failures),
                successful_attempts=len(delivered),
                failed_attempts=failed_attempts,
                successful_latencies_ms=tuple(latencies),
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
        self._last: dict[int, tuple[datetime, str]] = {}
        self._expires: dict[int, float] = {}
        self._value_start, self._quality_start = value_start, quality_start
        self._width = len(points)

    def update(self, samples: tuple[DeliveryPoint, ...]) -> BatchOutcome:
        pending: dict[int, int] = {}
        confirmed_versions: dict[int, tuple[datetime, str]] = {}
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
                if sample.sample_time < old_time:
                    # Historical/out-of-order sample must not roll back the image.
                    settled.append(offset)
                    continue
                if sample.sample_time == old_time and sample.sample_id == old_id:
                    # A duplicate is confirmed only while the register image
                    # is still readable. Never count an expired image as output.
                    if datetime.now(UTC).timestamp() < self._expires.get(index, 0.0):
                        delivered.append(offset)
                    settled.append(offset)
                    continue
                if sample.sample_time == old_time:
                    errors.append(f"Modbus conflicting identity for sample time: {key}")
                    continue
            if isinstance(sample.value, bool) or not isinstance(sample.value, int | float):
                errors.append(f"Modbus float32 requires numeric sample: {key}")
                continue
            try:
                binary = struct.pack(">f", sample.value)
            except (ValueError, OverflowError, struct.error) as exc:
                errors.append(f"Modbus float32 invalid at {key}: {exc}")
                continue
            if (
                sample.max_staleness_ms is not None
                and datetime.now(UTC).timestamp()
                >= sample.sample_time.timestamp() + sample.max_staleness_ms / 1000
            ):
                settled.append(offset)
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
        return BatchOutcome(delivered=confirmed, settled=tuple(settled), error="; ".join(errors))

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
        self,
        device_id: int,
        func_code: int,
        address: int,
        count: int = 1,
    ) -> Any:
        from pymodbus.constants import ExcCodes

        if device_id != self._unit_id:
            return ExcCodes.ILLEGAL_ADDRESS
        if func_code != 3:
            return ExcCodes.ILLEGAL_FUNCTION
        values = self._image.read(address, count)
        return values if values is not None else ExcCodes.ILLEGAL_ADDRESS

    async def async_setValues(
        self,
        device_id: int,
        func_code: int,
        address: int,
        values: list[int] | list[bool],
    ) -> Any:
        from pymodbus.constants import ExcCodes

        return ExcCodes.ILLEGAL_FUNCTION

    def device_ids(self) -> list[int]:
        return [self._unit_id]


class _ModbusServerPort(Protocol):
    """Typed boundary for the supported pymodbus async server lifecycle."""

    async def serve_forever(self, *, background: bool) -> None: ...
    async def shutdown(self) -> None: ...


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
            points=points,
            value_start=value_start,
            quality_start=quality_start,
        )
        self._context = _ModbusContext(self._image, unit_id)
        self._server: _ModbusServerPort | None = None

    async def open(self) -> None:
        if self._server is not None:
            return
        from pymodbus.server import ModbusTcpServer

        server = cast(
            _ModbusServerPort,
            ModbusTcpServer(self._context, address=self._address),
        )
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

    async def write_batch(self, samples: tuple[DeliveryPoint, ...]) -> BatchOutcome:
        started = time.perf_counter()
        result = self._image.update(samples)
        if not result.delivered:
            return result
        return BatchOutcome(
            delivered=result.delivered,
            settled=result.settled,
            error=result.error,
            successful_attempts=1,
            successful_latencies_ms=((time.perf_counter() - started) * 1000,),
        )
