"""Commander Service 文件 file_version 契约负面矩阵。

生产在 v1_49 起 *要求* file_version 为严格 MAJOR.MINOR.PATCH 字符串；
本矩阵覆盖缺失、错误类型与非法取值（与 CLI 侧
``tests/cli/test_config_loader.py`` 对齐），正向以版本化 Service 为证。
"""

from __future__ import annotations

import re
from pathlib import Path
from shutil import copytree

import pytest
import yaml

from commander.infrastructure.config.service_settings import load_service_settings

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SERVICE_FILE = _REPO_ROOT / "configs" / "services" / "service_commander_v1.4.0.yaml"


@pytest.fixture
def base_document() -> dict:
    document = yaml.safe_load(_SERVICE_FILE.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def _write(tmp_path: Path, document: dict) -> Path:
    root = tmp_path / "configs"
    source = _REPO_ROOT / "configs"
    copytree(source / "instances", root / "instances")
    copytree(source / "templates", root / "templates")
    version = document.get("file_version")
    valid_version = isinstance(version, str) and bool(
        re.fullmatch(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", version)
    )
    selected_version = version if valid_version else "1.4.0"
    deployment = {
        "schema_version": 1,
        "file_version": "1.0.0",
        "deployment_id": "unit-test",
        "services": {"commander": {"config_id": "commander", "file_version": selected_version}},
        "instances": {"device": {"farm-a": {"file_version": "1.4.0"}}},
    }
    deployment_path = root / "deployments" / "deployment_unit-test_v1.0.0.yaml"
    deployment_path.parent.mkdir(parents=True)
    deployment_path.write_text(
        yaml.safe_dump(deployment, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    target = root / "services" / f"service_commander_v{selected_version}.yaml"
    target.parent.mkdir(parents=True)
    target.write_text(
        yaml.safe_dump(document, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
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
    # Instance 的逻辑身份由 Deployment 决定，禁止绝对路径引用。
    base_document["file_version"] = file_version
    settings = load_service_settings(_write(tmp_path, base_document))
    assert settings.file_version == file_version


def test_shipped_commander_yaml_is_valid() -> None:
    """出厂版本化 Commander Service 必须通过校验。"""
    settings = load_service_settings(_SERVICE_FILE)
    assert settings.file_version == "1.4.0"
