---
name: local-qualification
description: Run explicit hardware, performance, or soak qualification only when the user requests it and the required real environment is available.
---

# Local Qualification

仅用户明确要求时执行。先运行：

```bash
python3 scripts/dev.py env
```

再按目标调用：

```bash
python3 scripts/dev.py python scripts/ci_gate.py hardware
python3 scripts/dev.py python scripts/ci_gate.py performance
python3 scripts/dev.py python scripts/ci_gate.py soak
```

规则：

1. `hardware` 必须是真实 ADS/TwinCAT PLC。
2. `performance` 是 `tests/src/performance/` 的进程内 pytest 性能基线，无需 root/tc netem；旧 netem Benchmark 已退役，`--mode`/`--duration` 参数已删除。
3. `soak` 是 `tests/src/reliability/` 的有界负载可靠性 pytest 用例；旧 profile/duration 环境变量已删除，无外部档位输入。
4. 环境不足为 `NOT_EXECUTED`，不得 fallback 到 mock。
5. 不自动触发真实 PLC 写入或其他高成本/现场风险动作。
6. 结果可用 `--output <path>.xml` 绑定具体 Git SHA 输出 JUnit XML（不产生 Markdown/JSON 性能报告）。
