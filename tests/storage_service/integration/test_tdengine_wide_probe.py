"""D2: isolated native TDengine wide-row capability probes.

Run against a dedicated disposable TDengine 3.3.6.13 instance using the
existing WIND_HUB_TEST_TDENGINE_* environment. No production adapter is
exercised; these probes establish native capabilities, not system acceptance.
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from importlib.metadata import version
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


@pytest.fixture
def wide_probe() -> Iterator[Probe]:
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
    connection = driver.connect(
        user=os.environ.get("WIND_HUB_TDENGINE_USER", "root"),
        password=password,
        host=host,
        port=int(port),
        read_timeout="10",
    )
    database = "whd2_" + uuid.uuid4().hex
    created = False
    try:
        assert list(connection.query("SELECT SERVER_VERSION()"))[0][0] == "3.3.6.13"
        connection.execute(
            f"CREATE DATABASE {database} PRECISION 'us' DURATION 30d KEEP 1095d "
            "VGROUPS 4 BUFFER 256 REPLICA 1 COMP 2 CACHEMODEL 'none' "
            "WAL_LEVEL 2 WAL_FSYNC_PERIOD 0"
        )
        created = True
        yield Probe(driver, connection, database)
    finally:
        try:
            if created:
                connection.execute(f"DROP DATABASE {database}")
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
        expected = raw_values
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
