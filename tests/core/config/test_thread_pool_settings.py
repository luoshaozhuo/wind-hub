"""runtime.thread_pool.max_workers 配置契约（restart-required 进程级线程池）。"""

from __future__ import annotations

from pathlib import Path
from shutil import copytree

import pytest

from core.application.errors import ConfigError
from core.application.settings import RuntimeSettings, ThreadPoolSettings
from core.infrastructure.config import YamlConfigAdapter

_FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "configs" / "example_ads"


def test_default_thread_pool_is_32() -> None:
    assert ThreadPoolSettings().max_workers == 32
    # 未配置时保留 None（无损保存语义）；默认 32 由进程加载层回退。
    assert RuntimeSettings().thread_pool is None
    assert (RuntimeSettings().thread_pool or ThreadPoolSettings()).max_workers == 32


@pytest.mark.parametrize("bad", [0, -1, 257])
def test_rejects_out_of_range(bad: int) -> None:
    with pytest.raises(ValueError, match="1\\.\\.256"):
        ThreadPoolSettings(max_workers=bad)


@pytest.mark.parametrize("bad", [True, 1.5, "32", None])
def test_rejects_non_integer(bad: object) -> None:
    with pytest.raises(ValueError, match="integer"):
        ThreadPoolSettings(max_workers=bad)  # type: ignore[arg-type]


def _load_system(tmp_path: Path, runtime_block: str) -> object:
    config_dir = copytree(_FIXTURE, tmp_path / "cfg")
    system = config_dir / "system.yaml"
    lines = system.read_text().splitlines()
    # 替换 fixture 中整个 runtime: 段（到 interfaces: 之前）。
    head = lines[: lines.index("runtime:")]
    tail = lines[lines.index("interfaces:") :]
    system.write_text("\n".join([*head, *runtime_block.splitlines(), *tail]) + "\n")
    return YamlConfigAdapter(config_dir).load().system.runtime


def test_yaml_thread_pool_block_parses(tmp_path: Path) -> None:
    runtime = _load_system(
        tmp_path,
        (
            "runtime:\n"
            "  watchdog_interval: 5.0\n"
            "  health_timeout: 1.0\n"
            "  thread_pool:\n"
            "    max_workers: 64\n"
        ),
    )
    assert runtime.thread_pool.max_workers == 64


def test_yaml_thread_pool_absent_stays_unconfigured(tmp_path: Path) -> None:
    runtime = _load_system(
        tmp_path,
        "runtime:\n  watchdog_interval: 5.0\n  health_timeout: 1.0\n",
    )
    # 缺省不实体化，保证配置保存无损往返；32 在进程加载层回退。
    assert runtime.thread_pool is None


def test_yaml_thread_pool_empty_block_uses_default(tmp_path: Path) -> None:
    runtime = _load_system(
        tmp_path,
        "runtime:\n  thread_pool: {}\n",
    )
    assert runtime.thread_pool is not None
    assert runtime.thread_pool.max_workers == 32


@pytest.mark.parametrize(
    "block",
    [
        "runtime:\n  thread_pool:\n    max_workers: 0\n",
        "runtime:\n  thread_pool:\n    max_workers: 257\n",
        "runtime:\n  thread_pool:\n    max_workers: '32'\n",
        "runtime:\n  thread_pool:\n    workers: 32\n",
        "runtime:\n  thread_pool: 32\n",
    ],
)
def test_yaml_thread_pool_invalid_rejected(tmp_path: Path, block: str) -> None:
    with pytest.raises(ConfigError):
        _load_system(tmp_path, block)
