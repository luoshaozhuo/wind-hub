"""Contract：配置集文件布局与指纹语义——Server/Collector/Commander 的共享契约。

三方经 ``config_hash``（``fingerprint_config_set``）校验同一份磁盘配置集：
指纹的确定性、文件集成员规则（仅 config_dir 内 YAML、排除 .history）、
必备文件清单与 schema 严格性（未知字段拒绝）都是跨进程契约，漂移会导致
prepare hash mismatch 或配置静默分叉。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.support.config_helper import write_config_tree
from wind_hub_core.config.fingerprint import fingerprint_config_set
from wind_hub_core.config.loader import load_config
from wind_hub_core.model.errors import ConfigError


def _site(base: Path) -> Path:
    """写出最小合法配置集。"""
    return write_config_tree(
        base,
        devices=[
            {
                "device_id": "modbus-1",
                "protocol": "modbus",
                "point_table": "modbus",
                "endpoint": {"host": "127.0.0.1", "port": 5020},
            }
        ],
        point_tables={
            "modbus": {
                "points": [
                    {
                        "point_id": "rotor.speed",
                        "point_groups": ["telemetry"],
                        "address": {"register_type": "holding", "address": 100},
                        "data_type": "float32",
                    }
                ]
            }
        },
        sinks=[
            {
                "name": "file_sink",
                "type": "file",
                "connection": {"path": "o.jsonl"},
            }
        ],
        tasks=[
            {
                "task_id": "modbus-telemetry",
                "device": "modbus-1",
                "point_group": "telemetry",
                "interval": 0.2,
                "targets": [{"sink": "file_sink"}],
            }
        ],
    )


class TestFingerprintContract:
    def test_same_content_same_hash(self, tmp_path: Path) -> None:
        config_dir = _site(tmp_path / "cfg")

        first = fingerprint_config_set(config_dir)
        second = fingerprint_config_set(config_dir)

        assert first == second
        assert len(first) == 64  # SHA-256 hex

    def test_identical_trees_at_different_locations_differ_by_design(
        self, tmp_path: Path
    ) -> None:
        """指纹含相对路径前缀（根目录名）——同内容不同根名指纹不同。

        该语义保证 Server 与 Worker 必须指向同一布局的配置集，防止
        「内容相同但根不同」被误判为一致。
        """
        dir_a = _site(tmp_path / "a" / "site")
        dir_b = _site(tmp_path / "b" / "site")

        assert fingerprint_config_set(dir_a) == fingerprint_config_set(dir_b)
        dir_c = _site(tmp_path / "b" / "other")
        assert fingerprint_config_set(dir_b) != fingerprint_config_set(dir_c)

    def test_content_change_changes_hash(self, tmp_path: Path) -> None:
        config_dir = _site(tmp_path / "cfg")
        before = fingerprint_config_set(config_dir)

        tasks = config_dir / "tasks.yaml"
        tasks.write_text(tasks.read_text(encoding="utf-8").replace("0.2", "0.5"))

        assert fingerprint_config_set(config_dir) != before

    def test_non_yaml_files_ignored(self, tmp_path: Path) -> None:
        config_dir = _site(tmp_path / "cfg")
        before = fingerprint_config_set(config_dir)

        (config_dir / "notes.txt").write_text("not config", encoding="utf-8")
        (config_dir / "README.md").write_text("docs", encoding="utf-8")

        assert fingerprint_config_set(config_dir) == before

    def test_history_directory_excluded(self, tmp_path: Path) -> None:
        """.history/ 是 Server 配置管理的历史区，不参与现役配置指纹。"""
        config_dir = _site(tmp_path / "cfg")
        before = fingerprint_config_set(config_dir)

        history = config_dir / ".history"
        history.mkdir()
        (history / "tasks.yaml.bak").write_text("old: true\n", encoding="utf-8")

        assert fingerprint_config_set(config_dir) == before

    def test_sibling_yaml_directory_is_ignored(self, tmp_path: Path) -> None:
        """配置集是自包含目录；同级目录不参与当前站点指纹。"""
        site = _site(tmp_path / "deployment" / "site")
        before = fingerprint_config_set(site)

        sibling = tmp_path / "deployment" / "common"
        sibling.mkdir(parents=True)
        (sibling / "shared.yaml").write_text("shared: true\n", encoding="utf-8")

        assert fingerprint_config_set(site) == before


class TestRequiredFilesContract:
    """必备文件清单：缺失任何一个都是 ConfigError 而非隐式默认。"""

    REQUIRED = [
        "system.yaml",
        "sinks.yaml",
        "units.yaml",
        "device_models.yaml",
        "devices.yaml",
        "points.yaml",
        "tasks.yaml",
    ]

    @pytest.mark.parametrize("missing", REQUIRED)
    def test_missing_file_rejected(self, tmp_path: Path, missing: str) -> None:
        config_dir = _site(tmp_path / "cfg")
        assert (config_dir / missing).exists(), f"fixture must write {missing}"
        (config_dir / missing).unlink()

        with pytest.raises(ConfigError):
            load_config(config_dir)

    def test_complete_set_loads(self, tmp_path: Path) -> None:
        config = load_config(_site(tmp_path / "cfg"))

        assert [d.device_id for d in config.devices.devices] == ["modbus-1"]
        assert [t.task_id for t in config.tasks.tasks] == ["modbus-telemetry"]


class TestSchemaStrictnessContract:
    def test_unknown_top_level_key_rejected(self, tmp_path: Path) -> None:
        """未知字段必须拒绝（extra_forbidden）——配置笔误不得静默生效。"""
        config_dir = _site(tmp_path / "cfg")
        tasks = config_dir / "tasks.yaml"
        content = tasks.read_text(encoding="utf-8")
        tasks.write_text(content + "unknown_key: true\n", encoding="utf-8")

        with pytest.raises(ConfigError):
            load_config(config_dir)

    def test_legacy_sink_params_rejected(self, tmp_path: Path) -> None:
        config_dir = _site(tmp_path / "cfg")
        sinks = config_dir / "sinks.yaml"
        content = sinks.read_text(encoding="utf-8")
        sinks.write_text(
            content.replace("connection:", "params:"),
            encoding="utf-8",
        )

        with pytest.raises(ConfigError):
            load_config(config_dir)

    def test_task_referencing_unknown_sink_rejected(self, tmp_path: Path) -> None:
        config_dir = _site(tmp_path / "cfg")
        tasks = config_dir / "tasks.yaml"
        tasks.write_text(
            tasks.read_text(encoding="utf-8").replace("file_sink", "ghost_sink")
        )

        with pytest.raises(ConfigError, match="ghost_sink"):
            load_config(config_dir)

    def test_device_referencing_unknown_point_table_rejected(
        self, tmp_path: Path
    ) -> None:
        config_dir = _site(tmp_path / "cfg")
        models = config_dir / "device_models.yaml"
        models.write_text(
            models.read_text(encoding="utf-8").replace(
                "point_table: modbus", "point_table: ghost"
            )
        )

        with pytest.raises(ConfigError):
            load_config(config_dir)
