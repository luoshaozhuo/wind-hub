"""Storage physical schema and immutable ownership contract failures."""


class TDengineSchemaConflictError(RuntimeError):
    """Physical TDengine schema, tags or owner differs from frozen identity."""


class QueryRejectedError(ValueError):
    """Storage query violates the bounded wide-table request contract."""


class HistoryReplayUnavailableError(RuntimeError):
    """Kafka replay invalidates the visible history snapshot."""


class WideTableNotCreatedError(LookupError):
    """An immutable historical family has no physical table registration."""
