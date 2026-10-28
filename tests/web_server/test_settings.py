"""wind-hub-server ServerSettings 单元测试。"""

from pathlib import Path

import pytest

from web_server.settings import ServerSettings

pytestmark = pytest.mark.unit


def test_settings_defaults() -> None:
    settings = ServerSettings(config_dir=Path("configs"))

    assert settings.config_dir == Path("configs")
    assert settings.host == "127.0.0.1"
    assert settings.port == 8080
    assert settings.collector_endpoints == {"collector": "127.0.0.1:50051"}
    assert settings.commander == "127.0.0.1:50052"


def test_multiple_collectors_require_explicit_worker_ids() -> None:
    settings = ServerSettings(
        config_dir=Path("configs"),
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
            config_dir=Path("configs"),
            collectors=("127.0.0.1:50051",),
        )


def test_duplicate_collector_worker_id_is_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate collector worker_id"):
        ServerSettings(
            config_dir=Path("configs"),
            collectors=(
                "collector-a=127.0.0.1:50051",
                "collector-a=127.0.0.1:50053",
            ),
        )


@pytest.mark.parametrize("port", [0, 65536])
def test_invalid_port_is_rejected(port: int) -> None:
    with pytest.raises(ValueError, match="port"):
        ServerSettings(config_dir=Path("configs"), port=port)


def test_non_positive_shutdown_timeout_is_rejected() -> None:
    with pytest.raises(ValueError, match="shutdown_timeout"):
        ServerSettings(
            config_dir=Path("configs"),
            shutdown_timeout=0,
        )


def test_log_level_must_be_uvicorn_level() -> None:
    with pytest.raises(ValueError, match="log_level"):
        ServerSettings(config_dir=Path("configs"), log_level="verbose")


def test_log_level_is_case_insensitive() -> None:
    settings = ServerSettings(config_dir=Path("configs"), log_level="INFO")
    assert settings.log_level == "INFO"
