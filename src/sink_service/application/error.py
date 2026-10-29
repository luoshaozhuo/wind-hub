"""Sink output errors shared by infrastructure implementations."""


class SinkError(Exception):
    """A sink failed to open, deliver or flush output."""
