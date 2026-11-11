"""Gateway v1 target leasing and fail-closed injection boundary."""

from __future__ import annotations

import pytest

from industrial_gateway.application.variable_store import VariableStore
from industrial_gateway.infrastructure.variable_leases import (
    ClaimRequest,
    LeaseError,
    VariableLeases,
)

pytestmark = pytest.mark.unit

PRINCIPAL_A = "cn-injector-a"
PRINCIPAL_B = "cn-injector-b"


def _claim(
    *,
    gateway_id: str = "gw1",
    owner_id: str = "generation-1",
    principal: str = PRINCIPAL_A,
    variable_ids: tuple[str, ...] = ("power",),
    action: str = "ACQUIRE",
    lease_token: str = "",
    lease_ms: int = 10000,
) -> ClaimRequest:
    return ClaimRequest(
        gateway_id=gateway_id,
        owner_id=owner_id,
        principal=principal,
        variable_ids=variable_ids,
        action=action,
        lease_token=lease_token,
        lease_ms=lease_ms,
    )


async def test_claim_fences_unleased_writer_and_allows_owner() -> None:
    leases = VariableLeases()
    store = VariableStore({"power": "float32"})
    owned = await leases.claim(_claim(variable_ids=("power",)), frozenset({"power"}))
    assert owned.accepted and owned.lease_token
    # 冻结契约：无有效租约一律 LEASE_LOST，无论目标是否被占用
    with pytest.raises(LeaseError, match="LEASE_LOST"):
        await leases.update("gw1", store, [("power", 10.0)], "", PRINCIPAL_A)
    assert store.read("power").value is None
    empty = VariableLeases()
    with pytest.raises(LeaseError, match="LEASE_LOST"):
        await empty.update(
            "gw2", VariableStore({"power": "float32"}), [("power", 10.0)], "", PRINCIPAL_A
        )
    result = await leases.update("gw1", store, [("power", 10.0)], owned.lease_token, PRINCIPAL_A)
    assert result[0].success
    assert store.read("power").value == 10.0


async def test_claim_acquisition_is_atomic_across_named_targets() -> None:
    leases = VariableLeases()
    first = await leases.claim(
        _claim(owner_id="generation-1", variable_ids=("p1",)),
        frozenset({"p1", "p2"}),
    )
    assert first.accepted
    second = await leases.claim(
        _claim(owner_id="generation-2", variable_ids=("p1", "p2")),
        frozenset({"p1", "p2"}),
    )
    assert not second.accepted
    assert second.error_code == "TARGET_CLAIMED"
    free = await leases.claim(
        _claim(owner_id="generation-2", variable_ids=("p2",)),
        frozenset({"p1", "p2"}),
    )
    assert free.accepted


async def test_renew_release_and_old_token_fencing() -> None:
    leases = VariableLeases()
    acquired = await leases.claim(_claim(variable_ids=("p1",)), frozenset({"p1"}))
    assert acquired.accepted
    renewed = await leases.claim(
        _claim(
            variable_ids=("p1",),
            action="RENEW",
            lease_token=acquired.lease_token,
        ),
        frozenset({"p1"}),
    )
    assert renewed.accepted
    assert renewed.lease_token == acquired.lease_token
    released = await leases.claim(
        _claim(
            variable_ids=("p1",),
            action="RELEASE",
            lease_token=acquired.lease_token,
        ),
        frozenset({"p1"}),
    )
    assert released.accepted
    stale = await leases.claim(
        _claim(
            variable_ids=("p1",),
            action="RELEASE",
            lease_token=acquired.lease_token,
        ),
        frozenset({"p1"}),
    )
    assert not stale.accepted and stale.error_code == "LEASE_LOST"
    with pytest.raises(LeaseError, match="LEASE_LOST"):
        await leases.update(
            "gw1",
            VariableStore({"p1": "int32"}),
            [("p1", 1)],
            acquired.lease_token,
            PRINCIPAL_A,
        )


async def test_claimed_point_missing_from_store_returns_item_level_not_found() -> None:
    """租约有效但点已从存储移除：逐项 NOT_FOUND，不放宽为整批失败。"""
    leases = VariableLeases()
    acquired = await leases.claim(_claim(variable_ids=("p1",)), frozenset({"p1"}))
    assert acquired.accepted
    result = await leases.update(
        "gw1", VariableStore({}), [("p1", 1)], acquired.lease_token, PRINCIPAL_A
    )
    assert result[0].variable_id == "p1"
    assert not result[0].success
    assert result[0].error_code == "NOT_FOUND"


async def test_batch_preflight_blocks_all_values_before_mutation() -> None:
    leases = VariableLeases()
    store = VariableStore({"p1": "int32", "p2": "int32"})
    acquired = await leases.claim(_claim(variable_ids=("p1",)), frozenset({"p1", "p2"}))
    with pytest.raises(LeaseError, match="LEASE_LOST"):
        await leases.update("gw1", store, [("p1", 1), ("p2", 2)], acquired.lease_token, PRINCIPAL_A)
    assert store.read("p1").value is None
    assert store.read("p2").value is None


async def test_expired_claim_can_be_replaced(monkeypatch: pytest.MonkeyPatch) -> None:
    import industrial_gateway.infrastructure.variable_leases as module

    clock = [0.0]
    monkeypatch.setattr(module, "monotonic", lambda: clock[0])
    leases = VariableLeases()
    old = await leases.claim(
        _claim(owner_id="generation-1", variable_ids=("p1",), lease_ms=1000),
        frozenset({"p1"}),
    )
    clock[0] = 1.1
    new = await leases.claim(
        _claim(owner_id="generation-2", variable_ids=("p1",), lease_ms=1000),
        frozenset({"p1"}),
    )
    assert old.accepted and new.accepted
    assert old.lease_token != new.lease_token
    with pytest.raises(LeaseError, match="LEASE_LOST"):
        await leases.update(
            "gw1", VariableStore({"p1": "int32"}), [("p1", 1)], old.lease_token, PRINCIPAL_A
        )


async def test_empty_principal_is_rejected_as_invalid_claim() -> None:
    """R1 P1-03：服务端认证主体为空时 fail-closed，禁止 ACQUIRE。"""
    leases = VariableLeases()
    denied = await leases.claim(
        _claim(principal="   "),
        frozenset({"power"}),
    )
    assert not denied.accepted
    assert denied.error_code == "INVALID_CLAIM"


async def test_renew_and_release_require_the_authenticated_principal() -> None:
    """R1 P1-03：B 持 A 的 owner_id/变量集/token，RENEW/RELEASE 必须拒绝且租约保留。"""
    leases = VariableLeases()
    acquired = await leases.claim(_claim(variable_ids=("p1",)), frozenset({"p1"}))
    assert acquired.accepted

    renew_by_b = await leases.claim(
        _claim(
            principal=PRINCIPAL_B,
            variable_ids=("p1",),
            action="RENEW",
            lease_token=acquired.lease_token,
        ),
        frozenset({"p1"}),
    )
    assert not renew_by_b.accepted
    assert renew_by_b.error_code == "LEASE_LOST"

    release_by_b = await leases.claim(
        _claim(
            principal=PRINCIPAL_B,
            variable_ids=("p1",),
            action="RELEASE",
            lease_token=acquired.lease_token,
        ),
        frozenset({"p1"}),
    )
    assert not release_by_b.accepted
    assert release_by_b.error_code == "LEASE_LOST"

    # A 的租约未被 B 的失败操作破坏：A 自己仍可 RENEW 后 RELEASE。
    renew_by_a = await leases.claim(
        _claim(
            variable_ids=("p1",),
            action="RENEW",
            lease_token=acquired.lease_token,
        ),
        frozenset({"p1"}),
    )
    assert renew_by_a.accepted
    release_by_a = await leases.claim(
        _claim(
            variable_ids=("p1",),
            action="RELEASE",
            lease_token=acquired.lease_token,
        ),
        frozenset({"p1"}),
    )
    assert release_by_a.accepted


async def test_update_with_foreign_principal_is_fenced_and_store_untouched() -> None:
    """R1 P1-03：B 用 A 的 token 调 update 必须 LEASE_LOST，变量值不变。"""
    leases = VariableLeases()
    store = VariableStore({"p1": "int32"})
    acquired = await leases.claim(_claim(variable_ids=("p1",)), frozenset({"p1"}))

    with pytest.raises(LeaseError, match="LEASE_LOST"):
        await leases.update("gw1", store, [("p1", 7)], acquired.lease_token, PRINCIPAL_B)
    assert store.read("p1").value is None

    # 同一认证主体 A 的正常 update 仍然成功，证明拒绝没有破坏原租约。
    result = await leases.update("gw1", store, [("p1", 7)], acquired.lease_token, PRINCIPAL_A)
    assert result[0].success
    assert store.read("p1").value == 7


async def test_same_owner_id_under_different_principal_cannot_share_claim() -> None:
    """owner_id 只是业务标识：同名 owner、不同 principal 仍是两个独立主体。"""
    leases = VariableLeases()
    first = await leases.claim(
        _claim(owner_id="generation-x", principal=PRINCIPAL_A, variable_ids=("p1",)),
        frozenset({"p1"}),
    )
    assert first.accepted
    # B 即便复用相同 owner_id，重叠目标仍被 TARGET_CLAIMED 拒绝，不能抢租。
    second = await leases.claim(
        _claim(owner_id="generation-x", principal=PRINCIPAL_B, variable_ids=("p1",)),
        frozenset({"p1"}),
    )
    assert not second.accepted
    assert second.error_code == "TARGET_CLAIMED"
    # B 拿 A 的 token 做 RENEW 同样被 principal 围栏拒绝。
    hijack = await leases.claim(
        _claim(
            owner_id="generation-x",
            principal=PRINCIPAL_B,
            variable_ids=("p1",),
            action="RENEW",
            lease_token=first.lease_token,
        ),
        frozenset({"p1"}),
    )
    assert not hijack.accepted
    assert hijack.error_code == "LEASE_LOST"
