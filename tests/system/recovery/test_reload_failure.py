"""验证无效候选配置被拒后不影响运行中设备，随后的有效变更可正常收敛。

v2 没有两段式 Prepare/Activate 事务：不落盘的候选校验走 ValidateConfig，
生效走模板新版本发布 + devices 文档 ReplaceConfig。
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from tests.fixtures.servers.modbus_server import ModbusMockServer
from tests.support.control import (
    get_task,
    replace_config,
    replace_template,
    start_task_instance,
    validate_config,
)
from tests.support.process import CollectorProcess
from tests.support.recovery_helpers import (
    DEVICE_ID,
    recovery_collector_tree,
    wait_telemetry_value,
    write_modbus_redis_config,
)

pytestmark = [pytest.mark.modbus, pytest.mark.redis, pytest.mark.real_service]

# v2：task_id == device_id == instance_id。
TASK_ID = DEVICE_ID
INSTANCE_ID = DEVICE_ID

TEMPLATE_PUBLISH_ENV = {"WIND_HUB_COLLECTOR_TEMPLATE_PUBLISH": "enabled"}


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


class TestReloadFailureResilience:
    async def test_invalid_candidate_rejected_then_valid_change_converges(
        self,
        modbus_server: ModbusMockServer,
        collector_factory,
        sink_service_factory,
        redis_service: str,
        nats_service: str,
        tmp_path: Path,
    ) -> None:
        flat_dir = write_modbus_redis_config(
            tmp_path / "cfg", modbus_server.port, redis_service, nats_url=nats_service
        )
        await sink_service_factory(flat_dir)
        v2_tree = recovery_collector_tree(flat_dir, tmp_path / "v2", nats_url=nats_service)
        proc: CollectorProcess = await collector_factory(
            v2_tree, env_extra=dict(TEMPLATE_PUBLISH_ENV)
        )
        await start_task_instance(proc.grpc_target, task_id=TASK_ID, instance_id=INSTANCE_ID)
        initial = await wait_telemetry_value(redis_service)

        # ---- 无效候选：devices 文档引用不存在的型号版本，校验拒绝、不落盘 ----
        devices_doc = _load(v2_tree / "instances" / "devices.yaml")
        broken_doc = copy.deepcopy(devices_doc)
        broken_doc["devices"][DEVICE_ID]["model"] = {"id": "ghost-model", "version": "9.9.9"}
        checked = await validate_config(
            proc.grpc_target,
            yaml.safe_dump(broken_doc, allow_unicode=True, sort_keys=False),
        )
        assert checked.valid is False
        assert list(checked.errors)

        # 进程存活，旧配置下采集与出数持续。
        assert proc.is_running()
        continued = await wait_telemetry_value(redis_service, after=str(initial["timestamp"]))

        # ---- 有效变更：task 节拍 0.2 → 0.5（新版本模板链 + devices 切换）----
        devices_path = v2_tree / "instances" / "devices.yaml"
        current = _load(devices_path)
        model_id = current["devices"][DEVICE_ID]["model"]["id"]
        model_version = current["devices"][DEVICE_ID]["model"]["version"]
        model_doc = _load(
            v2_tree / "templates" / "device_models" / model_id / f"{model_version}.yaml"
        )
        task_ref = model_doc["task"]
        task_doc = _load(
            v2_tree / "templates" / "tasks" / task_ref["id"] / f"{task_ref['version']}.yaml"
        )
        new_task = copy.deepcopy(task_doc)
        new_task["template_version"] = "1.0.1"
        new_task["interval"] = 0.5
        task_result = await replace_template(
            proc.grpc_target, "tasks", task_ref["id"], "1.0.1", new_task
        )
        assert task_result.success, list(task_result.errors)

        new_model = copy.deepcopy(model_doc)
        new_model["template_version"] = "1.0.1"
        new_model["task"] = {"id": task_ref["id"], "version": "1.0.1"}
        model_result = await replace_template(
            proc.grpc_target, "device_models", model_id, "1.0.1", new_model
        )
        assert model_result.success, list(model_result.errors)

        devices_doc["devices"][DEVICE_ID]["model"]["version"] = "1.0.1"
        activated = await replace_config(
            proc.grpc_target,
            yaml.safe_dump(devices_doc, allow_unicode=True, sort_keys=False),
        )
        assert activated.success, list(activated.errors)

        task = await get_task(proc.grpc_target, DEVICE_ID)
        assert task.interval_ms == 500
        final = await wait_telemetry_value(redis_service, after=str(continued["timestamp"]))
        assert final["quality"] == "good"
