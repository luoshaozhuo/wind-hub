"""wind-hub-server ServerSettings 单元测试。"""

from pathlib import Path

import pytest

from wind_hub_server.settings import ServerSettings


def test_settings_defaults() -> None:
    settings = ServerSettings(config_dir=Path("configs/template"))

    assert settings.config_dir == Path("configs/template")
    assert settings.host == "127.0.0.1"
    assert settings.port == 8080
    assert settings.collector_endpoints == {"collector": "127.0.0.1:50051"}
    assert settings.commander == "127.0.0.1:50052"


def test_multiple_collectors_require_explicit_worker_ids() -> None:
    settings = ServerSettings(
        config_dir=Path("configs/template"),
        collectors=(
            "collector-a=127.0.0.1:50051",
            "collector-b=127.0.0.1:50053",
        ),
    )

    assert settings.collector_endpoints == {
        "collector-a": "127.0.0.1:50051",
        "collector-b": "127.0.0.1:50053",
    }


def test_collector_without_worker_id_is_rejected() -> None:
    with pytest.raises(ValueError, match="worker_id=host:port"):
        ServerSettings(
            config_dir=Path("configs/template"),
            collectors=("127.0.0.1:50051",),
        )


def test_duplicate_collector_worker_id_is_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate collector worker_id"):
        ServerSettings(
            config_dir=Path("configs/template"),
            collectors=(
                "collector-a=127.0.0.1:50051",
                "collector-a=127.0.0.1:50053",
            ),
        )


@pytest.mark.parametrize("port", [0, 65536])
def test_invalid_port_is_rejected(port: int) -> None:
    with pytest.raises(ValueError, match="port"):
        ServerSettings(config_dir=Path("configs/template"), port=port)


def test_non_positive_shutdown_timeout_is_rejected() -> None:
    with pytest.raises(ValueError, match="shutdown_timeout"):
        ServerSettings(
            config_dir=Path("configs/template"),
            shutdown_timeout=0,
        )
