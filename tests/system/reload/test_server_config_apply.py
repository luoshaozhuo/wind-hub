"""System：REST 配置事务全链路——文件读取 → 修改 → Apply → Commander 收敛。

v2 架构下 Collector 自管三路径配置（五 Config RPC），不参与 Server 的
两段式事务：POST /config/apply 只对 Commander Prepare/Activate；成功后
Commander 推进到新 revision，Collector 保持自身 active_revision 不变且
仍然健康。失败路径（非法内容、非 Commander 所属文件）必须拒绝且不破坏
现役配置。

拓扑边界：本现场 Web Server 管理 v1 扁平配置（``cfg/tasks.yaml`` 等），
Commander 的 config_sources 只含 v2 三路径（``instances/``、``templates/``），
两套键名空间不重叠——扁平文件经真实 mTLS 快照 RPC 比对后必然在 Push 前
被拒，生产侧 fail-closed 并回滚写盘。真实 Commander Prepare/Activate 成功
路径由 tests/system/rpc/test_server_commander_rpc.py 的快照事务 round trip
覆盖；本文件验收 REST 侧的拒绝/回滚/无变异契约。
"""

from __future__ import annotations

import pytest
import yaml

from tests.system.conftest import SYSTEM_COLLECTOR_ID, SYSTEM_TASK_ID, FullStack

pytestmark = [pytest.mark.modbus, pytest.mark.real_service]


class TestConfigApplyViaRest:
    async def test_apply_legacy_file_outside_commander_sources_fails_closed_with_rollback(
        self, full_stack: FullStack
    ) -> None:
        # ---- 两个 Worker 的现役 revision 基线（真实 mTLS gRPC 监控面） ----
        workers_before = {
            row["worker_id"]: row.get("active_revision")
            for row in (await full_stack.http.get("/api/v1/workers")).json()
        }
        assert workers_before.get(SYSTEM_COLLECTOR_ID) is not None

        # ---- 读取现役配置文件，修改节拍并 Apply ----
        current = await full_stack.http.get("/api/v1/config/files/tasks.yaml")
        assert current.status_code == 200, current.text
        data = yaml.safe_load(current.json()["content"])
        data["tasks"][SYSTEM_TASK_ID]["interval"] = 0.7
        modified = yaml.safe_dump(data, allow_unicode=True)

        applied = await full_stack.http.post(
            "/api/v1/config/apply",
            json={"name": "tasks.yaml", "content": modified},
        )
        # 路由契约：写盘后事务失败且已回滚 → 200 + success=false（非 422）。
        assert applied.status_code == 200, applied.text
        body = applied.json()
        assert body["success"] is False
        assert body["rollback_performed"] is True
        assert any("config_sources" in message for message in body["errors"]), body["errors"]

        # ---- 现役快照零变异：REST 读模型与磁盘文件都保持旧节拍 ----
        task = await full_stack.http.get(f"/api/v1/tasks/{SYSTEM_TASK_ID}")
        assert task.json()["interval"] == pytest.approx(0.2)
        on_disk = await full_stack.http.get("/api/v1/config/files/tasks.yaml")
        restored = yaml.safe_load(on_disk.json()["content"])
        assert restored["tasks"][SYSTEM_TASK_ID]["interval"] == pytest.approx(0.2)

        # ---- 两个 Worker 仍然健康，active_revision 均未被推动 ----
        workers_after = {
            row["worker_id"]: row.get("active_revision")
            for row in (await full_stack.http.get("/api/v1/workers")).json()
        }
        assert workers_after == workers_before

    async def test_apply_invalid_content_rejected_and_system_unaffected(
        self, full_stack: FullStack
    ) -> None:
        broken = yaml.safe_dump(
            {
                "tasks": [
                    {
                        "task_id": SYSTEM_TASK_ID,
                        "device": "modbus-1",
                        "point_group": "telemetry",
                        "interval": 0.2,
                        "targets": [{"sink": "ghost_sink"}],
                    }
                ]
            }
        )
        applied = await full_stack.http.post(
            "/api/v1/config/apply",
            json={"name": "tasks.yaml", "content": broken},
        )
        # 非法配置：非 2xx 或 success=False；现役配置不得被破坏。
        if applied.status_code == 200:
            assert applied.json()["success"] is False
        else:
            assert applied.status_code >= 400

        task = await full_stack.http.get(f"/api/v1/tasks/{SYSTEM_TASK_ID}")
        assert task.status_code == 200
        assert task.json()["interval"] == pytest.approx(0.2)

    async def test_validate_endpoint_checks_without_applying(self, full_stack: FullStack) -> None:
        current = await full_stack.http.get("/api/v1/config/files/tasks.yaml")
        data = yaml.safe_load(current.json()["content"])
        data["tasks"][SYSTEM_TASK_ID]["interval"] = 3.3
        candidate = yaml.safe_dump(data, allow_unicode=True)

        review = await full_stack.http.post(
            "/api/v1/config/validate",
            json={"name": "tasks.yaml", "content": candidate},
        )
        assert review.status_code == 200, review.text

        # validate 不得改变现役配置。
        task = await full_stack.http.get(f"/api/v1/tasks/{SYSTEM_TASK_ID}")
        assert task.json()["interval"] == pytest.approx(0.2)
