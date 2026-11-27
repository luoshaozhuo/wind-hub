"""Ensure local DCEMS deployment corrections retain a valid release manifest.

These checks cover deployment assets only. The released Java JAR is unchanged;
a Java source patch alone cannot establish FactoryPoints runtime correctness.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
RELEASE = REPO_ROOT / "third_party" / "dc-ems"


def test_license_machine_identity_uses_server_contract() -> None:
    """Machine-code generation and Server must share a persisted identity."""
    script = (RELEASE / "deploy.sh").read_text(encoding="utf-8")
    compose = (RELEASE / "runtime/containerized/compose.yaml").read_text(encoding="utf-8")
    assert "machine-code DC-EMS" in script
    assert "machine-code DCEMS" not in script
    assert "ensure_machine_id" in script
    assert "ensure_machine_id\n\n    # 已部署" in script
    assert "source=${ROOT}/config/machine-id,target=/etc/machine-id,readonly" in script
    assert "source: ${DEPLOY_CONFIG_DIR}/machine-id" in compose
    assert "target: /etc/machine-id" in compose
    assert "read_only: true" in compose


def test_modified_deployment_assets_have_matching_manifest() -> None:
    """Local modifications must not silently invalidate package checksums."""
    manifest = (RELEASE / "MANIFEST.sha256").read_text(encoding="utf-8")
    entries: dict[str, str] = {}
    for line in manifest.splitlines():
        if not line.strip():
            continue
        checksum, relative = line.split(maxsplit=1)
        assert relative not in entries
        entries[relative] = checksum
    for relative in ("./deploy.sh", "./runtime/containerized/compose.yaml"):
        content = (RELEASE / relative).read_bytes()
        assert hashlib.sha256(content).hexdigest() == entries[relative]


def test_factorypoints_patch_covers_dispatch_and_statistics_nulls() -> None:
    """The source patch must guard both reported FactoryPoints failure paths."""
    patch = (
        REPO_ROOT / "tests/third_party/dc_ems/patches/factorypoints-null-dispatch-flags.patch"
    ).read_text(encoding="utf-8")
    for header in (
        "@@ -911,11 +911,11 @@",
        "@@ -1014,11 +1014,11 @@",
        "@@ -1089,21 +1089,32 @@",
        "@@ -1128,7 +1139,8 @@",
        "@@ -1148,7 +1160,8 @@",
        "@@ -1332,14 +1345,14 @@",
        "@@ -1347,9 +1360,9 @@",
        "@@ -1386,17 +1399,18 @@",
        "@@ -1415,7 +1429,7 @@",
    ):
        assert header in patch
    assert sum(1 for line in patch.splitlines() if line.startswith("@@")) == 9
    assert "t.emsWorkMode.getVal() != null" in patch
    assert "Boolean.TRUE.equals(emsFacDispatchActEnabled.getVal())" in patch
    assert "actSend == null || actLoopRef == null" in patch
    assert "limitCoe == null || contractCapacity == null" in patch
    assert "double turbinePower = ifNull(wt.getEmsPowAct().getVal(), 0d)" in patch
    assert "Boolean.TRUE.equals(wt.emsRunState.getVal())" in patch
    # update-callback config null guards (detected by independent harness)
    assert "ctrlPowerPoint != null && ctrlPowerPoint == 1" in patch
    assert "ctrlReaPowerPoint != null && ctrlReaPowerPoint == 1" in patch
    assert (
        "Boolean.TRUE.equals(GlobalData.FactoryConfig.emsReactAvoidCurrentOverFlowEnable.getVal())"
        in patch
    )
