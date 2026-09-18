"""性能压测框架（step22）——三协议 × tc netem 网络场景 × 统一指标报告。

本目录是**压测组件库**，不是 pytest 测试目录（没有 ``test_*.py``）：
压测本身需要 root 权限（tc / ip 命令）且耗时长，不进入 pytest 默认收集，
统一通过 ``scripts/run_benchmark.py`` 显式触发（决策 9/10）。组件的
单元测试（全部 mock，不真实跑压测）在 ``tests/unit/perf/`` 下。

模块：

- :mod:`~tests.perf.scenarios` — 压测场景定义（核心 8 场景 + 快速 3 场景）。
- :mod:`~tests.perf.veth` — veth pair 管理（10.99.0.1 ↔ 10.99.0.2）。
- :mod:`~tests.perf.netem` — tc netem 封装（延迟/抖动/丢包/中断）。
- :mod:`~tests.perf.servers` — 三个协议的本地真实 server 启动。
- :mod:`~tests.perf.collector` — 性能指标采集（吞吐/延迟/资源/重连）。
- :mod:`~tests.perf.reporter` — Markdown / JSON 报告生成。
- :mod:`~tests.perf.runner` — 协议无关的压测主体。
"""
