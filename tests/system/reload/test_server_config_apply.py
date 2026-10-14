"""System：REST 配置事务全链路——文件读取 → 修改 → Apply → 全 Worker 收敛。

Server 是配置事务唯一编排者：POST /config/apply 触发校验 → 全部 Worker
Prepare/Activate → 成功后 Worker active revision 一致。失败路径（非法
内容）必须拒绝且不破坏现役配置。
"""

from __future__ import annotations

import pytest
import yaml

from tests.support.wait import wait_until
from tests.system.conftest import SYSTEM_TASK_ID, FullStack

pytestmark = [pytest.mark.modbus, pytest.mark.real_service]


class TestConfigApplyViaRest:
    async def test_apply_modified_task_converges_all_workers(
        self, full_stack: FullStack
    ) -> None:
        # ---- 读取现役配置文件，修改节拍 ----
        current = await full_stack.http.get("/api/v1/config/files/tasks.yaml")
        assert current.status_code == 200, current.text
        content = current.json()["content"]
        data = yaml.safe_load(content)
        data["tasks"][0]["interval"] = 0.7
        modified = yaml.safe_dump(data, allow_unicode=True)

        # ---- Apply：全 Worker 两段事务 ----
        applied = await full_stack.http.post(
            "/api/v1/config/apply",
            json={"name": "tasks.yaml", "content": modified},
        )
        assert applied.status_code == 200, applied.text
        assert applied.json()["success"] is True, applied.json()

        # ---- 收敛可观测：REST 查询反映新节拍，Worker active revision 一致 ----
        task = await full_stack.http.get(f"/api/v1/tasks/{SYSTEM_TASK_ID}")
        assert task.json()["interval"] == pytest.approx(0.7)

        async def _converged() -> dict | None:
            workers = (await full_stack.http.get("/api/v1/workers")).json()
            revisions = {w["worker_id"]: w["active_revision"] for w in workers}
            if None in revisions.values():
                return None
            return revisions if len(set(revisions.values())) == 1 else None

        revisions = await wait_until(
            _converged,
            timeout=15.0,
            description="all workers converged to same active revision",
        )
        assert set(revisions) == {"collector-1", "commander"}

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

    async def test_validate_endpoint_checks_without_applying(
        self, full_stack: FullStack
    ) -> None:
        current = await full_stack.http.get("/api/v1/config/files/tasks.yaml")
        data = yaml.safe_load(current.json()["content"])
        data["tasks"][0]["interval"] = 3.3
        candidate = yaml.safe_dump(data, allow_unicode=True)

        review = await full_stack.http.post(
            "/api/v1/config/validate",
            json={"name": "tasks.yaml", "content": candidate},
        )
        assert review.status_code == 200, review.text

        # validate 不得改变现役配置。
        task = await full_stack.http.get(f"/api/v1/tasks/{SYSTEM_TASK_ID}")
        assert task.json()["interval"] == pytest.approx(0.2)
