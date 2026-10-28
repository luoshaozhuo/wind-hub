"""TDengine Sink 密码只写生命周期（第二轮 P2-TDENGINE-PW-001 回归）。

与 Redis 的差异前提：``TDengineSinkConnection.password`` 为必填字段。
因此必须验证**真实 Apply**（落盘 + 正式 Schema 重新加载 + reload 回滚），
不能只看 SinkService 的返回值：

1. 新建带密码 → 落盘并通过完整配置加载；
2. 编辑其他字段、不回传密码 → 旧密码保留、Apply 成功（第二轮阻断点）；
3. 编辑提交新密码 → 替换；
4. 整体配置保存（AdminStateService mutator）+ 真实 Apply → 旧密码保留；
5. redis↔tdengine 变更类型 → 不继承另一类型的密码（必填侧拒绝）；
6. 新建缺少必填密码 → Apply 被拒并回滚；
7. API 快照与脱敏文本不得泄露明文密码；校验报错也不得回显密码。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from core.infrastructure.config.adapter import YamlConfigAdapter
from tests.support.new_collector import write_collector_config_tree
from tests.web_server.test_config import COLLECTOR_ID, _FakeDirectory, _FakeWorker
from web_server.application.config.admin_state import (
    AdminDefinitionsState,
    AdminSinkItem,
    AdminStateService,
)
from web_server.application.config.files import SECRET_PLACEHOLDER, ConfigFileService
from web_server.application.config.service import ConfigService
from web_server.application.sink.service import SinkService
from web_server.application.worker.model import COMMANDER_WORKER_ID

pytestmark = pytest.mark.unit

_TD_BASE_CONNECTION = {
    "host": "127.0.0.1",
    "port": 6041,
    "database": "wind_hub",
    "stable": "point_values",
    "username": "root",
    "timeout": 5.0,
    "tls": False,
}
_TD_PASSWORD = "td-secret-value"


def _service(tmp_path: Path, *, sinks: dict | None = None, tasks: dict | None = None):
    base = write_collector_config_tree(tmp_path / "cfg", sinks=sinks, tasks=tasks)
    collector = _FakeWorker(COLLECTOR_ID)
    commander = _FakeWorker(COMMANDER_WORKER_ID)
    directory = _FakeDirectory({COLLECTOR_ID: collector})
    config = ConfigService(
        base,
        directory,
        commander,
        ConfigService.load_directory(base),
    )
    admin = ConfigFileService(config)
    service = SinkService(
        collectors=directory,
        monitoring=SimpleNamespace(sinks_snapshot=lambda: []),
        placements=MagicMock(),
        config=config,
        admin=admin,
    )
    return service, admin, base


def _td_connection(**overrides: object) -> dict[str, object]:
    return {**_TD_BASE_CONNECTION, **overrides}


def _load_sinks_yaml(base: Path) -> dict:
    return yaml.safe_load((base / "sinks.yaml").read_text(encoding="utf-8"))


async def test_create_tdengine_with_password_applies_and_persists(tmp_path: Path) -> None:
    service, _admin, base = _service(tmp_path)

    result = await service.upsert(
        "td1",
        {
            "type": "tdengine",
            "enabled": True,
            "connection": _td_connection(password=_TD_PASSWORD),
        },
    )

    assert result.success, result.errors
    assert _load_sinks_yaml(base)["sinks"]["td1"]["connection"]["password"] == _TD_PASSWORD
    # 正式配置加载必须接受，且密码以明文标量进入解析后的连接配置。
    snapshot = YamlConfigAdapter(base).load()
    assert snapshot.sinks["td1"].kind == "tdengine"
    assert snapshot.sinks["td1"].connection["password"] == _TD_PASSWORD


async def test_edit_tdengine_without_password_key_preserves_and_applies(tmp_path: Path) -> None:
    service, _admin, base = _service(tmp_path)
    await service.upsert(
        "td1",
        {"type": "tdengine", "enabled": True, "connection": _td_connection(password=_TD_PASSWORD)},
    )

    # UI 编辑态密码框留空：payload 直接省略 password 键，仅改 host。
    result = await service.upsert(
        "td1",
        {
            "type": "tdengine",
            "enabled": True,
            "connection": _td_connection(host="10.0.0.8"),
        },
    )

    assert result.success, result.errors
    connection = _load_sinks_yaml(base)["sinks"]["td1"]["connection"]
    assert connection["host"] == "10.0.0.8"
    assert connection["password"] == _TD_PASSWORD
    snapshot = YamlConfigAdapter(base).load()
    assert snapshot.sinks["td1"].connection["password"] == _TD_PASSWORD


async def test_edit_tdengine_with_new_password_replaces(tmp_path: Path) -> None:
    service, _admin, base = _service(tmp_path)
    await service.upsert(
        "td1",
        {"type": "tdengine", "enabled": True, "connection": _td_connection(password=_TD_PASSWORD)},
    )

    result = await service.upsert(
        "td1",
        {
            "type": "tdengine",
            "enabled": True,
            "connection": _td_connection(password="rotated-pw"),
        },
    )

    assert result.success, result.errors
    assert _load_sinks_yaml(base)["sinks"]["td1"]["connection"]["password"] == "rotated-pw"


async def test_edit_tdengine_with_empty_password_is_rejected(tmp_path: Path) -> None:
    """TDengine 密码必填：显式清空（""）不能像 Redis 一样通过 Apply。"""
    service, _admin, base = _service(tmp_path)
    await service.upsert(
        "td1",
        {"type": "tdengine", "enabled": True, "connection": _td_connection(password=_TD_PASSWORD)},
    )

    result = await service.upsert(
        "td1",
        {
            "type": "tdengine",
            "enabled": True,
            "connection": _td_connection(host="10.0.0.8", password=""),
        },
    )

    assert result.success is False
    assert any("password" in error for error in result.errors)
    # 回滚：磁盘仍是旧密码与旧 host。
    connection = _load_sinks_yaml(base)["sinks"]["td1"]["connection"]
    assert connection["password"] == _TD_PASSWORD
    assert connection["host"] == "127.0.0.1"


async def test_create_tdengine_without_password_is_rejected_and_rolled_back(tmp_path: Path) -> None:
    service, _admin, base = _service(tmp_path)
    before = (base / "sinks.yaml").read_text(encoding="utf-8")

    result = await service.upsert(
        "td_new",
        {"type": "tdengine", "enabled": True, "connection": _td_connection()},
    )

    assert result.success is False
    assert any("password" in error for error in result.errors)
    # 失败必须回滚磁盘并保持旧配置可加载。
    assert (base / "sinks.yaml").read_text(encoding="utf-8") == before
    assert "td_new" not in YamlConfigAdapter(base).load().sinks


async def test_switch_redis_to_tdengine_does_not_inherit_password(tmp_path: Path) -> None:
    service, _admin, base = _service(tmp_path)
    # 已有 Redis Sink（带密码，无 ACL username）。
    assert (
        await service.upsert(
            "mix",
            {
                "type": "redis",
                "enabled": True,
                "connection": {"host": "127.0.0.1", "port": 6379, "password": "redis-pw"},
            },
        )
    ).success

    # 改为 tdengine 且不回传密码：不得继承 redis-pw；tdengine 必填校验必须拒绝。
    result = await service.upsert(
        "mix",
        {"type": "tdengine", "enabled": True, "connection": _td_connection()},
    )
    assert result.success is False
    on_disk = _load_sinks_yaml(base)["sinks"]["mix"]
    assert on_disk["type"] == "redis"
    assert on_disk["connection"].get("password") == "redis-pw"

    # 显式提供新密码后允许切换，且落盘的是新密码。
    result = await service.upsert(
        "mix",
        {
            "type": "tdengine",
            "enabled": True,
            "connection": _td_connection(password=_TD_PASSWORD),
        },
    )
    assert result.success, result.errors
    switched = _load_sinks_yaml(base)["sinks"]["mix"]
    assert switched["type"] == "tdengine"
    assert switched["connection"]["password"] == _TD_PASSWORD


async def test_switch_tdengine_to_redis_does_not_inherit_password(tmp_path: Path) -> None:
    service, _admin, base = _service(tmp_path)
    assert (
        await service.upsert(
            "mix",
            {
                "type": "tdengine",
                "enabled": True,
                "connection": _td_connection(password=_TD_PASSWORD),
            },
        )
    ).success

    result = await service.upsert(
        "mix",
        {
            "type": "redis",
            "enabled": True,
            # Redis 密码可空；payload 不含 password 键。
            "connection": {"host": "127.0.0.1", "port": 6379},
        },
    )

    assert result.success, result.errors
    connection = _load_sinks_yaml(base)["sinks"]["mix"]["connection"]
    assert "password" not in connection or not connection.get("password")


async def test_admin_state_bulk_edit_preserves_tdengine_password_through_apply(
    tmp_path: Path,
) -> None:
    """整体配置保存接口（AdminStateService.replace_all）+ 真实 Apply 链路。"""
    sinks = {
        "td1": {
            "type": "tdengine",
            "enabled": True,
            "connection": _td_connection(password=_TD_PASSWORD),
        }
    }
    tasks = {
        "t1": {
            "device_group_id": "dev1",
            "point_group": "g",
            "interval": 1.0,
            "outputs": [
                {
                    "sink_id": "td1",
                    "points": [
                        {
                            "source": {"device_id": "dev1", "point_id": "p1"},
                            "address": {"field": "value"},
                        }
                    ],
                }
            ],
        }
    }
    _service_sink, admin, base = _service(tmp_path, sinks=sinks, tasks=tasks)

    admin_api = AsyncMock()
    state_service = AdminStateService(admin_api)
    await state_service.replace_all(
        devices=[],
        tasks=[],
        sinks=[
            AdminSinkItem(
                name="td1",
                type="tdengine",
                enabled=True,
                # 前端结构化状态不回显密码。
                connection=_td_connection(host="10.0.0.9"),
            )
        ],
        definitions=AdminDefinitionsState(
            units={}, device_types={}, device_models={}, point_tables={}
        ),
    )
    mutator = admin_api.mutate_yaml_files.call_args.args[1]
    documents = {
        name: yaml.safe_load((base / name).read_text(encoding="utf-8"))
        for name in (
            "devices.yaml",
            "tasks.yaml",
            "sinks.yaml",
            "units.yaml",
            "device_models.yaml",
            "points.yaml",
        )
    }
    mutator(documents)
    candidate = yaml.safe_dump(documents["sinks.yaml"], allow_unicode=True, sort_keys=False)

    # 经过正式单文件 Apply（恢复占位/校验/落盘/reload/回滚全套）。
    result = await admin.apply_file("sinks.yaml", candidate, source="test", comment="bulk edit")
    assert result.success, result.errors
    connection = _load_sinks_yaml(base)["sinks"]["td1"]["connection"]
    assert connection["host"] == "10.0.0.9"
    assert connection["password"] == _TD_PASSWORD
    assert YamlConfigAdapter(base).load().sinks["td1"].connection["password"] == _TD_PASSWORD


async def test_snapshot_and_redacted_text_do_not_leak_password(tmp_path: Path) -> None:
    service, admin, _base = _service(tmp_path)
    await service.upsert(
        "td1",
        {"type": "tdengine", "enabled": True, "connection": _td_connection(password=_TD_PASSWORD)},
    )

    row = service.get_sink("td1")
    assert row.has_password is True
    assert "password" not in row.connection
    assert _TD_PASSWORD not in row.model_dump_json()

    redacted = admin.read_file_redacted("sinks.yaml")
    assert _TD_PASSWORD not in redacted
    assert SECRET_PLACEHOLDER in redacted


async def test_validation_errors_do_not_echo_password(tmp_path: Path) -> None:
    """携密 Apply 因其他字段失败时，错误文本不得带出明文密码。"""
    service, _admin, _base = _service(tmp_path)

    result = await service.upsert(
        "td_bad",
        {
            "type": "tdengine",
            "enabled": True,
            "connection": _td_connection(database="bad name!", password=_TD_PASSWORD),
        },
    )

    assert result.success is False
    assert result.errors
    assert _TD_PASSWORD not in " ".join(result.errors)
