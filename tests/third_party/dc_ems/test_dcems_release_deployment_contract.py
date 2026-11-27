"""Verify the DCEMS vendor release stays immutable in wind-hub tests."""

from __future__ import annotations

import hashlib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
RELEASE = REPO_ROOT / "third_party" / "dc-ems"
TESTS = REPO_ROOT / "tests" / "third_party" / "dc_ems"

VENDOR_SHA256 = {
    "./deploy.sh": "578b587404ac94cd8f5ca461ae7c15a78521996a96dbdea2e93ba8226f3d036d",
    "./runtime/containerized/compose.yaml": (
        "a72378e64679eeef3e06029d45236cf0bcab9040dcc293384e3c582c6b7bc70d"
    ),
    "./app/server/dc-ems.jar": ("1df1c053265c7d1127755b4792646a2e5f3503fd5ba6f23fae43c6cbcf9ae7d2"),
}


def test_vendor_release_is_unchanged() -> None:
    """The published manifest and vendor assets must retain original digests."""
    manifest = (RELEASE / "MANIFEST.sha256").read_text(encoding="utf-8")
    entries: dict[str, str] = {}
    for line in manifest.splitlines():
        if not line.strip():
            continue
        checksum, relative = line.split(maxsplit=1)
        assert relative not in entries
        entries[relative] = checksum
    for relative, expected in VENDOR_SHA256.items():
        assert entries[relative] == expected
        assert hashlib.sha256((RELEASE / relative).read_bytes()).hexdigest() == expected


def test_test_only_machine_identity_and_no_jar_override() -> None:
    """Machine identity belongs in the isolated overlay; no Java overrides."""
    overlay = (TESTS / "compose.wtg_control.yaml").read_text(encoding="utf-8")
    runner = (TESTS / "run_wtg_dcems_server.sh").read_text(encoding="utf-8")
    vendor_compose = (RELEASE / "runtime/containerized/compose.yaml").read_text(encoding="utf-8")
    assert "target: /etc/machine-id" in overlay
    assert "source: ${DEPLOY_CONFIG_DIR:" in overlay
    assert "target: /etc/machine-id" not in vendor_compose
    assert "DCEMS_WTG_PATCHED_JAR is unsupported" in runner
    assert not (TESTS / "compose.wtg_patched_jar.yaml").exists()
    assert not any((TESTS / "patches").glob("*.patch"))
