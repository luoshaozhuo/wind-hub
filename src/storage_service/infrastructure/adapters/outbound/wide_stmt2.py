"""Typed STMT2 column binding for complete device-wide TDengine rows.

The caller owns a single connection and must fence UNKNOWN I/O before retrying.
A prepared statement is not shared across threads or conflicting schema families.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol, cast

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
    schema: WideSchema,
    records: Sequence[WideRecord],
    device_types: Mapping[str, str],
    statement: WideStmt2Statement | None = None,
) -> None:
    """Write verified multi-device wide rows in one prepared STMT2 execution.

    The connection pool owns any caller-supplied statement. Statements created
    here are closed on success or failure.
    """
    if not records:
        return
    first = records[0]
    database = schema.database_name(first.wind_farm_id)
    if any(row.wind_farm_id != first.wind_farm_id for row in records):
        raise ValueError("wide STMT2 batch cannot mix wind farms")
    if len(records) > 1000:
        raise ValueError("STMT2 device-wide batch exceeds 1000 physical rows")
    by_device: dict[str, list[WideRecord]] = {}
    for row in records:
        if row.device_id not in device_types:
            raise ValueError("STMT2 target lacks verified device type")
        by_device.setdefault(row.device_id, []).append(row)
    builder = getattr(driver, "stmt2_bind_param_view", None)
    varchar_tag = getattr(driver, "varchar_to_tag", None)
    level_tag = getattr(driver, "int_to_tag", None)
    if not all(callable(fn) for fn in (builder, varchar_tag, level_tag)):
        raise RuntimeError("taosws connector lacks dynamic STMT2 binding API")
    builder_fn = cast(Callable[..., Any], builder)
    varchar_tag_fn = cast(Callable[..., Any], varchar_tag)
    level_tag_fn = cast(Callable[..., Any], level_tag)
    level = {"raw": 1, "protocol": 2, "business": 3}[schema.data_level.value]
    bindings: list[object] = []
    for device_id, group in by_device.items():
        device_type = device_types[device_id]
        if len(device_id.encode("utf-8")) > 128 or len(device_type.encode("utf-8")) > 64:
            raise ValueError("device STMT2 TAG exceeds declared width")
        columns = bind_wide_columns(schema, group, driver)
        bindings.append(
            builder_fn(
                table_name=schema.subtable_name(device_id),
                tags=[
                    varchar_tag_fn(device_id),
                    varchar_tag_fn(device_type),
                    varchar_tag_fn(schema.point_table_id),
                    level_tag_fn(level),
                    varchar_tag_fn(schema.fingerprint),
                ],
                columns=columns,
            )
        )
    owned = statement is None
    active = statement if statement is not None else connection.stmt2_statement()
    try:
        if owned:
            placeholders = ", ".join("?" for _ in range(len(schema.columns) + 2))
            tags = ", ".join("?" for _ in range(5))
            active.prepare(
                f"INSERT INTO ? USING {database}.{schema.supertable_name()} "
                f"TAGS ({tags}) VALUES ({placeholders})"
            )
        active.bind(bindings)
        if active.execute() < 0:
            raise RuntimeError("TDengine STMT2 returned a negative affected-row count")
    finally:
        if owned:
            active.close()
