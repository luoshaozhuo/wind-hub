"""Domain exception hierarchy.

Every error that the domain layer knows about should subclass
``WindHubError``.  Specific subclasses carry semantic information
(command ids, protocol names) for callers further up the stack.
"""

from __future__ import annotations


class WindHubError(Exception):
    """Base class for all wind-hub domain exceptions."""


class ConfigError(WindHubError):
    """Raised when configuration is missing, malformed, or inconsistent.

    Examples: missing required keys, invalid YAML structure, duplicate
    device IDs.
    """


class ProtocolError(WindHubError):
    """Raised when a protocol adapter encounters a communication failure.

    Examples: TCP connection refused, Modbus exception response,
    IEC104 link timeout.
    """


class SinkError(WindHubError):
    """Raised when a sink adapter fails to write or flush data.

    Examples: Kafka broker unavailable, disk full on file sink,
    database connection lost.
    """


class CommandError(WindHubError):
    """Raised when command execution fails at the domain level.

    Carries the ``command_id`` so callers can correlate the error
    with the original Command.
    """

    def __init__(self, message: str, command_id: str) -> None:
        super().__init__(message)
        self.command_id = command_id


class OperationTimeoutError(WindHubError):
    """Raised when an operation exceeds its configured timeout.

    Distinct from Python's built-in ``TimeoutError`` to avoid
    accidental shadowing.
    """


class ProcessorError(WindHubError):
    """Raised when a processor fails during transformation.

    Covers enrichment service failures, filter configuration errors,
    and derived-point computation errors.
    """
