"""generate_virtual_site 必须产出可被 Core 解析的新契约配置。

回归：生成的 Sink 必须经 sinks.yaml 的 outputs 绑定全部采集 Task，
Task 自身不再声明 outputs，并通过完整配置自检。
"""

from pathlib import Path

import pytest
import yaml

from core.infrastructure.config.adapter import YamlConfigAdapter
from scripts.generate_virtual_site import generate_site

pytestmark = pytest.mark.unit


def test_generated_site_loads_with_sink_outputs_contract(tmp_path: Path) -> None:
    output = tmp_path / "virtual"
    result = generate_site(output, count=1)

    assert result == output
    assert output.is_dir()  # 自检失败时整个目录会被 rmtree

    tasks = yaml.safe_load((output / "tasks.yaml").read_text(encoding="utf-8"))
    task_ids = [f"virtual-{family}" for family in ("modbus", "ads", "iec104")]
    for task_id in task_ids:
        task = tasks["tasks"][task_id]
        assert "outputs" not in task  # Task 不再携带 Sink 去向

    sinks = yaml.safe_load((output / "sinks.yaml").read_text(encoding="utf-8"))
    redis_sink = sinks["sinks"]["redis_latest"]
    assert redis_sink["type"] == "redis"
    # Sink 经 outputs.<task_id> 绑定三个采集 Task（空映射=全量点组）。
    assert set(redis_sink["outputs"]) == set(task_ids)

    system = yaml.safe_load((output / "system.yaml").read_text(encoding="utf-8"))
    assert system["messaging"]["backend"] == "nats"

    snapshot = YamlConfigAdapter(output).load()
    assert len(snapshot.site.devices) == 3
    assert snapshot.sinks["redis_latest"].kind == "redis"
    assert set(snapshot.sinks["redis_latest"].outputs) == set(task_ids)
