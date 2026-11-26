"""验证 WebSocket 本机地址修补仅作用于隔离静态资源。"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.third_party.dc_ems.prepare_web import BUNDLE_NAME, LEGACY, LOCAL, stage_web


def test_websocket_address_is_origin_based_without_mutating_release(tmp_path: Path) -> None:
    release = tmp_path / "release"
    bundle = release / "static" / "js" / BUNDLE_NAME
    bundle.parent.mkdir(parents=True)
    content = f"function wire(){{return {{{LEGACY}}};}}"
    bundle.write_text(content, encoding="utf-8")
    (release / "index.html").write_text("<html></html>", encoding="utf-8")
    target = tmp_path / "runtime" / "web"
    stage_web(release, target)
    assert bundle.read_text(encoding="utf-8") == content
    rewritten = (target / "static" / "js" / BUNDLE_NAME).read_text(encoding="utf-8")
    assert rewritten.count(LOCAL) == 1
    assert "10.0.160.86:443" not in rewritten
    with pytest.raises(FileExistsError):
        stage_web(release, target)


def test_unknown_bundle_is_not_modified(tmp_path: Path) -> None:
    source = tmp_path / "source"
    bundle = source / "static" / "js" / BUNDLE_NAME
    bundle.parent.mkdir(parents=True)
    bundle.write_text("unknown release version", encoding="utf-8")
    destination = tmp_path / "output"
    with pytest.raises(ValueError, match="Web build differs"):
        stage_web(source, destination)
    assert not destination.exists()
