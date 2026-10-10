"""配置用例通过端口编排完整快照。"""

from pathlib import Path

from core.application.config_use_case import ConfigUseCase
from core.infrastructure.config.adapter import YamlConfigAdapter

ROOT = Path(__file__).resolve().parents[4]


def test_config_use_case_load_and_diff() -> None:
    use_case = ConfigUseCase(YamlConfigAdapter(ROOT / "configs" / "example_modbus"))
    snapshot = use_case.load()
    assert snapshot.site.site_id == "example_modbus"
    assert not use_case.diff(snapshot, snapshot).has_changes

def test_config_use_case_reload_returns_new_snapshot_and_diff() -> None:
    use_case = ConfigUseCase(YamlConfigAdapter(ROOT / "configs" / "example_modbus"))
    previous = use_case.load()
    current, difference = use_case.reload(previous)
    assert current is not previous
    assert not difference.has_changes
    assert current.site == previous.site
