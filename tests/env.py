"""真实测试服务的环境配置边界。

测试环境变量统一从两处读取（后者优先）：

1. 进程环境变量；
2. ``tests/test.env``（本地文件，不入库——模板见 ``tests/test.env.example``）。

本模块只负责读取与解析，不启动任何服务；Docker Compose 生命周期由
``tests/fixtures/services/compose.py`` 管理。任何「真实服务未配置」的判定
都返回 ``None``，由调用方决定 ``pytest.skip``——本模块不隐式 skip。
"""

from __future__ import annotations

import os
from pathlib import Path

TEST_ENV_FILE = Path(__file__).resolve().parent / "test.env"

_loaded = False


def load_test_env() -> None:
    """把 ``tests/test.env`` 中的键值并入进程环境（不覆盖已存在的变量）。

    幂等；文件不存在时为空操作。只解析 ``KEY=VALUE`` 行，忽略注释与空行，
    不执行任何 shell 展开。
    """
    global _loaded
    if _loaded:
        return
    _loaded = True
    if not TEST_ENV_FILE.is_file():
        return
    for line in TEST_ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def env_or_none(name: str) -> str | None:
    """读取环境变量；空串视为未配置，返回 ``None``。"""
    load_test_env()
    value = os.environ.get(name, "").strip()
    return value or None


def kafka_bootstrap_from_env() -> str | None:
    """外部已就绪的 Kafka bootstrap servers；未配置返回 ``None``。"""
    return env_or_none("WIND_HUB_TEST_KAFKA")


def postgres_dsn_from_env() -> str | None:
    """外部已就绪的 PostgreSQL DSN；未配置返回 ``None``。"""
    return env_or_none("WIND_HUB_TEST_POSTGRES_DSN")


def ads_config_from_env() -> dict[str, str | int] | None:
    """真实 TwinCAT ADS 环境配置；任一必填项缺失返回 ``None``。

    必填：``WIND_HUB_TEST_ADS_HOST`` / ``WIND_HUB_TEST_ADS_NET_ID``。
    可选：``WIND_HUB_TEST_ADS_PORT``（默认 801）、读写验证 symbol。
    """
    host = env_or_none("WIND_HUB_TEST_ADS_HOST")
    net_id = env_or_none("WIND_HUB_TEST_ADS_NET_ID")
    if not host or not net_id:
        return None
    return {
        "host": host,
        "net_id": net_id,
        "port": int(env_or_none("WIND_HUB_TEST_ADS_PORT") or "801"),
        "read_symbol": env_or_none("WIND_HUB_TEST_ADS_READ_SYMBOL"),
        "write_symbol": env_or_none("WIND_HUB_TEST_ADS_WRITE_SYMBOL"),
    }
