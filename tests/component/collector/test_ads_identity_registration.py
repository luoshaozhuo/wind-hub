"""ADS target AMS Net ID uniqueness at cold-start and hot-add boundaries."""

from dataclasses import replace

import pytest
from unittest.mock import AsyncMock

from core.application.errors import ProtocolError

from collector.application.config import CollectorConfig
from core.application import ConfigError
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
        await runtime.add_device("dev2", second, config.point_table_for_device(DeviceId("dev1")))

    assert set(runtime.devices) == {"dev1"}


async def test_hot_add_accepts_distinct_ads_target() -> None:
    config = make_collector_config(protocol="ads", tasks={})
    runtime, _ = make_runtime(config)
    second = _second_device(config, target="127.0.0.2.1.1")
    await runtime.add_device("dev2", second, config.point_table_for_device(DeviceId("dev1")))
    assert set(runtime.devices) == {"dev1", "dev2"}


async def test_remove_frees_target_for_re_registration() -> None:
    # A4：删除设备后，相同目标 Net ID 必须允许重新注册。
    config = make_collector_config(protocol="ads", tasks={})
    runtime, _ = make_runtime(config)
    second = _second_device(config, target="127.0.0.2.1.1")
    table = config.point_table_for_device(DeviceId("dev1"))
    await runtime.add_device("dev2", second, table)
    await runtime.remove_device("dev2")
    await runtime.add_device("dev2", second, table)
    assert set(runtime.devices) == {"dev1", "dev2"}


async def test_rebuild_rejects_target_conflict_with_other_device() -> None:
    # A5：重建设备前检查与*其他*设备的身份冲突；冲突时旧会话保持不变。
    config = make_collector_config(protocol="ads", tasks={})
    runtime, _ = make_runtime(config)
    second = _second_device(config, target="127.0.0.2.1.1")
    table = config.point_table_for_device(DeviceId("dev1"))
    await runtime.add_device("dev2", second, table)

    original_session = runtime.devices["dev1"]
    conflicting = replace(
        config.devices[DeviceId("dev1")],
        protocol_options={"target_net_id": "127.0.0.2.1.1"},
    )
    with pytest.raises(ConfigError, match="already registered"):
        await runtime.rebuild_device("dev1", conflicting, table)
    assert runtime.devices["dev1"] is original_session
    assert set(runtime.devices) == {"dev1", "dev2"}


async def test_rebuild_aborts_when_old_close_unconfirmed() -> None:
    config = make_collector_config(protocol="ads", tasks={})
    runtime, _ = make_runtime(config)
    original = runtime.devices["dev1"]
    original.close = AsyncMock(
        side_effect=ProtocolError("physical close not confirmed")
    )
    replacement = _second_device(config, target="127.0.0.2.1.1")
    # Use the same device ID to request a rebuild with changed ADS target.
    replacement = replace(replacement, device_id=DeviceId("dev1"))

    with pytest.raises(ProtocolError, match="physical close not confirmed"):
        await runtime.rebuild_device(
            "dev1", replacement, config.point_table_for_device(DeviceId("dev1"))
        )

    assert runtime.devices["dev1"] is original


async def test_remove_keeps_device_on_unconfirmed_close() -> None:
    config = make_collector_config(protocol="ads", tasks={})
    runtime, _ = make_runtime(config)
    original = runtime.devices["dev1"]
    original.close = AsyncMock(
        side_effect=ProtocolError("physical close not confirmed")
    )

    with pytest.raises(ProtocolError, match="physical close not confirmed"):
        await runtime.remove_device("dev1")

    assert runtime.devices["dev1"] is original
