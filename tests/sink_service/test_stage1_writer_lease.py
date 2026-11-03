"""Exclusive Writer lease: a second Writer must fail closed."""

from __future__ import annotations

from pathlib import Path

import pytest

from sink_service.domain.config import ConfigurationError
from sink_service.infrastructure.writer_lease import WriterLease

pytestmark = [pytest.mark.unit, pytest.mark.reliability]


def test_second_writer_is_rejected(tmp_path: Path) -> None:
    first = WriterLease(tmp_path)
    second = WriterLease(tmp_path)
    first.acquire()
    try:
        with pytest.raises(ConfigurationError, match="another configuration Writer"):
            second.acquire()
    finally:
        first.release()


def test_lease_is_reacquirable_after_release(tmp_path: Path) -> None:
    lease = WriterLease(tmp_path)
    lease.acquire()
    lease.release()
    lease.acquire()
    lease.release()


def test_double_acquire_same_instance_is_rejected(tmp_path: Path) -> None:
    lease = WriterLease(tmp_path)
    lease.acquire()
    with pytest.raises(ConfigurationError, match="already acquired"):
        lease.acquire()
    lease.release()
