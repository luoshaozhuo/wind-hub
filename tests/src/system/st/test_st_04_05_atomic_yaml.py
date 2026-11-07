"""ST-04 / ST-05 原子 YAML 替换故障注入（设计 §14.3 ST-04、ST-05）。

ST-04：原子替换（os.replace）发生前故障——原目标必须完整保留，
       临时文件被清理，错误显式标注 replaced=False（不虚报已替换）。
ST-05：替换成功后父目录 fsync 故障——结果必须暴露持久化不确定性
       （replaced=True），不伪称回滚；目标内容已是新数据。

两类故障均通过 monkeypatch 注入真实 OSError，不改动生产代码。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from core.infrastructure.config.generic_yaml import (
    ConfigErrorCode,
    ConfigOperationError,
    read_yaml,
    save_yaml,
)

pytestmark = [pytest.mark.system, pytest.mark.fault_injection, pytest.mark.safety]


def test_st_04_pre_replace_failure_keeps_original_and_cleans_temp(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = tmp_path / "site.yaml"
    original = {"site_id": "origin", "keep": True}
    save_yaml(target, original)

    def explode(_src: object, _dst: object) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", explode)

    with pytest.raises(ConfigOperationError) as exc_info:
        save_yaml(target, {"site_id": "new", "keep": False})

    error = exc_info.value
    # 替换未发生：错误必须如实标注 replaced=False，调用方不得误以为已持久化。
    assert error.replaced is False
    assert error.code is ConfigErrorCode.IO_ERROR
    # 原目标完整保留、可解析、内容字节级未变。
    assert read_yaml(target) == original
    # 临时文件已被 finally 清理，目录中不留 .tmp 残留。
    assert list(tmp_path.glob(".*.tmp")) == []


def test_st_04_permission_denied_before_replace_is_classified(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """替换前 PermissionError 归类 PERMISSION_DENIED，同样保持 replaced=False。"""
    target = tmp_path / "site.yaml"
    save_yaml(target, {"v": 1})

    def deny(_src: object, _dst: object) -> None:
        raise PermissionError("simulated permission denial")

    monkeypatch.setattr(os, "replace", deny)

    with pytest.raises(ConfigOperationError) as exc_info:
        save_yaml(target, {"v": 2})
    assert exc_info.value.code is ConfigErrorCode.PERMISSION_DENIED
    assert exc_info.value.replaced is False
    assert read_yaml(target) == {"v": 1}
    assert list(tmp_path.glob(".*.tmp")) == []


def test_st_05_post_replace_directory_fsync_failure_reports_uncertainty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = tmp_path / "site.yaml"
    save_yaml(target, {"site_id": "origin"})

    real_fsync = os.fsync
    calls = 0
    directory_fsyncs = 0

    def selective_fsync(fd: int) -> None:
        nonlocal calls, directory_fsyncs
        # 第一次 fsync 作用于临时文件 fd，必须成功；仅父目录同步故障。
        calls += 1
        if calls == 1:
            return real_fsync(fd)
        directory_fsyncs += 1
        raise OSError("simulated directory fsync failure")

    monkeypatch.setattr(os, "fsync", selective_fsync)

    new_data = {"site_id": "new", "evidence": "replaced-before-fsync"}
    with pytest.raises(ConfigOperationError) as exc_info:
        save_yaml(target, new_data)

    error = exc_info.value
    # 替换已经发生：禁止伪称回滚，replaced 必须为 True 以暴露持久化不确定性。
    assert error.replaced is True
    assert error.code is ConfigErrorCode.IO_ERROR
    assert directory_fsyncs == 1
    # os.replace 已完成，目标内容即为新数据。
    assert read_yaml(target) == new_data
    # 临时文件路径已被替换消费，无残留。
    assert list(tmp_path.glob(".*.tmp")) == []
