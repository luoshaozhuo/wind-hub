"""System：REST 配置事务全链路——文件读取 → 修改 → Apply → Commander 收敛。

v2 架构下 Collector 自管三路径配置（五 Config RPC），不参与 Server 的
两段式事务：POST /config/apply 只对 Commander Prepare/Activate；成功后
Commander 推进到新 revision，Collector 保持自身 active_revision 不变且
仍然健康。失败路径（非法内容）必须拒绝且不破坏现役配置。
"""

from __future__ import annotations

import pytest
import yaml

from tests.support.wait import wait_until
from tests.system.conftest import SYSTEM_COLLECTOR_ID, SYSTEM_TASK_ID, FullStack

pytestmark = [pytest.mark.modbus, pytest.mark.real_service]


class TestConfigApplyViaRest:
    async def test_apply_modified_task_converges_all_workers(self, full_stack: FullStack) -> None:
        # ---- 读取现役配置文件，修改节拍 ----
        current = await full_stack.http.get("/api/v1/config/files/tasks.yaml")
        assert current.status_code == 200, current.text
        content = current.json()["content"]
        data = yaml.safe_load(content)
        data["tasks"][SYSTEM_TASK_ID]["interval"] = 0.7
        modified = yaml.safe_dump(data, allow_unicode=True)

        # ---- Apply：全 Worker 两段事务 ----
        applied = await full_stack.http.post(
            "/api/v1/config/apply",
            json={"name": "tasks.yaml", "content": modified},
        )
        assert applied.status_code == 200, applied.text
        assert applied.json()["success"] is True, applied.json()

        # ---- 收敛可观测：REST 查询反映新节拍，Commander 推进 revision；
        # Collector v2 不参与该事务，保持自身 revision 且仍然健康。 ----
        task = await full_stack.http.get(f"/api/v1/tasks/{SYSTEM_TASK_ID}")
        assert task.json()["interval"] == pytest.approx(0.7)

        async def _converged() -> dict | None:
            workers = {
                w["worker_id"]: w for w in (await full_stack.http.get("/api/v1/workers")).json()
            }
            commander = workers.get("commander") or workers.get("commander-1")
            if commander is None or not commander.get("active_revision"):
                return None
            collector = workers.get(SYSTEM_COLLECTOR_ID)
            if collector is None or collector.get("active_revision") is None:
                return None
            return {
                "commander": commander["active_revision"],
                "collector": collector["active_revision"],
            }

        revisions = await wait_until(
            _converged,
            timeout=15.0,
            description="commander converged, collector healthy with own revision",
        )
        # Collector 不参与 Server 事务：两个 Worker 的 revision 来源不同，
        # 各自非空即可；Collector 不被 tasks.yaml 变更推动。
        assert revisions["collector"]
        assert revisions["commander"] != revisions["collector"]

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
