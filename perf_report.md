# wind-hub Performance Benchmark

## 1. 摘要

- 日期：2026-10-02 19:56 UTC
- 平台：Linux-6.6.87.2-microsoft-standard-WSL2-x86_64-with-glibc2.43
- Python：3.11.16（20 核）
- 协议：ads, iec104, modbus
- 场景数：3（delay_10ms, ideal, loss_1pct）
- 单场景测量时长：30s（另有预热，不计入统计）
- Sink：NullSink（隔离外部 IO，测采集 + Task 分发）

## 2. 性能矩阵（协议 × 场景）

| 协议 | 场景 | 吞吐 (点/s) | P50 (ms) | P95 (ms) | P99 (ms) | 最大 (ms) | 丢点 |
|---|---|---|---|---|---|---|---|
| modbus | ideal | 10000 | 1.32 | 2.37 | 4.71 | 50.51 | 0 |
| modbus | delay_10ms | 10000 | 0.97 | 2.08 | 2.61 | 114.33 | 0 |
| modbus | loss_1pct | 10000 | 0.98 | 1.55 | 1.86 | 53.79 | 0 |
| iec104 | ideal | 4850 | 9.79 | 15.13 | 78.85 | 115.89 | 0 |
| iec104 | delay_10ms | 4817 | 9.85 | 15.20 | 80.54 | 710.08 | 0 |
| iec104 | loss_1pct | 4817 | 9.86 | 14.83 | 90.23 | 128.48 | 0 |
| ads | ideal | 1500 | 0.76 | 1.07 | 1.19 | 1.54 | 0 |
| ads | delay_10ms | 1500 | 0.73 | 1.04 | 2.16 | 39.13 | 0 |
| ads | loss_1pct | 1500 | 0.73 | 1.02 | 1.15 | 1.65 | 0 |

## 3. 延迟分布

单点延迟 = 采集时间戳 → 引擎处理完成（engine observer 口径）。

| 协议 | 场景 | P50 (ms) | P95 (ms) | P99 (ms) | 最大 (ms) |
|---|---|---|---|---|---|
| modbus | ideal | 1.32 | 2.37 | 4.71 | 50.51 |
| modbus | delay_10ms | 0.97 | 2.08 | 2.61 | 114.33 |
| modbus | loss_1pct | 0.98 | 1.55 | 1.86 | 53.79 |
| iec104 | ideal | 9.79 | 15.13 | 78.85 | 115.89 |
| iec104 | delay_10ms | 9.85 | 15.20 | 80.54 | 710.08 |
| iec104 | loss_1pct | 9.86 | 14.83 | 90.23 | 128.48 |
| ads | ideal | 0.76 | 1.07 | 1.19 | 1.54 |
| ads | delay_10ms | 0.73 | 1.04 | 2.16 | 39.13 |
| ads | loss_1pct | 0.73 | 1.02 | 1.15 | 1.65 |

曲线图：`perf_latency.png`（matplotlib 可用时生成）。

## 4. 重连行为

以驱动 ``health()`` 每秒轮询的「健康 → 不健康 → 健康」跳变计数。

| 协议 | 场景 | 重连次数 | 最长恢复 (ms) |
|---|---|---|---|
| modbus | ideal | 0 | 0 |
| modbus | delay_10ms | 0 | 0 |
| modbus | loss_1pct | 0 | 0 |
| iec104 | ideal | 0 | 0 |
| iec104 | delay_10ms | 0 | 0 |
| iec104 | loss_1pct | 0 | 0 |
| ads | ideal | 0 | 0 |
| ads | delay_10ms | 0 | 0 |
| ads | loss_1pct | 0 | 0 |

## 5. 资源占用（wind-hub 进程）

CPU 为测量窗口平均利用率（单核 100% 上限）；内存/FD 为窗口峰值。

| 协议 | 场景 | CPU (%) | 峰值内存 (MiB) | 峰值 FD |
|---|---|---|---|---|
| modbus | ideal | 8.7 | 516.7 | 10 |
| modbus | delay_10ms | 8.4 | 531.9 | 10 |
| modbus | loss_1pct | 8.1 | 539.2 | 12 |
| iec104 | ideal | 23.8 | 549.1 | 10 |
| iec104 | delay_10ms | 24.2 | 549.1 | 10 |
| iec104 | loss_1pct | 24.3 | 546.1 | 10 |
| ads | ideal | 2.4 | 548.6 | 10 |
| ads | delay_10ms | 1.7 | 547.7 | 10 |
| ads | loss_1pct | 1.8 | 548.2 | 10 |

## 6. 结论与建议

- 理想链路下 modbus 吞吐 10000 点/s，P99 延迟 4.71 ms。
- 理想链路下 iec104 吞吐 4850 点/s，P99 延迟 78.85 ms。
- 理想链路下 ads 吞吐 1500 点/s，P99 延迟 1.19 ms。
- 所有场景无丢点。

