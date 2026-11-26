"""Realtime Redis, Kafka and Modbus TCP Server outputs.

These adapters do not parse NATS messages, select source points, or run schedulers.
"""

from __future__ import annotations

import asyncio
import json
import struct
import time
from collections import OrderedDict
from dataclasses import dataclass, field, replace
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
                    payload = {
                        "wind_farm_id": sample.wind_farm_id,
                        "collector_id": sample.collector_id,
                        "device_id": sample.device_id,
                        "point_id": sample.point_id,
                        "sample_id": sample.sample_id,
                        "value": sample.value,
                        "quality": sample.quality,
                        "sample_time": timestamp,
                        "publish_time": sample.publish_time.isoformat().replace("+00:00", "Z"),
                    }
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


@dataclass(slots=True)
class _KafkaWrite:
    """One source write awaiting partition-local physical batch settlement."""

    key: bytes
    payloads: tuple[bytes, ...]
    result: asyncio.Future[BatchOutcome]
    enqueued_at: float
    delivered: list[int] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    latency_weights: list[tuple[float, int]] = field(default_factory=list)
    successful_attempts: int = 0
    failed_attempts: int = 0
    submit_seconds: float = 0.0


# Each physical Builder owns contiguous slices of one or more original writes.
_KafkaSlice = tuple[_KafkaWrite, int, int]
_KafkaConfirmation = tuple[list[_KafkaSlice], Any, float]


class KafkaOutput:
    """Broker-confirmed Kafka JSON v3 device rows with partition-local batching."""

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
        if type(linger_ms) is not int or not 0 <= linger_ms <= 10:
            raise ConfigurationError("Kafka linger_ms must be 0..10")
        if type(max_batch_size) is not int or max_batch_size <= 0:
            raise ConfigurationError("Kafka max_batch_size must be positive")
        self._servers, self._topic = servers, topic
        self._timeout_ms = timeout_ms
        self._credential_ref = credential_ref
        self._max_batch_size = max_batch_size
        self._linger_ms = linger_ms
        self._producer: Any = None
        self._device_locks = tuple(asyncio.Lock() for _ in range(128))
        self._partition_cache: OrderedDict[bytes, int] = OrderedDict()
        self._partition_queues: dict[int, list[_KafkaWrite]] = {}
        self._partition_workers: dict[int, asyncio.Task[None]] = {}
        self._settlements: set[asyncio.Task[None]] = set()
        self._closing = False

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
        self._closing = False

    async def close(self) -> None:
        producer = self._producer
        if producer is not None:
            # Stop admission before draining submissions and their confirmations.
            # Normal STOP/Reload must never cancel an owned Broker Future.
            self._closing = True
            workers = tuple(self._partition_workers.values())
            if workers:
                await asyncio.gather(*(asyncio.shield(task) for task in workers))
            settlements = tuple(self._settlements)
            if settlements:
                await asyncio.gather(*(asyncio.shield(task) for task in settlements))
            producer.client.cluster.remove_listener(self._invalidate_partition_cache)
            self._partition_cache.clear()
            await producer.stop()
            self._producer = None

    async def write_batch(self, samples: tuple[DeliveryPoint, ...]) -> BatchOutcome:
        """Reject non-v3 point-oriented writes; Kafka requires encoded device rows."""
        if not samples:
            return BatchOutcome((), ())
        return BatchOutcome((), (), "Kafka requires encoded complete device-wide records")

    async def write_encoded_batch(
        self, payloads: tuple[bytes, ...], *, key: bytes, stripe: int
    ) -> BatchOutcome:
        """Queue one Reliable write while retaining its device order and result.

        Args:
            payloads: Pre-encoded Kafka records, in source order.
            key: Immutable device routing key.
            stripe: Device lock index.

        Returns:
            Confirmation and failure indexes local to this write.
        """
        lock_started = time.perf_counter()
        lock = self._device_locks[stripe]
        await lock.acquire()
        release_lock = True
        try:
            lock_seconds = time.perf_counter() - lock_started
            metadata_seconds = 0.0
            producer = self._producer
            if producer is None or self._closing:
                return BatchOutcome((), (), "Kafka output is not connected")
            if not payloads:
                return BatchOutcome((), ())

            try:
                partition = self._partition_cache.get(key)
                if partition is None:
                    metadata_started = time.perf_counter()
                    try:
                        await asyncio.wait_for(
                            producer.client._wait_on_metadata(self._topic),
                            timeout=self._timeout_ms / 1000,
                        )
                    finally:
                        metadata_seconds += time.perf_counter() - metadata_started
                    partition = producer._partition(self._topic, None, key, None, key, None)
                    self._partition_cache[key] = partition
                    if len(self._partition_cache) > 10000:
                        self._partition_cache.popitem(last=False)
                elif key in self._partition_cache:
                    self._partition_cache.move_to_end(key)
            except Exception as exc:
                self._partition_cache.pop(key, None)
                return BatchOutcome((), (), f"Kafka partition unavailable: {exc}")

            if self._closing or producer is not self._producer:
                self._partition_cache.pop(key, None)
                return BatchOutcome((), (), "Kafka output is not connected")
            result: asyncio.Future[BatchOutcome] = asyncio.get_running_loop().create_future()
            request = _KafkaWrite(key, payloads, result, time.perf_counter())
            queue = self._partition_queues.setdefault(partition, [])
            queue.append(request)
            if partition not in self._partition_workers:
                task = asyncio.create_task(self._drain_partition(partition, producer))
                self._partition_workers[partition] = task
                task.add_done_callback(self._observe_worker)

            try:
                outcome = await asyncio.shield(result)
            except asyncio.CancelledError:
                # The native write still owns the device until its result settles.
                # The quota layer separately quarantines cancelled native I/O.
                if not result.done():
                    result.add_done_callback(lambda _result: lock.release())
                    release_lock = False
                raise

            stages = outcome.kafka_stage_seconds
            assert stages is not None
            return replace(
                outcome,
                kafka_stage_seconds=(lock_seconds, metadata_seconds, stages[2], stages[3]),
            )
        finally:
            if release_lock:
                lock.release()

    def _observe_worker(self, task: asyncio.Task[None]) -> None:
        """Fail closed on unexpected dispatcher or settlement termination."""
        if task.cancelled() or task.exception() is not None:
            self._closing = True

    def _settlement_finished(self, task: asyncio.Task[None]) -> None:
        self._settlements.discard(task)
        self._observe_worker(task)

    async def _drain_partition(self, partition: int, producer: Any) -> None:
        """Submit ready writes in FIFO order, without waiting for earlier ACKs."""
        queue = self._partition_queues[partition]
        try:
            # Give already runnable device writes a chance to join the Builder.
            # There is no timer-based linger and no additional admission quota.
            await asyncio.sleep(self._linger_ms / 1000)
            while queue:
                requests = queue[:]
                queue.clear()
                try:
                    pending, error = await self._submit_group(partition, producer, requests)
                    task = asyncio.create_task(self._settle_group(requests, pending, error))
                    self._settlements.add(task)
                    task.add_done_callback(self._settlement_finished)
                except BaseException:
                    # An unexpected abort is not evidence of successful I/O.
                    self._closing = True
                    for request in requests:
                        request.result.cancel()
                    raise
        finally:
            for request in queue:
                request.result.cancel()
            del self._partition_queues[partition]
            del self._partition_workers[partition]

    async def _submit_builder(
        self,
        producer: Any,
        partition: int,
        builder: Any,
        members: list[_KafkaSlice],
        pending: list[_KafkaConfirmation],
    ) -> None:
        """Submit one physical Builder and retain every source slice it owns."""
        builder.close()
        submitted_at = time.perf_counter()
        try:
            confirmation = await producer.send_batch(builder, self._topic, partition=partition)
        except Exception:
            members[0][0].failed_attempts += 1
            raise
        finally:
            now = time.perf_counter()
            for request, _begin, _end in members:
                request.submit_seconds = now - request.enqueued_at
        pending.append((members, confirmation, submitted_at))

    async def _submit_group(
        self,
        partition: int,
        producer: Any,
        requests: list[_KafkaWrite],
    ) -> tuple[list[_KafkaConfirmation], str]:
        """Fill physical Builders across device boundaries in one Partition."""
        pending: list[_KafkaConfirmation] = []
        members: list[_KafkaSlice] = []
        error = ""
        try:
            builder = producer.create_batch()
            for request in requests:
                begin = 0
                for index, payload in enumerate(request.payloads):
                    if builder.append(key=request.key, value=payload, timestamp=None) is not None:
                        continue
                    if index > begin:
                        members.append((request, begin, index))
                    if not members:
                        raise ValueError("Kafka record exceeds configured batch capacity")
                    await self._submit_builder(producer, partition, builder, members, pending)
                    builder = producer.create_batch()
                    members = []
                    begin = index
                    if builder.append(key=request.key, value=payload, timestamp=None) is None:
                        raise ValueError("Kafka record exceeds configured batch capacity")
                members.append((request, begin, len(request.payloads)))
            if members:
                await self._submit_builder(producer, partition, builder, members, pending)
        except Exception as exc:
            error = f"Kafka batch not queued: {exc}"
            for request in requests:
                self._partition_cache.pop(request.key, None)
                request.submit_seconds = time.perf_counter() - request.enqueued_at
        return pending, error

    @staticmethod
    async def _settle_group(
        requests: list[_KafkaWrite],
        pending: list[_KafkaConfirmation],
        queue_error: str,
    ) -> None:
        """Fan physical confirmations out to the original zero-based indexes.

        Count each physical attempt once, on its first source slice. Latency
        weights and delivery indexes always belong to the actual source write.
        """
        try:
            for members, confirmation, submitted_at in pending:
                try:
                    await confirmation
                except Exception as exc:
                    members[0][0].failed_attempts += 1
                    for request, begin, end in members:
                        request.failures.append(
                            f"Kafka batch [{begin}, {end}) not confirmed: {exc}"
                        )
                else:
                    members[0][0].successful_attempts += 1
                    latency_ms = (time.perf_counter() - submitted_at) * 1000
                    for request, begin, end in members:
                        request.delivered.extend(range(begin, end))
                        request.latency_weights.append((latency_ms, end - begin))

            now = time.perf_counter()
            for request in requests:
                if queue_error and len(request.delivered) < len(request.payloads):
                    request.failures.append(queue_error)
                indexes = tuple(request.delivered)
                request.result.set_result(
                    BatchOutcome(
                        delivered=indexes,
                        settled=indexes,
                        error="; ".join(request.failures),
                        successful_attempts=request.successful_attempts,
                        failed_attempts=request.failed_attempts,
                        successful_latency_weights=tuple(request.latency_weights),
                        kafka_stage_seconds=(
                            0.0,
                            0.0,
                            request.submit_seconds,
                            max(0.0, now - request.enqueued_at - request.submit_seconds),
                        ),
                    )
                )
        finally:
            # Never turn an unexpectedly aborted settlement into a negative ACK.
            # Cancellation propagates to the native quota's quarantine path.
            for request in requests:
                if not request.result.done():
                    request.result.cancel()

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
