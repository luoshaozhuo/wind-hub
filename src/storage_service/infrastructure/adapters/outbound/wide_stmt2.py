"""Typed STMT2 column binding for complete device-wide TDengine rows.

The caller owns a single connection and must fence UNKNOWN I/O before retrying.
A prepared statement is not shared across threads or conflicting schema families.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol

from storage_service.domain.wide_record import WideRecord
from storage_service.domain.wide_row import bind_wide_row
from storage_service.domain.wide_schema import WideSchema

_COLUMN_FACTORIES = {
    "BOOL": "bools_to_column",
    "TINYINT": "tiny_ints_to_column",
    "TINYINT UNSIGNED": "unsigned_tiny_ints_to_column",
    "SMALLINT": "small_ints_to_column",
    "SMALLINT UNSIGNED": "unsigned_small_ints_to_column",
    "INT": "ints_to_column",
    "INT UNSIGNED": "unsigned_ints_to_column",
    "BIGINT": "big_ints_to_column",
    "BIGINT UNSIGNED": "unsigned_big_ints_to_column",
    "FLOAT": "floats_to_column",
    "DOUBLE": "doubles_to_column",
}


class WideStmt2Statement(Protocol):
    def prepare(self, sql: str) -> None: ...
    def bind(self, params: list[object]) -> None: ...
    def execute(self) -> int: ...
    def close(self) -> None: ...


class WideStmt2Connection(Protocol):
    def stmt2_statement(self) -> WideStmt2Statement: ...


def _factory_name(sql_type: str) -> str:
    if sql_type in _COLUMN_FACTORIES:
        return _COLUMN_FACTORIES[sql_type]
    if sql_type.startswith("VARCHAR("):
        return "varchar_to_column"
    if sql_type.startswith("VARBINARY("):
        return "varbinary_to_column"
    raise ValueError(f"unsupported physical type: {sql_type}")


def bind_wide_columns(
    schema: WideSchema,
    records: Sequence[WideRecord],
    driver: object,
) -> list[object]:
    """Build timestamp, complete values, and 2-bit quality column vectors."""
    if not records:
        raise ValueError("STMT2 wide batch cannot be empty")
    if len(records) > 1000:
        raise ValueError("STMT2 device-wide batch exceeds 1000 physical rows")
    bound = tuple(bind_wide_row(record, schema) for record in records)
    values: list[object] = []
    kinds = ("TIMESTAMP", *(column.sql_type for column in schema.columns), "VARBINARY")
    for index, kind in enumerate(kinds):
        if index == 0:
            data: list[Any] = [record.timestamp_us for record in bound]
            factory_name = "micros_timestamps_to_column"
        elif index == len(kinds) - 1:
            data = [record.quality_bitmap for record in bound]
            factory_name = "varbinary_to_column"
        else:
            data = [record.values[index - 1] for record in bound]
            factory_name = _factory_name(kind)
        factory = getattr(driver, factory_name, None)
        if not callable(factory):
            raise RuntimeError(f"taosws connector lacks required {factory_name}")
        values.append(factory(data))
    return values


def write_wide_stmt2(
    connection: WideStmt2Connection,
    driver: object,
    *,
    table_name: str,
    schema: WideSchema,
    records: Sequence[WideRecord],
) -> None:
    """Write complete timestamp-keyed rows without read-before-write SELECT.

    This synchronous method does not retry uncertain operations. The surrounding
    lease must preserve ownership until driver I/O returns or is isolated.
    """
    if not records:
        return
    first = records[0]
    expected = (
        f"{schema.database_name(first.wind_farm_id)}."
        f"{schema.subtable_name(first.device_id)}"
    )
    if table_name != expected:
        raise ValueError("wide insert target differs from exact device schema identity")
    if any(
        row.wind_farm_id != first.wind_farm_id or row.device_id != first.device_id
        for row in records
    ):
        raise ValueError("wide STMT2 batch cannot mix device identities")
    database, subtable = expected.split(".", 1)
    columns = bind_wide_columns(schema, records, driver)
    statement = connection.stmt2_statement()
    try:
        placeholders = ", ".join("?" for _ in columns)
        statement.prepare(f"INSERT INTO {database}.{subtable} VALUES ({placeholders})")
        builder = getattr(driver, "stmt2_bind_param_view", None)
        if not callable(builder):
            raise RuntimeError("taosws connector lacks STMT2 column-binding API")
        binding = builder(table_name=None, tags=None, columns=columns)
        statement.bind([binding])
        if statement.execute() < 0:
            raise RuntimeError("TDengine STMT2 returned a negative affected-row count")
    finally:
        statement.close()
