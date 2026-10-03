---
name: local-qualification
description: Run explicit hardware, performance, or soak qualification only when the user requests it and the required real environment is available.
---

# Local Qualification

## 目的

执行真实 ADS 硬件、性能/netem 或 soak 资格验证。

## 规则

1. 仅用户明确要求时执行。
2. 执行前必须 preflight 环境。
3. 可执行项：
   - `hardware`：真实 ADS/TwinCAT PLC；
   - `performance`：root/CAP_NET_ADMIN + tc/netem；
   - `soak`：明确 profile/duration 的长稳验证。
4. 环境不足必须报告 `NOT_EXECUTED`，不得 fallback 到 mock。
5. 不自动触发真实 PLC 写入、8h/24h soak 或其他高成本/现场风险动作。
6. 报告绑定具体 Git SHA，并记录环境/profile。
