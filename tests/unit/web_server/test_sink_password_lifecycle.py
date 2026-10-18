"""Sink upsert 的 Redis 密码只写/清空生命周期。

契约：
- payload 不含 password 键 → 沿用同名 Redis sink 的旧密码；
- password 为 ""/null → 显式清空（从 YAML 移除该键）；
- 非空字符串 → 更新为新值。
"""

from __future__ import annotations

from typing import Any

from web_server.application.config.files import ConfigApplyResult
from web_server.application.sink.service import SinkService


class _FakeAdmin:
    """在内存文档上执行 mutate_yaml_files，捕获最终 YAML 结构。"""

    def __init__(self, documents: dict[str, dict[str, Any]]) -> None:
        self._documents = documents

    async def mutate_yaml_files(
        self,
        names: tuple[str, ...],
        mutator: Any,
        *,
        source: str,
        comment: str,
    ) -> ConfigApplyResult:
        del names, source, comment
        mutator(self._documents)
        return ConfigApplyResult(success=True)


def _service(previous_password: str | None) -> tuple[SinkService, dict[str, Any]]:
    connection: dict[str, Any] = {"host": "127.0.0.1", "port": 6379}
    if previous_password is not None:
        connection["password"] = previous_password
    documents = {
        "sinks.yaml": {
            "sinks": {
                "redis_main": {
                    "type": "redis",
                    "enabled": True,
                    "connection": connection,
                }
            }
        }
    }
    return SinkService(None, None, None, None, _FakeAdmin(documents)), documents


async def test_upsert_without_password_key_preserves_existing() -> None:
    service, documents = _service("old-pw")

    result = await service.upsert(
        "redis_main",
        {
            "type": "redis",
            "enabled": True,
            "connection": {"host": "10.0.0.1", "port": 6380},
        },
    )

    assert result.success is True
    connection = documents["sinks.yaml"]["sinks"]["redis_main"]["connection"]
    assert connection["password"] == "old-pw"
    assert connection["host"] == "10.0.0.1"


async def test_upsert_empty_string_clears_password() -> None:
    service, documents = _service("old-pw")

    result = await service.upsert(
        "redis_main",
        {
            "type": "redis",
            "enabled": True,
            "connection": {"host": "127.0.0.1", "password": ""},
        },
    )

    assert result.success is True
    connection = documents["sinks.yaml"]["sinks"]["redis_main"]["connection"]
    assert "password" not in connection


async def test_upsert_null_clears_password() -> None:
    service, documents = _service("old-pw")

    result = await service.upsert(
        "redis_main",
        {
            "type": "redis",
            "enabled": True,
            "connection": {"host": "127.0.0.1", "password": None},
        },
    )

    assert result.success is True
    connection = documents["sinks.yaml"]["sinks"]["redis_main"]["connection"]
    assert "password" not in connection


async def test_upsert_new_password_overwrites() -> None:
    service, documents = _service("old-pw")

    result = await service.upsert(
        "redis_main",
        {
            "type": "redis",
            "enabled": True,
            "connection": {"host": "127.0.0.1", "password": "new-pw"},
        },
    )

    assert result.success is True
    connection = documents["sinks.yaml"]["sinks"]["redis_main"]["connection"]
    assert connection["password"] == "new-pw"


async def test_upsert_without_previous_password_writes_none() -> None:
    service, documents = _service(None)

    result = await service.upsert(
        "redis_main",
        {
            "type": "redis",
            "enabled": True,
            "connection": {"host": "127.0.0.1"},
        },
    )

    assert result.success is True
    connection = documents["sinks.yaml"]["sinks"]["redis_main"]["connection"]
    assert "password" not in connection
