"""Gateway v1 target leasing and fail-closed injection boundary."""

from __future__ import annotations

import pytest

from contracts import industrial_gateway_pb2 as pb
from industrial_gateway.application.variable_store import VariableStore
from industrial_gateway.infrastructure.variable_leases import (
    LeaseError,
    VariableLeases,
)

pytestmark = pytest.mark.unit


async def test_claim_fences_unleased_writer_and_allows_owner() -> None:
    leases = VariableLeases()
    store = VariableStore({"power": "float32"})
    owned = await leases.claim(
        pb.ClaimTargetsRequest(
            gateway_id="gw1",
            owner_id="generation-1",
            variable_ids=["power"],
            action="ACQUIRE",
            lease_ms=10000,
        ),
        frozenset({"power"}),
    )
    assert owned.accepted and owned.lease_token
    # 冻结契约：无有效租约一律 LEASE_LOST，无论目标是否被占用
    with pytest.raises(LeaseError, match="LEASE_LOST"):
        await leases.update("gw1", store, [("power", 10.0)], "")
    assert store.read("power").value is None
    empty = VariableLeases()
    with pytest.raises(LeaseError, match="LEASE_LOST"):
        await empty.update("gw2", VariableStore({"power": "float32"}), [("power", 10.0)], "")
    result = await leases.update("gw1", store, [("power", 10.0)], owned.lease_token)
    assert result[0].success
    assert store.read("power").value == 10.0


async def test_claim_acquisition_is_atomic_across_named_targets() -> None:
    leases = VariableLeases()
    first = await leases.claim(
        pb.ClaimTargetsRequest(
            gateway_id="gw1",
            owner_id="generation-1",
            variable_ids=["p1"],
            action="ACQUIRE",
            lease_ms=10000,
        ),
        frozenset({"p1", "p2"}),
    )
    assert first.accepted
    second = await leases.claim(
        pb.ClaimTargetsRequest(
            gateway_id="gw1",
            owner_id="generation-2",
            variable_ids=["p1", "p2"],
            action="ACQUIRE",
            lease_ms=10000,
        ),
        frozenset({"p1", "p2"}),
    )
    assert not second.accepted
    assert second.error_code == "TARGET_CLAIMED"
    free = await leases.claim(
        pb.ClaimTargetsRequest(
            gateway_id="gw1",
            owner_id="generation-2",
            variable_ids=["p2"],
            action="ACQUIRE",
            lease_ms=10000,
        ),
        frozenset({"p1", "p2"}),
    )
    assert free.accepted


async def test_renew_release_and_old_token_fencing() -> None:
    leases = VariableLeases()
    request = pb.ClaimTargetsRequest(
        gateway_id="gw1",
        owner_id="generation-1",
        variable_ids=["p1"],
        action="ACQUIRE",
        lease_ms=10000,
    )
    acquired = await leases.claim(request, frozenset({"p1"}))
    assert acquired.accepted
    renewed = await leases.claim(
        pb.ClaimTargetsRequest(
            gateway_id="gw1",
            owner_id="generation-1",
            variable_ids=["p1"],
            action="RENEW",
            lease_token=acquired.lease_token,
            lease_ms=10000,
        ),
        frozenset({"p1"}),
    )
    assert renewed.accepted
    assert renewed.lease_token == acquired.lease_token
    released = await leases.claim(
        pb.ClaimTargetsRequest(
            gateway_id="gw1",
            owner_id="generation-1",
            variable_ids=["p1"],
            action="RELEASE",
            lease_token=acquired.lease_token,
        ),
        frozenset({"p1"}),
    )
    assert released.accepted
    stale = await leases.claim(
        pb.ClaimTargetsRequest(
            gateway_id="gw1",
            owner_id="generation-1",
            variable_ids=["p1"],
            action="RELEASE",
            lease_token=acquired.lease_token,
        ),
        frozenset({"p1"}),
    )
    assert not stale.accepted and stale.error_code == "LEASE_LOST"
    with pytest.raises(LeaseError, match="LEASE_LOST"):
        await leases.update(
            "gw1", VariableStore({"p1": "int32"}), [("p1", 1)], acquired.lease_token
        )


async def test_claimed_point_missing_from_store_returns_item_level_not_found() -> None:
    """租约有效但点已从存储移除：逐项 NOT_FOUND，不放宽为整批失败。"""
    leases = VariableLeases()
    acquired = await leases.claim(
        pb.ClaimTargetsRequest(
            gateway_id="gw1",
            owner_id="generation-1",
            variable_ids=["p1"],
            action="ACQUIRE",
            lease_ms=10000,
        ),
        frozenset({"p1"}),
    )
    assert acquired.accepted
    result = await leases.update("gw1", VariableStore({}), [("p1", 1)], acquired.lease_token)
    assert result[0].variable_id == "p1"
    assert not result[0].success
    assert result[0].error_code == "NOT_FOUND"


async def test_batch_preflight_blocks_all_values_before_mutation() -> None:
    leases = VariableLeases()
    store = VariableStore({"p1": "int32", "p2": "int32"})
    acquired = await leases.claim(
        pb.ClaimTargetsRequest(
            gateway_id="gw1",
            owner_id="generation-1",
            variable_ids=["p1"],
            action="ACQUIRE",
            lease_ms=10000,
        ),
        frozenset({"p1", "p2"}),
    )
    with pytest.raises(LeaseError, match="LEASE_LOST"):
        await leases.update("gw1", store, [("p1", 1), ("p2", 2)], acquired.lease_token)
    assert store.read("p1").value is None
    assert store.read("p2").value is None


async def test_expired_claim_can_be_replaced(monkeypatch: pytest.MonkeyPatch) -> None:
    import industrial_gateway.infrastructure.variable_leases as module

    clock = [0.0]
    monkeypatch.setattr(module, "monotonic", lambda: clock[0])
    leases = VariableLeases()
    old = await leases.claim(
        pb.ClaimTargetsRequest(
            gateway_id="gw1",
            owner_id="generation-1",
            variable_ids=["p1"],
            action="ACQUIRE",
            lease_ms=1000,
        ),
        frozenset({"p1"}),
    )
    clock[0] = 1.1
    new = await leases.claim(
        pb.ClaimTargetsRequest(
            gateway_id="gw1",
            owner_id="generation-2",
            variable_ids=["p1"],
            action="ACQUIRE",
            lease_ms=1000,
        ),
        frozenset({"p1"}),
    )
    assert old.accepted and new.accepted
    assert old.lease_token != new.lease_token
    with pytest.raises(LeaseError, match="LEASE_LOST"):
        await leases.update("gw1", VariableStore({"p1": "int32"}), [("p1", 1)], old.lease_token)
