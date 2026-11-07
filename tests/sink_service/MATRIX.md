# Sink Service v1_20 测试追溯矩阵（S2 交付物）

绑定设计：`docs/Sink_Service_软件设计_v1_20.md` §14.2～§14.4。
本文件随测试代码一起维护；状态口径：

- `IMPLEMENTED`：目标文件已按四层级目录落地，节点可执行且通过；
- `PARTIAL`：有同主题可执行测试但范围/层级未完全覆盖设计条目；
- `ABSENT`：无对应 pytest 归属（场景在 S3/S4 计划中重建，或仍缺失）；
- `NOT_RUN`：测试存在但真实环境不具备，按设计禁止 Mock 冒充。

当前可执行资产：12 个测试文件、135 个节点，全部通过（`pytest tests/sink_service -q`）。
每个文件仅一个层级 Marker，与物理目录一致；专题 Marker（contract/reliability/performance/modbus/file）叠加。

## 1. 设计 §14.3 目标文件矩阵（35 项）落实情况

| 设计目标文件 | 层级 | 状态 | 实际归属 / 说明 |
| --- | --- | --- | --- |
| test_output_policy.py | unit | IMPLEMENTED | `unit/test_output_policy.py`（12）三策略/阈值/合并/STOP 语义 |
| test_sample_identity.py | unit | PARTIAL | SampleBatch v2 契约在 `unit/test_config_validation.py` 与 `component/test_realtime_cache.py` 中间接覆盖；乱序/冲突样本专项 ABSENT |
| test_delivery_state.py | unit | PARTIAL | desired/actual 转换在 `component/test_delivery_runtime.py`（11）覆盖；纯领域状态机单测未单列 |
| test_ack_rules.py | unit | PARTIAL | `unit/test_reliable_seams.py`（8）覆盖能力校验/durable 身份/冷态可观察性；ACK/NAK/DLQ 判定规则仅在遗留 JetStream 系统测试中，已 parked |
| test_delivery_runtime.py | component | IMPLEMENTED | `component/test_delivery_runtime.py`（11）生命周期/看门狗/手动 STOP 不复活/资源所有权 |
| test_realtime_cache.py | component | IMPLEMENTED | `component/test_realtime_cache.py`（8）LRU 16384 FIFO 淘汰、负缓存、拓扑变更清缓存、调度合并 |
| test_config_snapshot.py | component | IMPLEMENTED | `component/test_config_snapshot.py`（14）Push/Apply/CAS/候选容量/失败收敛不推进 runtime |
| test_config_sources.py | integration | PARTIAL | 单 Service 闭包、显式 pathname、默认路径回退拒绝、legacy 字段拒绝在 `unit/test_config_validation.py`（39）；"切换来源需重启"的真实进程验证属 system，ABSENT |
| test_worker_ipc.py | component | ABSENT | Main/Worker IPC 代际/超时/部分收敛，S3 三进程场景重建 |
| test_metrics.py | component | IMPLEMENTED | `component/test_metrics.py`（11）20 指标词汇表、窗口/P99/事件环/冷态诚实 |
| test_diagnose.py | component | ABSENT | 诊断分阶段证据/限流无单测 |
| test_grpc_contract.py | integration | PARTIAL | `integration/test_grpc_contract.py`（8）进程内真实 Servicer + 错误映射/13 RPC 能力集；真实 mTLS/Stub 网络面属 system，ABSENT |
| test_jetstream_real.py | integration | ABSENT | 旧双进程版本已随 v1_20 迁移删除，S3 按三进程重建 |
| test_nats_realtime_real.py | integration | ABSENT | S3 真实 NATS Core 链路 |
| test_adapters_real.py | integration | PARTIAL | `unit/test_modbus_image.py`（9）寄存器映射；`integration/test_redis_batch_contract.py`（2）真实 Redis 500 点批；Kafka/Modbus 真实协议 ABSENT |
| test_config_disk_real.py | integration | IMPLEMENTED | `integration/test_config_repository.py`（10）真实文件系统 GenerationStore 原子提交/CAS/越界/模板不可变 |
| test_lifecycle.py | system | ABSENT | S3；/tmp/qa_sink/smoke_three_process.py 为 pytest 外现有证据脚本 |
| test_delivery_flow.py | system | ABSENT | S3 |
| test_dlq_recovery.py | system | ABSENT | S3 十项故障注入 |
| test_config_apply.py | system | ABSENT | 组件级 Apply 已覆盖于 `component/test_config_snapshot.py`；IPC 部分生效 system 级 ABSENT |
| test_metrics_query.py | system | ABSENT | S3 |
| test_metrics_cycle_e2e.py | system | ABSENT | S3 |
| test_diagnose_pipeline.py | system | ABSENT | S3 |
| test_grpc_api.py | system | ABSENT | S3 |
| test_resource_safety.py | system | ABSENT | S3 |
| test_capacity.py | system | ABSENT | S4 重建（旧 Gate-5 口径已 parked） |
| test_static_assignment.py | component | PARTIAL | 通道角色隔离（delivery_runtime）、精确闭包（config_validation 39 项）已覆盖；无独立归属文件 |
| test_container_scaleout.py | system | ABSENT | 多容器扩展不在单机验收范围 |
| test_field_delivery.py | system | NOT_RUN | 需授权现场目标，环境不具备 |
| test_realtime_expiry_e2e.py | system | ABSENT | S3 场景 10 |
| test_reliable_outage_replay_e2e.py | system | ABSENT | S3 场景 8 |
| test_dlq_capacity_e2e.py | system | ABSENT | S3 场景 9 |
| test_reliable_duplicate_e2e.py | system | ABSENT | S3 场景 3/6 |
| test_replay_starvation_e2e.py | system | ABSENT | S3/S4 |
| test_modbus_server_e2e.py | system | ABSENT | S3 真实 Modbus |

小结：IMPLEMENTED 7、PARTIAL 6、ABSENT 21、NOT_RUN 1。ABSENT 全部集中在
integration 真实链路与 system 层级，是 S3/S4 的明确输入，不以 Mock 或降级断言冒充。

## 2. 设计 §14.4 42 项 UC 场景追溯

42 项 UC-Sxx 全部规定 `system` 层级 + 真实进程/真实基础设施。当前 pytest 树无
system 节点（`system/` 仅保留 `__init__.py`），因此 42 项统一状态为 **ABSENT（待 S3 重建）**，
不计任何通过。低层既有证据与 UC 的对应：

| UC | 低层可执行证据（非 system 替代，仅追溯） |
| --- | --- |
| UC-01/08/09 启停恢复 | delivery_runtime 看门狗/STOP 不复活；config_repository 代际持久化；/tmp/qa_sink 三进程 smoke（pytest 外） |
| UC-02/03/11 派发与隔离 | delivery_runtime 11 项；realtime_cache 路由/负缓存 |
| UC-04 实时派发 | test_output_policy 12 项；test_realtime_cache 8 项；redis_batch_contract 2 项 |
| UC-05/10/12 ACK/DLQ/恢复 | test_reliable_seams 8 项（durable 身份/能力 fail-closed/冷态）；ACK 状态机系统级 ABSENT |
| UC-06/12/14 指标事件 | test_metrics 11 项（窗口/P99/事件环/无数据不伪造） |
| UC-07 配置应用 | test_config_snapshot 14 项 + test_config_repository 10 项 + test_config_validation 39 项 |
| UC-13 诊断 | 无 |
| gRPC 13 RPC（表 14-3） | test_grpc_contract 8 项进程内覆盖；网络/mTLS/RBAC 系统级 ABSENT |

## 3. 验收任务书规定的 11 个覆盖领域当前归属

1. SampleBatch v2 编解码/主体/约束：config_validation、realtime_cache、redis_batch_contract（PARTIAL，契约负例可补强）。
2. Collector 发布契约：Sink 侧消费在 realtime_cache；Collector 装配受 P1 缺陷阻断（见验收报告 S1）。
3. Realtime 路由/缓存/过期：realtime_cache 全绿；过期 e2e ABSENT。
4. Reliable ACK/重试/DLQ：reliable_seams 纯逻辑 PARTIAL；真实 JetStream ABSENT（S3）。
5. 三类适配器：Modbus 映射 9 项、Redis 真实批 2 项；Kafka 真实链路 ABSENT。
6. 三进程隔离：ABSENT（S3）。
7. IPC 认证/代际/超时：ABSENT（S3）。
8. 配置 Get/Push/Apply/热更/重启：组件+磁盘层 63 项已覆盖；进程内热更/重启 ABSENT。
9. 13 个 gRPC RPC：进程内 8 项 PARTIAL；全方法真实 Stub 逐项 ABSENT。
10. 指标/P99/事件/Diagnose：前三者 11 项 IMPLEMENTED；Diagnose ABSENT。
11. 资源安全：delivery_runtime 所有权/失败移除 PARTIAL；未收敛 I/O 系统级 ABSENT。

## 4. 废弃测试处置

6 个 v1_1x 双进程/Reader-Writer/旧 Gate-5 测试（旧 `test_stage1_dual_process`、
`test_stage3_real_infra`、`test_stage3_reliable_jetstream`、`test_stage3_modbus_tcp`、
`test_stage1_config_reader`、`test_stage4_performance`）所依赖的双 Service、
`process_role`/`config_writer`、`--peer-service-file` 与旧 Gate-5 口径在 v1_20 已删除，
无法原地迁移，已从仓库删除而非 skip 保留；其覆盖的场景在本矩阵 §1/§2 标注归属，
并由 S3/S4 的三进程真实链路脚本（仓库外 QA harness）重建。
无 skip/xfail 被用来冒充通过；当前树内零 skip、零 xfail。
