"""ADS 三项应用层超时：校验、解析与 system.yaml 无损往返。"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from core.application.config_diff import diff_config_snapshots
from core.application.errors import ConfigError
from core.application.settings import ADSLocalConfig
from core.infrastructure.config.adapter import YamlConfigAdapter
from tests.support.new_commander import write_minimal_config_tree


def _load(base: Path):
    return YamlConfigAdapter(base).load()


def test_ads_local_config_timeout_defaults_and_validation() -> None:
    config = ADSLocalConfig(local_ams_net_id="127.0.0.1.1.1", local_ip="127.0.0.1")
    assert (config.request_timeout_ms, config.connect_timeout_ms, config.close_timeout_ms) == (
        1000,
        3000,
        3000,
    )
    for name in ("request_timeout_ms", "connect_timeout_ms", "close_timeout_ms"):
        with pytest.raises(ValueError, match=name):
            ADSLocalConfig(
                local_ams_net_id="127.0.0.1.1.1",
                local_ip="127.0.0.1",
                **{name: 0},
            )
        with pytest.raises(ValueError, match=name):
            ADSLocalConfig(
                local_ams_net_id="127.0.0.1.1.1",
                local_ip="127.0.0.1",
                **{name: 1.5},  # type: ignore[arg-type]
            )


def test_parse_domain_reads_custom_timeouts_and_defaults(tmp_path: Path) -> None:
    base = write_minimal_config_tree(
        tmp_path / "custom",
        protocol="ads",
        devices={"ads-1": {"model_id": "mod", "endpoint": {"host": "127.0.0.1", "port": 851}}},
        point_tables={"tab": {"protocol": "ads", "points": {}}},
        protocol_options={"target_net_id": "127.0.0.1.2.1"},
        ads={
            "local_ams_net_id": "127.0.0.1.1.1",
            "local_ip": "127.0.0.1",
            "request_timeout_ms": 250,
            "connect_timeout_ms": 750,
            "close_timeout_ms": 900,
        },
    )
    ads = _load(base).system.ads
    assert ads is not None
    assert (
        ads.request_timeout_ms,
        ads.connect_timeout_ms,
        ads.close_timeout_ms,
    ) == (250, 750, 900)

    base_default = write_minimal_config_tree(
        tmp_path / "default",
        protocol="ads",
        devices={"ads-1": {"model_id": "mod", "endpoint": {"host": "127.0.0.1", "port": 851}}},
        point_tables={"tab": {"protocol": "ads", "points": {}}},
        protocol_options={"target_net_id": "127.0.0.1.2.1"},
        ads={"local_ams_net_id": "127.0.0.1.1.1", "local_ip": "127.0.0.1"},
    )
    ads = _load(base_default).system.ads
    assert (
        ads.request_timeout_ms,
        ads.connect_timeout_ms,
        ads.close_timeout_ms,
    ) == (1000, 3000, 3000)


@pytest.mark.parametrize("bad_value", [0, -1, "1000", 1.5, True])
def test_parse_domain_rejects_invalid_timeouts(tmp_path: Path, bad_value: object) -> None:
    base = write_minimal_config_tree(
        tmp_path,
        protocol="ads",
        devices={"ads-1": {"model_id": "mod", "endpoint": {"host": "127.0.0.1", "port": 851}}},
        point_tables={"tab": {"protocol": "ads", "points": {}}},
        protocol_options={"target_net_id": "127.0.0.1.2.1"},
        ads={
            "local_ams_net_id": "127.0.0.1.1.1",
            "local_ip": "127.0.0.1",
            "request_timeout_ms": bad_value,
        },
    )
    # 直接构造抛 ValueError；适配器对部分类型包装为 ConfigError，两者都须拒绝。
    with pytest.raises((ValueError, ConfigError)):
        _load(base)


def test_timeout_roundtrip_survives_save(tmp_path: Path) -> None:
    base = write_minimal_config_tree(
        tmp_path,
        protocol="ads",
        devices={"ads-1": {"model_id": "mod", "endpoint": {"host": "127.0.0.1", "port": 851}}},
        point_tables={"tab": {"protocol": "ads", "points": {}}},
        protocol_options={"target_net_id": "127.0.0.1.2.1"},
        ads={
            "local_ams_net_id": "127.0.0.1.1.1",
            "local_ip": "127.0.0.1",
            "request_timeout_ms": 333,
            "connect_timeout_ms": 444,
            "close_timeout_ms": 555,
        },
    )
    adapter = YamlConfigAdapter(base)
    snapshot = adapter.load()
    adapter.save(snapshot)
    reloaded = adapter.load()
    assert not diff_config_snapshots(snapshot, reloaded).has_changes
    raw = yaml.safe_load((base / "system.yaml").read_text())
    assert raw["ads"]["request_timeout_ms"] == 333
    assert raw["ads"]["connect_timeout_ms"] == 444
    assert raw["ads"]["close_timeout_ms"] == 555
