"""Storage physical schema and immutable ownership contract failures."""


class TDengineSchemaConflictError(RuntimeError):
    """Physical TDengine schema, tags or owner differs from frozen identity."""
