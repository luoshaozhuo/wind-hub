"""D2: isolated native TDengine wide-row capability probes.

Run against a dedicated disposable TDengine 3.3.6.13 instance using the
existing WIND_HUB_TEST_TDENGINE_* environment. No production adapter is
exercised; these probes establish native capabilities, not system acceptance.
"""

from __future__ import annotations

import os
import statistics
import subprocess
import time
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from importlib.metadata import version
from threading import Event
from typing import Any

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.real_service]

_POINT_COUNT = 500
_QUALITY = bytes([0x55] * 125)


@dataclass(frozen=True)
class Probe:
    """A disposable database and its native driver connection."""

    driver: Any
    connection: Any
    database: str
    connection_options: dict[str, Any] = field(repr=False)
    vgroups: int = 4
    buffer_mb: int = 256


@pytest.fixture
def wide_probe(request: pytest.FixtureRequest) -> Iterator[Probe]:
    """Require explicit opt-in and the exact server and driver versions."""
    if os.environ.get("WIND_HUB_TEST_TDENGINE_WIDE_PROBE") != "1":
        pytest.skip("enable isolated D2 probe explicitly")
    endpoint = os.environ.get("WIND_HUB_TEST_TDENGINE_ENDPOINT")
    password = os.environ.get("WIND_HUB_TEST_TDENGINE_PASSWORD")
    if not endpoint or password is None:
        pytest.fail("D2 requires a dedicated endpoint and credential")
    host, separator, port = endpoint.rpartition(":")
    if not separator or not host or not port.isdecimal():
        pytest.fail("expected TDengine host:port")
    driver = pytest.importorskip("taosws")
    assert version("taos-ws-py") == "0.7.1"
    options = {
        "user": os.environ.get("WIND_HUB_TDENGINE_USER", "root"),
        "password": password,
        "host": host,
        "port": int(port),
        "read_timeout": "10",
    }
    connection = driver.connect(**options)
    vgroups, buffer_mb = getattr(request, "param", (4, 256))
    assert vgroups in (2, 4, 8) and buffer_mb in (128, 256, 512)
    database = "whd2_" + uuid.uuid4().hex
    created = False
    try:
        assert list(connection.query("SELECT SERVER_VERSION()"))[0][0] == "3.3.6.13"
        connection.execute(
            f"CREATE DATABASE {database} PRECISION 'us' DURATION 30d KEEP 1095d "
            f"VGROUPS {vgroups} BUFFER {buffer_mb} REPLICA 1 COMP 2 CACHEMODEL 'none' "
            "WAL_LEVEL 2 WAL_FSYNC_PERIOD 0"
        )
        created = True
        yield Probe(driver, connection, database, options, vgroups, buffer_mb)
    finally:
        try:
            if created:
                cleanup = driver.connect(**options)
                try:
                    cleanup.execute(f"DROP DATABASE {database}")
                finally:
                    cleanup.close()
        finally:
            connection.close()


def _create_family(probe: Probe, level: str, kind: str, devices: int = 2) -> None:
    """Create a versioned 500-column family and distinct device subtables."""
    columns = ", ".join(f"p{i:03d}_v1_0_0 {kind}" for i in range(_POINT_COUNT))
    db = probe.database
    probe.connection.execute(
        f"CREATE STABLE {db}.{level}_v1_0_0 "
        f"(ts TIMESTAMP, {columns}, quality VARBINARY(125)) TAGS (device_id VARCHAR(64))"
    )
    for index in range(devices):
        probe.connection.execute(
            f"CREATE TABLE {db}.{level}_d{index} "
            f"USING {db}.{level}_v1_0_0 TAGS ('wtg_{index:03d}')"
        )


def _bind(
    probe: Probe,
    *,
    table: str | None,
    timestamps: list[int],
    base: int,
    missing_first: bool = False,
) -> Any:
    """Build distinct changing values and the corresponding quality column."""
    driver = probe.driver
    columns = [driver.micros_timestamps_to_column(timestamps)]
    for point in range(_POINT_COUNT):
        values = [
            None if missing_first and point == 0 else base + point + index
            for index in range(len(timestamps))
        ]
        columns.append(driver.big_ints_to_column(values))
    quality = bytes([0x54]) + _QUALITY[1:] if missing_first else _QUALITY
    columns.append(driver.varbinary_to_column([quality] * len(timestamps)))
    return driver.stmt2_bind_param_view(table_name=table, tags=None, columns=columns)


def _single_statement(probe: Probe, table: str) -> Any:
    """Prepare a complete-row statement for one precreated table."""
    statement = probe.connection.stmt2_statement()
    try:
        placeholders = ", ".join(["?"] * (_POINT_COUNT + 2))
        statement.prepare(f"INSERT INTO {probe.database}.{table} VALUES ({placeholders})")
    except BaseException:
        statement.close()
        raise
    return statement


def _assert_row(
    probe: Probe,
    table: str,
    timestamp: int,
    base: int,
    *,
    missing_first: bool = False,
) -> None:
    """Check every physical point and quality byte independently."""
    rows = list(
        probe.connection.query(
            f"SELECT * FROM {probe.database}.{table} WHERE ts = {timestamp}"
        )
    )
    assert len(rows) == 1
    row = rows[0]
    assert len(row) == _POINT_COUNT + 2
    expected = [base + i for i in range(_POINT_COUNT)]
    if missing_first:
        expected[0] = None
    assert list(row[1:-1]) == expected
    quality = bytes([0x54]) + _QUALITY[1:] if missing_first else _QUALITY
    assert bytes(row[-1]) == quality


def test_td08_full_row_and_null_overwrite(wide_probe: Probe) -> None:
    """A later complete row must replace all previous values including NULL."""
    probe = wide_probe
    _create_family(probe, "protocol", "BIGINT")
    ts = time.time_ns() // 1000
    for base, missing in [(100, False), (100, False), (200, True)]:
        statement = _single_statement(probe, "protocol_d0")
        try:
            statement.bind(
                [_bind(probe, table=None, timestamps=[ts], base=base, missing_first=missing)]
            )
            assert statement.execute() >= 0
        finally:
            statement.close()
        _assert_row(probe, "protocol_d0", ts, base, missing_first=missing)


def test_td02_td03_multitable_and_statement_reuse(wide_probe: Probe) -> None:
    """Probe dynamic table binding and repeated execute on one prepared object.

    Unsupported syntax/API is a real failure, not a silent single-table fallback.
    """
    probe = wide_probe
    _create_family(probe, "protocol", "BIGINT")
    ts = time.time_ns() // 1000
    placeholders = ", ".join(["?"] * (_POINT_COUNT + 2))
    probe.connection.execute(f"USE {probe.database}")
    statement = probe.connection.stmt2_statement()
    try:
        statement.prepare(f"INSERT INTO ? VALUES ({placeholders})")
        for cycle in range(2):
            moment = ts + cycle * 100_000
            statement.bind(
                [
                    _bind(
                        probe,
                        table=f"protocol_d{device}",
                        timestamps=[moment],
                        base=cycle * 1000 + device * 10000,
                    )
                    for device in range(2)
                ]
            )
            assert statement.execute() >= 0
            for device in range(2):
                _assert_row(
                    probe,
                    f"protocol_d{device}",
                    moment,
                    cycle * 1000 + device * 10000,
                )
    finally:
        statement.close()


@pytest.mark.parametrize(
    ("level", "kind"),
    [("raw", "VARBINARY(8)"), ("protocol", "FLOAT"), ("business", "DOUBLE")],
)
def test_td11_wide_schema_and_quality(wide_probe: Probe, level: str, kind: str) -> None:
    """Create and inspect 500 distinct columns in each data level."""
    probe = wide_probe
    _create_family(probe, level, kind)
    table = f"{probe.database}.{level}_d0"
    described = list(probe.connection.query(f"DESCRIBE {table}"))
    assert len(described) == _POINT_COUNT + 3
    assert str(described[0][0]).lower() == "ts"
    for index, row in enumerate(described[1 : _POINT_COUNT + 1]):
        assert str(row[0]).lower() == f"p{index:03d}_v1_0_0"
        assert str(row[1]).upper() == kind.split("(")[0]
        if level == "raw":
            assert int(row[2]) == 8
    quality = described[_POINT_COUNT + 1]
    assert str(quality[0]).lower() == "quality"
    assert str(quality[1]).upper() == "VARBINARY"
    assert int(quality[2]) == 125
    driver = probe.driver
    ts = time.time_ns() // 1000
    raw_values = [bytes([0, 255, index % 256, index // 256]) for index in range(_POINT_COUNT)]
    numeric_values = [index + 0.25 for index in range(_POINT_COUNT)]
    if level == "raw":
        columns = [driver.varbinary_to_column([value]) for value in raw_values]
        expected: list[bytes] | list[float] = raw_values
    elif level == "protocol":
        columns = [driver.floats_to_column([value]) for value in numeric_values]
        expected = numeric_values
    else:
        columns = [driver.doubles_to_column([value]) for value in numeric_values]
        expected = numeric_values
    statement = _single_statement(probe, f"{level}_d0")
    try:
        binding = driver.stmt2_bind_param_view(
            table_name=None,
            tags=None,
            columns=[
                driver.micros_timestamps_to_column([ts]),
                *columns,
                driver.varbinary_to_column([_QUALITY]),
            ],
        )
        statement.bind([binding])
        assert statement.execute() >= 0
    finally:
        statement.close()
    rows = list(probe.connection.query(f"SELECT * FROM {table} WHERE ts = {ts}"))
    assert len(rows) == 1
    assert list(rows[0][1:-1]) == expected
    assert bytes(rows[0][-1]) == _QUALITY


def test_td12_database_parameters(wide_probe: Probe) -> None:
    """Verify actual server DDL, not merely successful CREATE DATABASE."""
    ddl = str(
        list(wide_probe.connection.query(f"SHOW CREATE DATABASE {wide_probe.database}"))[0][-1]
    ).upper()
    normalized = " ".join(ddl.split())
    for fragment in (
        "PRECISION 'US'",
        "VGROUPS 4",
        "BUFFER 256",
        "REPLICA 1",
        "COMP 2",
        "CACHEMODEL 'NONE'",
        "WAL_LEVEL 2",
        "WAL_FSYNC_PERIOD 0",
    ):
        assert fragment in normalized, ddl
    assert "DURATION 30D" in normalized or "DURATION 43200M" in normalized, ddl
    assert "KEEP 1095D" in normalized or "KEEP 1576800M" in normalized, ddl


def _run_writer(
    probe: Probe,
    devices: list[int],
    *,
    start_us: int,
    cycles: int,
    batch_cycles: int,
) -> list[float]:
    """Write one bounded device stripe with an independent connection."""
    connection = probe.driver.connect(**probe.connection_options)
    local = Probe(probe.driver, connection, probe.database, probe.connection_options)
    timings: list[float] = []
    statement = None
    try:
        connection.execute(f"USE {probe.database}")
        statement = connection.stmt2_statement()
        placeholders = ", ".join(["?"] * (_POINT_COUNT + 2))
        statement.prepare(f"INSERT INTO ? VALUES ({placeholders})")
        for first in range(0, cycles, batch_cycles):
            end = min(first + batch_cycles, cycles)
            timestamps = [start_us + i * 100_000 for i in range(first, end)]
            started = time.perf_counter()
            statement.bind(
                [
                    _bind(
                        local,
                        table=f"protocol_d{device}",
                        timestamps=timestamps,
                        base=device * 1_000_000 + first,
                    )
                    for device in devices
                ]
            )
            assert statement.execute() >= 0
            timings.append(time.perf_counter() - started)
    finally:
        try:
            if statement is not None:
                statement.close()
        finally:
            connection.close()
    return timings


def _verify_workload(probe: Probe, devices: int, cycles: int) -> None:
    """Check every physical value and quality byte after timed ingestion."""
    for device in range(devices):
        rows = probe.connection.query(
            f"SELECT * FROM {probe.database}.protocol_d{device} ORDER BY ts"
        )
        count = 0
        for index, row in enumerate(rows):
            expected = [device * 1_000_000 + index + point for point in range(_POINT_COUNT)]
            assert list(row[1:-1]) == expected
            assert bytes(row[-1]) == _QUALITY
            count += 1
        assert count == cycles


def _read_while_writing(probe: Probe, stop: Event, ready: Event) -> int:
    """Run independent historical queries until the writer completes."""
    connection = probe.driver.connect(**probe.connection_options)
    count = 0
    try:
        ready.set()
        while not stop.is_set():
            list(connection.query(f"SELECT COUNT(*) FROM {probe.database}.protocol_v1_0_0"))
            count += 1
            stop.wait(0.1)
    finally:
        connection.close()
    return count


@pytest.mark.performance
@pytest.mark.slow
@pytest.mark.parametrize(
    "wide_probe",
    [(2, 256), (4, 256), (8, 256), (4, 128), (4, 512)],
    indirect=True,
)
@pytest.mark.parametrize(("batch_cycles", "workers"), [(1, 1), (10, 1), (10, 4)])
@pytest.mark.parametrize("concurrent_read", [False, True])
def test_td01_td04_td05_td06_td10_capacity_matrix(
    wide_probe: Probe,
    batch_cycles: int,
    workers: int,
    concurrent_read: bool,
    record_property: Any,
) -> None:
    """Measure native ingestion alone and with independent query contention."""
    if os.environ.get("WIND_HUB_TEST_TDENGINE_WIDE_CAPACITY") != "1":
        pytest.skip("enable D2 capacity matrix explicitly")
    probe = wide_probe
    devices = 30
    cycles = int(os.environ.get("WIND_HUB_TEST_TDENGINE_WIDE_CYCLES", "600"))
    assert 600 <= cycles <= 36_000
    _create_family(probe, "protocol", "BIGINT", devices=devices)
    start_us = time.time_ns() // 1000 - cycles * 100_000
    stop = Event()
    ready = Event()
    stripes = [list(range(worker, devices, workers)) for worker in range(workers)]
    with ThreadPoolExecutor(max_workers=workers + 1) as pool:
        reader = (
            pool.submit(_read_while_writing, probe, stop, ready) if concurrent_read else None
        )
        try:
            if reader is not None:
                assert ready.wait(timeout=10)
            started = time.perf_counter()
            futures = [
                pool.submit(
                    _run_writer,
                    probe,
                    stripe,
                    start_us=start_us,
                    cycles=cycles,
                    batch_cycles=batch_cycles,
                )
                for stripe in stripes
            ]
            timings = [duration for future in futures for duration in future.result()]
            elapsed = time.perf_counter() - started
        finally:
            stop.set()
        reads = reader.result() if reader is not None else 0
    _verify_workload(probe, devices, cycles)
    rows = devices * cycles
    record_property("vgroups", probe.vgroups)
    record_property("buffer_mb", probe.buffer_mb)
    record_property("rows", rows)
    record_property("elapsed_seconds", elapsed)
    record_property("rows_per_second", rows / elapsed)
    record_property("points_per_second", rows * _POINT_COUNT / elapsed)
    record_property("batch_seconds_median", statistics.median(timings))
    record_property("batch_seconds_max", max(timings))
    record_property("concurrent_queries", reads)
    if concurrent_read:
        assert reads > 0
    if probe.vgroups == 4 and probe.buffer_mb == 256 and workers == 4:
        if not concurrent_read:
            assert rows / elapsed >= 300


@pytest.mark.fault_injection
@pytest.mark.restart
def test_td07_acknowledged_rows_survive_container_restart(wide_probe: Probe) -> None:
    """Check acknowledged WAL persistence on an explicitly dedicated container."""
    if os.environ.get("WIND_HUB_TEST_TDENGINE_WIDE_RESTART") != "1":
        pytest.skip("enable dedicated destructive restart experiment explicitly")
    container = os.environ.get("WIND_HUB_TEST_TDENGINE_CONTAINER")
    if not container:
        pytest.fail("dedicated TDengine container name is required")
    probe = wide_probe
    _create_family(probe, "protocol", "BIGINT")
    ts = time.time_ns() // 1000
    statement = _single_statement(probe, "protocol_d0")
    try:
        statement.bind([_bind(probe, table=None, timestamps=[ts], base=123)])
        assert statement.execute() >= 0
    finally:
        statement.close()
    subprocess.run(["docker", "kill", container], check=True, timeout=30)
    subprocess.run(["docker", "start", container], check=True, timeout=30)
    deadline = time.monotonic() + 120
    while True:
        try:
            connection = probe.driver.connect(**probe.connection_options)
        except Exception:
            if time.monotonic() >= deadline:
                raise
            time.sleep(1)
        else:
            break
    try:
        recovered = Probe(probe.driver, connection, probe.database, probe.connection_options)
        _assert_row(recovered, "protocol_d0", ts, 123)
    finally:
        connection.close()


def test_td11_reject_excessive_declared_row_width(wide_probe: Probe) -> None:
    """Reject an excessive RAW row declaration instead of silently truncating."""
    probe = wide_probe
    columns = ", ".join(f"p{i:03d} VARBINARY(2048)" for i in range(_POINT_COUNT))
    with pytest.raises(Exception) as error:
        probe.connection.execute(
            f"CREATE STABLE {probe.database}.oversized "
            f"(ts TIMESTAMP, {columns}, quality VARBINARY(125)) TAGS (device_id INT)"
        )
    message = str(error.value).lower()
    assert any(token in message for token in ("row", "length", "size", "column")), message
