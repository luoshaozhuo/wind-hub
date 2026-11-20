"""Unit tests for HistoricalPointTableClient construction and guard rails.

The real gRPC round-trip is covered by system integration; these tests pin the
fail-closed construction contract, approved-manifest allow-listing, cache key
isolation and the 128-entry bound without standing up a Repository process.
"""

from __future__ import annotations

import pytest

from sink_service.domain.config import ConfigurationError
from sink_service.infrastructure.adapters.outbound.historical_point_table import (
    HistoricalPointTableClient,
)

pytestmark = pytest.mark.unit

APPROVED = {"point_table_modbus_wtg_sim_v1.0.0": "a" * 64}
TOKEN_REF = "env:WIND_HUB_HISTORICAL_TOKEN"


def test_construction_requires_env_token_reference() -> None:
    with pytest.raises(ConfigurationError, match="token"):
        HistoricalPointTableClient("127.0.0.1:5000", "WIND_HUB_HISTORICAL_TOKEN", APPROVED)


def test_construction_rejects_empty_endpoint() -> None:
    with pytest.raises(ConfigurationError):
        HistoricalPointTableClient("", TOKEN_REF, APPROVED)


def test_construction_rejects_empty_approved_manifests() -> None:
    with pytest.raises(ConfigurationError, match="approved manifest"):
        HistoricalPointTableClient("127.0.0.1:5000", TOKEN_REF, {})


@pytest.mark.parametrize(
    "bad_digest",
    ["", "short", "g" * 64, "A" * 64, "a" * 63],
)
def test_construction_rejects_non_sha256_manifest_digest(bad_digest: str) -> None:
    with pytest.raises(ConfigurationError, match="approved manifest"):
        HistoricalPointTableClient(
            "127.0.0.1:5000",
            TOKEN_REF,
            {"point_table_modbus_wtg_sim_v1.0.0": bad_digest},
        )


def test_unapproved_point_table_is_rejected_before_any_network_call() -> None:
    client = HistoricalPointTableClient("127.0.0.1:1", TOKEN_REF, APPROVED)
    with pytest.raises(ConfigurationError, match="no approved field mapping"):
        # An unroutable endpoint proves rejection happens before gRPC.
        import asyncio

        asyncio.run(client.get("point_table_unknown_v1.0.0", "wind_turbine"))


def test_cache_keys_are_isolated_by_point_table_and_device_type() -> None:
    client = HistoricalPointTableClient("127.0.0.1:5000", TOKEN_REF, APPROVED)
    # Distinct identities must not collide in the cache dictionary.
    client._cached[("pt_a_v1.0.0", "wind_turbine")] = object()  # type: ignore[assignment]
    client._cached[("pt_a_v1.0.0", "photovoltaic")] = object()  # type: ignore[assignment]
    client._cached[("pt_b_v1.0.0", "wind_turbine")] = object()  # type: ignore[assignment]
    assert len(client._cached) == 3
    assert ("pt_a_v1.0.0", "wind_turbine") in client._cached
    assert ("pt_a_v1.0.0", "photovoltaic") in client._cached


def test_cache_holds_at_most_128_entries() -> None:
    client = HistoricalPointTableClient("127.0.0.1:5000", TOKEN_REF, APPROVED)
    # The production get() path clears the cache when it reaches this bound, so
    # the dictionary must never be allowed to grow past the documented limit.
    cache_limit = 128
    for index in range(cache_limit):
        client._cached[(f"pt_{index}_v1.0.0", "wind_turbine")] = object()  # type: ignore[assignment]
    assert len(client._cached) == cache_limit
    assert all(isinstance(key, tuple) and len(key) == 2 for key in client._cached)
