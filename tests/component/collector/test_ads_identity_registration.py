"""ADS target AMS Net ID uniqueness at cold-start and hot-add boundaries."""

from dataclasses import replace

import pytest

from core.application import ConfigError
from collector.application.config import CollectorConfig
from core.domain import Device, DeviceId
from tests.support.new_collector import make_collector_config, make_runtime


def _second_device(config: CollectorConfig, *, target: str) -> Device:
    original = config.devices[DeviceId("dev1")]
    return replace(
        original,
        device_id=DeviceId("dev2"),
        protocol_options={"target_net_id": target},
    )


def test_cold_start_rejects_duplicate_ads_target() -> None:
    config = make_collector_config(protocol="ads")
    second = _second_device(config, target="127.0.0.1.1.1")
    with pytest.raises(ConfigError, match="shared by devices"):
        replace(config, devices={**config.devices, DeviceId("dev2"): second})


def test_cold_start_accepts_distinct_ads_targets() -> None:
    config = make_collector_config(protocol="ads")
    second = _second_device(config, target="127.0.0.2.1.1")
    updated = replace(config, devices={**config.devices, DeviceId("dev2"): second})
    assert len(updated.devices) == 2


async def test_hot_add_rejects_duplicate_without_mutation() -> None:
    config = make_collector_config(protocol="ads", tasks={})
    runtime, _ = make_runtime(config)
    second = _second_device(config, target="127.0.0.1.1.1")

    with pytest.raises(ConfigError, match="already registered"):
        await runtime.add_device(
            "dev2", second, config.point_table_for_device(DeviceId("dev1"))
        )

    assert set(runtime.devices) == {"dev1"}


async def test_hot_add_accepts_distinct_ads_target() -> None:
    config = make_collector_config(protocol="ads", tasks={})
    runtime, _ = make_runtime(config)
    second = _second_device(config, target="127.0.0.2.1.1")
    await runtime.add_device(
        "dev2", second, config.point_table_for_device(DeviceId("dev1"))
    )
    assert set(runtime.devices) == {"dev1", "dev2"}
