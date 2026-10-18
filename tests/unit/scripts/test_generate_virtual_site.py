"""generate_virtual_site 必须产出可被 Core 解析的新契约配置。

回归：生成任务必须引用可用的 Redis Sink，并通过完整配置自检。
"""

from pathlib import Path

import yaml

from core.infrastructure.config.adapter import YamlConfigAdapter
from scripts.generate_virtual_site import generate_site


def test_generated_site_loads_with_task_outputs_contract(tmp_path: Path) -> None:
    output = tmp_path / "virtual"
    result = generate_site(output, count=1)

    assert result == output
    assert output.is_dir()  # 自检失败时整个目录会被 rmtree

    sinks = yaml.safe_load((output / "sinks.yaml").read_text(encoding="utf-8"))
    assert sinks["sinks"]["redis_latest"]["type"] == "redis"
    assert "points" not in sinks["sinks"]["redis_latest"]

    tasks = yaml.safe_load((output / "tasks.yaml").read_text(encoding="utf-8"))
    for family in ("modbus", "ads", "iec104"):
        task = tasks["tasks"][f"virtual-{family}"]
        assert task["outputs"] == [{"sink_id": "redis_latest"}]

    snapshot = YamlConfigAdapter(output).load()
    assert len(snapshot.site.devices) == 3
    assert snapshot.sinks["file_archive"].kind == "redis"
