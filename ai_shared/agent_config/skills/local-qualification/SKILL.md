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
python3 scripts/dev.py python scripts/ci_gate.py performance --mode quick
python3 scripts/dev.py python scripts/ci_gate.py soak
```

规则：

1. `hardware` 必须是真实 ADS/TwinCAT PLC。
2. `performance` 必须满足 root/CAP_NET_ADMIN 与 tc/netem 要求。
3. `soak` 必须明确 profile/duration。
4. 环境不足为 `NOT_EXECUTED`，不得 fallback 到 mock。
5. 不自动触发真实 PLC 写入、8h/24h soak 或其他高成本/现场风险动作。
6. 报告绑定具体 Git SHA，并记录环境/profile。
