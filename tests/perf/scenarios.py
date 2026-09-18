"""压测场景定义（决策 3/4）——模块级常量，可直接扩展。

每个场景是一个 :class:`~tests.perf.netem.NetemScenario`；``ideal`` 以外
的场景经 tc netem 注入到 veth pair 上。``outage_*`` 场景不在启动时应用
规则，而是在测量阶段由 runner 触发一次中断-恢复（模拟基站掉线）。
"""

from __future__ import annotations

from tests.perf.netem import NetemScenario

# 核心 8 场景（决策 3）：完整矩阵，跑正式报告用。
CORE_SCENARIOS: list[NetemScenario] = [
    NetemScenario(name="ideal"),
    NetemScenario(name="delay_1ms", delay_ms=1),
    NetemScenario(name="delay_10ms", delay_ms=10),
    NetemScenario(name="delay_50ms", delay_ms=50),
    NetemScenario(name="jitter_10ms_5ms", delay_ms=10, jitter_ms=5),
    NetemScenario(name="loss_0.1pct", loss_pct=0.1),
    NetemScenario(name="loss_1pct", loss_pct=1),
    NetemScenario(name="outage_5s", outage_duration_s=5),
]

# 快速 3 场景：冒烟/回归用（--quick）。
QUICK_SCENARIOS: list[NetemScenario] = [
    NetemScenario(name="ideal"),
    NetemScenario(name="delay_10ms", delay_ms=10),
    NetemScenario(name="loss_1pct", loss_pct=1),
]
