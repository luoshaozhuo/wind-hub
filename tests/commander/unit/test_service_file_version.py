"""Commander Service 文件 file_version 契约负面矩阵。

生产在 v1_49 起 *要求* file_version 为严格 MAJOR.MINOR.PATCH 字符串；
本矩阵覆盖缺失、错误类型与非法取值（与 CLI 侧
``tests/cli/test_config_loader.py`` 对齐），正向以出厂 commander.yaml 为证。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from commander.infrastructure.config.service_settings import load_service_settings

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SERVICE_FILE = _REPO_ROOT / "configs" / "services" / "commander.yaml"


@pytest.fixture
def base_document() -> dict:
    document = yaml.safe_load(_SERVICE_FILE.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def _write(tmp_path: Path, document: dict) -> Path:
    target = tmp_path / "commander.yaml"
    target.write_text(
        yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    return target


@pytest.mark.parametrize(
    "file_version",
    [
        None,  # 缺失（_keys 拒绝缺字段）
        1,  # 错误类型：整数
        1.0,  # 错误类型：浮点
        ["1", "0", "0"],  # 错误类型：序列
        "1.0",  # 两段
        "1.0.0.0",  # 四段
        "01.0.0",  # 前导零
        "1.0.0-rc1",  # 预发布后缀
        "v1.0.0",  # 前缀
        " 1.0.0",  # 前导空白
        "1.0.0 ",  # 尾随空白
        "",  # 空字符串
    ],
)
def test_file_version_is_required_strict_semver(
    tmp_path: Path, base_document: dict, file_version: object
) -> None:
    if file_version is None:
        base_document.pop("file_version")
    else:
        base_document["file_version"] = file_version
    with pytest.raises(ValueError, match="file_version|Commander service"):
        load_service_settings(_write(tmp_path, base_document))


@pytest.mark.parametrize("file_version", ["1.0.0", "0.0.1", "12.34.56"])
def test_valid_file_version_is_accepted(
    tmp_path: Path, base_document: dict, file_version: str
) -> None:
    # 配置源路径按服务文件位置解析；临时副本改指仓库内绝对路径。
    root = _REPO_ROOT / "configs"
    base_document["config_sources"] = {
        "instances": {"devices": str(root / "instances" / "devices.yaml")},
        "templates": {"root": str(root / "templates")},
    }
    base_document["file_version"] = file_version
    settings = load_service_settings(_write(tmp_path, base_document))
    assert settings.file_version == file_version


def test_shipped_commander_yaml_is_valid() -> None:
    """出厂 commander.yaml 必须通过校验（file_version: 1.3.0）。"""
    settings = load_service_settings(_SERVICE_FILE)
    assert settings.file_version == "1.3.0"
