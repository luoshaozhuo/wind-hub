"""长时稳定性 soak（spec §13）：1h smoke / 8h pre-release / 24h release candidate。

**默认不执行**——短周期开发环境跑长 soak 没有意义。显式开启：

.. code-block:: bash

    WIND_HUB_SOAK_PROFILE=smoke_1h pytest -m soak tests/soak/test_long_running.py
    WIND_HUB_SOAK_PROFILE=prerelease_8h pytest ...   # 预发布
    WIND_HUB_SOAK_PROFILE=rc_24h pytest ...          # 发布候选

``WIND_HUB_SOAK_DURATION_S`` 可覆盖时长（用于验证 soak 路径本身，如 60s）。
报告写入仓库根目录 ``soak_report_<profile>.md / .json``。

稳定性判据：测量窗口内零背压丢弃、零重复、错过周期率 < 1%；内存增长
有界（终点 < 起点 +20% 且 < 起点 +100MB）；asyncio 任务数不爬升（终点
≤ 起点 +10——采集/sink 任务在启动期已全部创建，之后只应有瞬时抖动）。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from tests.reliability.soak.report import generate_soak_json, generate_soak_markdown
from tests.reliability.soak.runner import PROFILES, run_soak

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]

#: 长 soak 档位 → 测量时长（秒）。
_DURATION_S = {
    "smoke_1h": 3600.0,
    "prerelease_8h": 8 * 3600.0,
    "rc_24h": 24 * 3600.0,
}


def _soak_profile_from_env() -> str:
    profile = os.environ.get("WIND_HUB_SOAK_PROFILE", "").strip()
    if not profile:
        pytest.skip(
            "SKIPPED: 长 soak 默认不执行；按需设置 "
            "WIND_HUB_SOAK_PROFILE=smoke_1h|prerelease_8h|rc_24h"
        )
    if profile not in _DURATION_S:
        pytest.fail(
            f"未知 WIND_HUB_SOAK_PROFILE={profile!r}（可选：{sorted(_DURATION_S)}）"
        )
    return profile


async def test_long_running_target_load() -> None:
    soak_name = _soak_profile_from_env()
    duration_s = float(os.environ.get("WIND_HUB_SOAK_DURATION_S", _DURATION_S[soak_name]))
    warmup_s = min(60.0, duration_s * 0.05)

    profile = PROFILES["target_100x10hz_500x1hz"]
    logger.info(
        "长 soak 开始：档位 %s，时长 %gs（预热 %gs）", soak_name, duration_s, warmup_s
    )
    metrics = await run_soak(
        profile,
        duration_s=duration_s,
        warmup_s=warmup_s,
        on_ready=lambda: logger.info("预热结束，进入测量窗口"),
    )

    md_path = _REPO_ROOT / f"soak_report_{soak_name}.md"
    generate_soak_markdown(
        metrics, md_path, title=f"wind-hub Soak — {soak_name} ({profile.name})"
    )
    generate_soak_json(metrics, md_path.with_suffix(".json"))

    # ---- 数据完整性 ----
    assert metrics.points_dropped == 0, f"背压丢弃 {metrics.points_dropped} 点"
    assert metrics.sink_received >= metrics.points_routed * 0.99
    assert metrics.sink_duplicates == 0, f"重复点 {metrics.sink_duplicates} 个"

    # ---- 节拍保持 ----
    for c in metrics.cycles:
        missed_ratio = c.missed_cycles / max(c.cycles, 1)
        assert missed_ratio < 0.01, (
            f"任务 {c.task_id} 错过周期率 {missed_ratio:.2%} 超过 1%"
        )

    # ---- 资源稳定（泄漏判据）----
    if metrics.memory_start_mb > 0:
        growth_mb = metrics.memory_end_mb - metrics.memory_start_mb
        assert metrics.memory_end_mb < metrics.memory_start_mb * 1.2, (
            f"内存增长超限：{metrics.memory_start_mb:.1f} → "
            f"{metrics.memory_end_mb:.1f} MB（+{growth_mb:.1f} MB）"
        )
        assert growth_mb < 100.0, f"内存绝对增长 +{growth_mb:.1f} MB 超过 100 MB"
    if metrics.asyncio_tasks_start > 0:
        assert metrics.asyncio_tasks_end <= metrics.asyncio_tasks_start + 10, (
            f"asyncio 任务数爬升：{metrics.asyncio_tasks_start} → "
            f"{metrics.asyncio_tasks_end}"
        )
