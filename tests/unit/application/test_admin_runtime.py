"""Admin Phase 3-5 新增基础组件单元测试。

仅覆盖纯内存/本机无网络逻辑；真实 PLC、Sink 和子网诊断由后续集成测试覆盖。
"""

from datetime import UTC, datetime

from wind_hub.application.event_log import EventLogStore
from wind_hub.application.usecase.admin_runtime import (
    QualityRecorder,
    SystemHealthUseCase,
)
from wind_hub.domain.model.point import PointValue, Quality


def test_event_log_filters_level_source_and_keyword() -> None:
    logs = EventLogStore()
    logs.append("INFO", "config", "system.yaml", "configuration applied")
    logs.append("ERROR", "device", "wtg-001", "read timeout")

    rows = logs.query(level="ERROR", keyword="timeout")

    assert len(rows) == 1
    assert rows[0].object == "wtg-001"


def test_quality_recorder_keeps_real_point_quality() -> None:
    recorder = QualityRecorder()
    recorder.observe(
        [
            PointValue(
                device_id="d1",
                point_id="p1",
                value=1.0,
                quality=Quality.GOOD,
                timestamp=datetime.now(UTC),
            ),
            PointValue(
                device_id="d1",
                point_id="p2",
                value=None,
                quality=Quality.BAD,
                timestamp=datetime.now(UTC),
            ),
        ]
    )

    rows = recorder.window(3600)

    assert len(rows) == 2
    assert rows[1][3] is Quality.BAD


def test_system_health_returns_real_host_snapshot() -> None:
    health = SystemHealthUseCase()

    result = health.snapshot("1 h")

    assert result["series"]
    assert result["current"]["cpu_count"] >= 1
    assert isinstance(result["mounts"], list)
