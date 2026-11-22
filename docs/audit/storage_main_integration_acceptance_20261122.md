# Storage → main 融合实施与验收报告（2026-11-22）

## 1. 结论与 Git 基线

结论：**INTEGRATION_READY_WITH_LIMITATIONS**。已执行已裁决的融合编码及定向验证，未发现本次新增 P0/P1/P2；保留 main 既有 Sink 测试失败和 containerd 验证限制，不能解释为全仓测试全绿。最终 main 合并由 ChatGPT 裁决。

- 融合分支：`merge/storage-main-integration-20261122`。
- 本次验收产品代码 HEAD：`ab38110b9d6376805784ed51c640f13ac9400e1c`；本报告以随后独立文档提交交付，最终交付 HEAD 由 `git log -1` 及交付摘要给出，避免文档自引用 SHA。
- 重新 fetch 后 `origin/main`：`8eaa3ef842bf49d9a54e45c965ef3e24c11fa89d`。
- 来源 `origin/feat/storage-service-v1-implementation`：`d1d936822abab45083555d48030f5d5d0a23860e`。
- merge-base：`b86a32534ce7af49d492cd220425c9fb27c7be50`。
- 接续时已有 M1/M2 三个提交：`2e453a82e`、`2abb30e09`、`6a0906394`；本轮完成其余融合并清理未使用模块。
- 未修改或 push main、未修改来源分支、未创建 PR、未 rebase/force push。按 main 为基增量引入，不执行整体 ours/theirs。

## 2. M1–M5 融合结果

| 范围 | 结果与保留证据 |
|---|---|
| Contracts | main 七字段 SampleItem v3、严格字段校验原样保留；configuration_repository 仅新增 GetServiceConfigBundle 消息/RPC；common 增加 target/active revision；Storage Proto 和四生成物纳入生成脚本，执行全量再生成及一致性检查 |
| Core | 增加 deployment.py；publication_identity 保留 tasks/devices_sim/无版本目录规则，增量接受 Deployment 与版本文件名；不引入无生产调用方的 local_selection |
| Configuration | main B3 完整目录发布、原鉴权、任务钉版及 `_resolve_legacy_sources` 原调用通路保留；增加 deployment 校验、版本闭包 repository/ports、ServiceConfigBundle application/server 整链；补充 Sink/Delivery Instance→Template 固定依赖 |
| CLI | main 的 85 个叶子全部保留，增加 9 个 Storage 叶子，总计 94，重复数 0；保留其他服务 TLS/Bearer；storage 内部通道与 WIND_HUB_STORAGE_OPERATOR_TOKEN 增量接线 |
| 非 Storage | Sink/Collector/Commander/Industrial Gateway/Data Generator/Web Server 生产目录均与 origin/main 字节一致；sink_service.yaml 保留 schema 2；不引入 rich SampleItem、工程级别别名或伪造 RAW 编码 |
| 配置与依赖 | cli.example 仅增加 Storage 块；两份 Storage YAML 原样引入；taos-ws-py/Storage extra、入口和锁文件纳入；Ruff 豁免限生成 RPC 方法命名，mypy 保持 strict，import-linter 增加三条 Storage 契约 |
| Fixture | configuration_live.py 与 main 完全相同；F3/F6 改用 main 两参工厂和正式 token 来源；Storage 生产代码零修改 |

此前 M2 引入 `core.infrastructure.grpc_bearer` 及其专属测试，但全仓生产调用方为零。按任务 §7/§8 的保留条件移除，不为保留孤儿模块而改造已冻结认证。该专属测试验证的是本次明确不引入的共享抽象，原 main 无此测试；其删除不削减 main 或 Storage 有效测试覆盖。认证替代证据为 main Configuration 安全集成、CLI 测试及 Storage 自有授权测试与真实重启部署流。未引入 bearer.py/storage_live.py 等无实际调用方辅助文件。

## 3. Storage 权威文件保留

以来源最新 SHA 的 Git blob 与工作树逐字节比较：53 个权威文件中 **51 个完全一致**，仅以下两个测试适配：

1. `tests/storage_service/integration/test_cli_restart_activation.py`：删除旧 PKI/旧 bearer fixture 依赖，采用 main `write_live_configuration(root, port)` 和 `live.tokens["viewer"]`；保留原配置发布、target/active、停止重启、参数生效及历史绑定断言。
2. `tests/storage_service/unit/test_sink_wire_contract.py`：四个旧 rich API 测试改为五个 main 正式 API 测试；覆盖 protocol/business 编解码、完整业务投影要求、Sink 拒绝伪 RAW、Storage 合法 RAW 解码、双方拒绝 engineering 别名。原 94 unit 语义扩为 95 unit，component 仍为 11。

逐字节保持的集合包括全部 `src/storage_service/**`、其余 Storage 测试、Storage CLI、两份服务/Bootstrap YAML、v1_4 设计文档及 F5/F6 报告。合并前审查报告另按来源保留，属于历史证据，其旧 SHA/建议不覆盖本报告或当前任务裁决。

## 4. 80 个文本冲突逐项台账

对最新 main/Storage 重新运行 `git -c core.quotePath=false merge-tree --write-tree --name-only origin/main origin/feat/storage-service-v1-implementation`，退出码 1，重现 80 个冲突；该命令只构造模拟树，不修改工作树或分支。当前不是未完成的 Git merge。

统计：RESOLVED=80、BLOCKED=0、NOT_APPLICABLE=0。64 项直接保留 main 原文件，其余 16 项手工增量融合或重新生成。未移植旧分支平行 CLI/非 Storage 重构，不删除 main 有效测试。

| # | 路径 | 状态 | 处理 |
|---|---|---|---|
| 1 | `configs/services/cli.example.yaml` | RESOLVED | 增量手工融合/重新生成，保留 main 契约 |
| 2 | `configs/services/sink_service.yaml` | RESOLVED | 保留 main 原文件 |
| 3 | `docs/design/Sink_Service_软件设计.md` | RESOLVED | 保留 main 原文件 |
| 4 | `src/cli/common_commands.py` | RESOLVED | 保留 main 原文件 |
| 5 | `src/cli/configuration_commands.py` | RESOLVED | 保留 main 原文件 |
| 6 | `src/cli/diagnose_commands.py` | RESOLVED | 保留 main 原文件 |
| 7 | `src/cli/runtime.py` | RESOLVED | 增量手工融合/重新生成，保留 main 契约 |
| 8 | `src/collector/application/acquisition.py` | RESOLVED | 保留 main 原文件 |
| 9 | `src/collector/application/batch_builder.py` | RESOLVED | 保留 main 原文件 |
| 10 | `src/collector/application/diagnostics.py` | RESOLVED | 保留 main 原文件 |
| 11 | `src/collector/application/ports/acquisition.py` | RESOLVED | 保留 main 原文件 |
| 12 | `src/collector/infrastructure/adapters/outbound/publisher.py` | RESOLVED | 保留 main 原文件 |
| 13 | `src/configuration_service/application/ports.py` | RESOLVED | 增量手工融合/重新生成，保留 main 契约 |
| 14 | `src/configuration_service/application/service.py` | RESOLVED | 增量手工融合/重新生成，保留 main 契约 |
| 15 | `src/configuration_service/infrastructure/adapters/inbound/grpc/repository_server.py` | RESOLVED | 增量手工融合/重新生成，保留 main 契约 |
| 16 | `src/configuration_service/infrastructure/adapters/outbound/sqlite/repository.py` | RESOLVED | 增量手工融合/重新生成，保留 main 契约 |
| 17 | `src/contracts/configuration_repository.proto` | RESOLVED | 增量手工融合/重新生成，保留 main 契约 |
| 18 | `src/contracts/configuration_repository_pb2.py` | RESOLVED | 增量手工融合/重新生成，保留 main 契约 |
| 19 | `src/contracts/configuration_repository_pb2.pyi` | RESOLVED | 增量手工融合/重新生成，保留 main 契约 |
| 20 | `src/contracts/configuration_repository_pb2_grpc.py` | RESOLVED | 增量手工融合/重新生成，保留 main 契约 |
| 21 | `src/contracts/configuration_repository_pb2_grpc.pyi` | RESOLVED | 增量手工融合/重新生成，保留 main 契约 |
| 22 | `src/contracts/sample_batch.py` | RESOLVED | 保留 main 原文件 |
| 23 | `src/core/configuration/publication_identity.py` | RESOLVED | 增量手工融合/重新生成，保留 main 契约 |
| 24 | `src/industrial_gateway/infrastructure/adapters/inbound/grpc/data_server.py` | RESOLVED | 保留 main 原文件 |
| 25 | `src/industrial_gateway/infrastructure/config/schema.py` | RESOLVED | 保留 main 原文件 |
| 26 | `src/sink_service/application/config.py` | RESOLVED | 保留 main 原文件 |
| 27 | `src/sink_service/application/delivery_point.py` | RESOLVED | 保留 main 原文件 |
| 28 | `src/sink_service/infrastructure/adapters/inbound/grpc_auth.py` | RESOLVED | 保留 main 原文件 |
| 29 | `src/sink_service/infrastructure/adapters/inbound/grpc_server.py` | RESOLVED | 保留 main 原文件 |
| 30 | `src/sink_service/infrastructure/adapters/outbound/gateway.py` | RESOLVED | 保留 main 原文件 |
| 31 | `src/sink_service/infrastructure/adapters/outbound/kafka_wire.py` | RESOLVED | 保留 main 原文件 |
| 32 | `src/sink_service/infrastructure/config_schema.py` | RESOLVED | 保留 main 原文件 |
| 33 | `src/sink_service/infrastructure/reliable_runtime.py` | RESOLVED | 保留 main 原文件 |
| 34 | `src/web_server/adapter/outbound/grpc/sink.py` | RESOLVED | 保留 main 原文件 |
| 35 | `tests/cli/conftest.py` | RESOLVED | 保留 main 原文件 |
| 36 | `tests/cli/e2e_util.py` | RESOLVED | 保留 main 原文件 |
| 37 | `tests/cli/test_command_tree.py` | RESOLVED | 增量手工融合/重新生成，保留 main 契约 |
| 38 | `tests/cli/test_r1f3_strict_cli_e2e.py` | RESOLVED | 保留 main 原文件 |
| 39 | `tests/cli/test_shell_e2e.py` | RESOLVED | 保留 main 原文件 |
| 40 | `tests/collector/component/test_acquisition_runtime_v1.py` | RESOLVED | 保留 main 原文件 |
| 41 | `tests/collector/unit/test_batch_builder.py` | RESOLVED | 保留 main 原文件 |
| 42 | `tests/collector/unit/test_publisher_sample_evidence.py` | RESOLVED | 保留 main 原文件 |
| 43 | `tests/configuration_service/component/test_bundle_exact_limits.py` | RESOLVED | 保留 main 原文件 |
| 44 | `tests/configuration_service/component/test_repository_transactions.py` | RESOLVED | 保留 main 原文件 |
| 45 | `tests/configuration_service/component/test_schema_regressions.py` | RESOLVED | 增量手工融合/重新生成，保留 main 契约 |
| 46 | `tests/configuration_service/component/test_shipped_yaml_repository.py` | RESOLVED | 增量手工融合/重新生成，保留 main 契约 |
| 47 | `tests/configuration_service/component/test_sqlite_backup.py` | RESOLVED | 保留 main 原文件 |
| 48 | `tests/configuration_service/component/test_typed_references.py` | RESOLVED | 保留 main 原文件 |
| 49 | `tests/configuration_service/conftest.py` | RESOLVED | 保留 main 原文件 |
| 50 | `tests/configuration_service/integration/test_bundle_limits.py` | RESOLVED | 保留 main 原文件 |
| 51 | `tests/configuration_service/integration/test_cli_integration.py` | RESOLVED | 保留 main 原文件 |
| 52 | `tests/configuration_service/integration/test_pagination_history.py` | RESOLVED | 保留 main 原文件 |
| 53 | `tests/configuration_service/integration/test_repository_rpc.py` | RESOLVED | 保留 main 原文件 |
| 54 | `tests/configuration_service/integration/test_security.py` | RESOLVED | 保留 main 原文件 |
| 55 | `tests/configuration_service/integration/test_version_conflict.py` | RESOLVED | 保留 main 原文件 |
| 56 | `tests/configuration_service/reliability/test_backup_process_restart.py` | RESOLVED | 保留 main 原文件 |
| 57 | `tests/contracts/test_generation.py` | RESOLVED | 增量手工融合/重新生成，保留 main 契约 |
| 58 | `tests/sink_service/_stage1_fixtures.py` | RESOLVED | 保留 main 原文件 |
| 59 | `tests/sink_service/component/test_b1_encoded_kafka_delivery.py` | RESOLVED | 保留 main 原文件 |
| 60 | `tests/sink_service/component/test_realtime_cache.py` | RESOLVED | 保留 main 原文件 |
| 61 | `tests/sink_service/system/conftest.py` | RESOLVED | 保留 main 原文件 |
| 62 | `tests/sink_service/system/test_concurrent_delivery_e2e.py` | RESOLVED | 保留 main 原文件 |
| 63 | `tests/sink_service/system/test_delivery_flow.py` | RESOLVED | 保留 main 原文件 |
| 64 | `tests/sink_service/system/test_grpc_api.py` | RESOLVED | 保留 main 原文件 |
| 65 | `tests/sink_service/system/test_uc10_s03_version_ownership.py` | RESOLVED | 保留 main 原文件 |
| 66 | `tests/sink_service/system/test_uc11_s02_shared_sink_isolation.py` | RESOLVED | 保留 main 原文件 |
| 67 | `tests/sink_service/unit/test_kafka_device_stripes.py` | RESOLVED | 保留 main 原文件 |
| 68 | `tests/sink_service/unit/test_kafka_output_partition_cache.py` | RESOLVED | 保留 main 原文件 |
| 69 | `tests/sink_service/unit/test_kafka_wire_b3_bytes.py` | RESOLVED | 保留 main 原文件 |
| 70 | `tests/sink_service/unit/test_kafka_wire_parity.py` | RESOLVED | 保留 main 原文件 |
| 71 | `tests/sink_service/unit/test_reliable_sample_evidence.py` | RESOLVED | 保留 main 原文件 |
| 72 | `tests/sink_service/unit/test_replay_header_ack_bypass.py` | RESOLVED | 保留 main 原文件 |
| 73 | `tests/sink_service/unit/test_strict_config_keys_domain.py` | RESOLVED | 保留 main 原文件 |
| 74 | `tests/src/component/test_collector/test_publisher_v2.py` | RESOLVED | 保留 main 原文件 |
| 75 | `tests/src/unit/test_contracts/test_sample_batch.py` | RESOLVED | 保留 main 原文件 |
| 76 | `tests/support/configuration_live.py` | RESOLVED | 保留 main 原文件 |
| 77 | `tests/system/e2e/full_chain/conftest.py` | RESOLVED | 保留 main 原文件 |
| 78 | `tests/system/e2e/full_chain/integration_segments.py` | RESOLVED | 保留 main 原文件 |
| 79 | `tests/system/e2e/full_chain/test_e2e_semantic_matrix.py` | RESOLVED | 保留 main 原文件 |
| 80 | `tests/system/e2e/full_chain/test_e2e_surface_matrix.py` | RESOLVED | 保留 main 原文件 |

## 5. 潜伏语义文件

合并前报告的“13 个”按来源中引用 grpc_bearer 的非 CLI 文件明确列出；CLI runtime 单独融合，并额外检查报告点名的六个 Sink 文件。

| 文件 | 状态 | 证据 |
|---|---|---|
| `src/commander/infrastructure/adapters/inbound/grpc/server.py` | RESOLVED | 与 main 完全一致，无旧共享认证依赖 |
| `src/commander/infrastructure/adapters/inbound/grpc_transport.py` | RESOLVED | 与 main 完全一致，无旧共享认证依赖 |
| `src/commander/infrastructure/adapters/outbound/gateway.py` | RESOLVED | 与 main 完全一致，无旧共享认证依赖 |
| `src/data_generator/infrastructure/adapters/inbound/server.py` | RESOLVED | 与 main 完全一致，无旧共享认证依赖 |
| `src/data_generator/infrastructure/gateway_auth.py` | RESOLVED | 与 main 完全一致，无旧共享认证依赖 |
| `src/industrial_gateway/infrastructure/adapters/inbound/grpc/data_server.py` | RESOLVED | 与 main 完全一致，无旧共享认证依赖 |
| `src/industrial_gateway/infrastructure/adapters/outbound/grpc/commander_client.py` | RESOLVED | 与 main 完全一致，无旧共享认证依赖 |
| `src/industrial_gateway/infrastructure/adapters/outbound/grpc/generator_client.py` | RESOLVED | 与 main 完全一致，无旧共享认证依赖 |
| `src/sink_service/infrastructure/adapters/inbound/grpc_auth.py` | RESOLVED | 与 main 完全一致，无旧共享认证依赖 |
| `src/sink_service/infrastructure/adapters/outbound/gateway.py` | RESOLVED | 与 main 完全一致，无旧共享认证依赖 |
| `src/web_server/adapter/outbound/grpc/collector.py` | RESOLVED | 与 main 完全一致，无旧共享认证依赖 |
| `src/web_server/adapter/outbound/grpc/commander.py` | RESOLVED | 与 main 完全一致，无旧共享认证依赖 |
| `src/web_server/adapter/outbound/grpc/sink.py` | RESOLVED | 与 main 完全一致，无旧共享认证依赖 |
| `src/sink_service/application/reliable_policy.py` | RESOLVED（补充） | 与 main 完全一致，保留正式 Delivery API |
| `src/sink_service/application/output_pool.py` | RESOLVED（补充） | 与 main 完全一致，保留正式 Delivery API |
| `src/sink_service/application/ports/output.py` | RESOLVED（补充） | 与 main 完全一致，保留正式 Delivery API |
| `src/sink_service/application/realtime.py` | RESOLVED（补充） | 与 main 完全一致，保留正式 Delivery API |
| `src/sink_service/application/realtime_delivery.py` | RESOLVED（补充） | 与 main 完全一致，保留正式 Delivery API |
| `src/sink_service/infrastructure/adapters/inbound/nats_consumer.py` | RESOLVED（补充） | 与 main 完全一致，保留正式 Delivery API |

`src/cli/runtime.py` 为 RESOLVED：main 通道/TLS 保留，仅增加 Storage。`tests/src/component/test_collector/test_publisher_v2.py` 与 main 一致；378 个业务 Python 模块导入冒烟通过。冲突标记扫描无命中、`git diff --check` 为 0。首次导入扫描误包含 CLI `__main__`，其正常参数校验退出 2；随后排除可执行入口，仅导入模块，退出 0，未修改生产入口行为。

## 6. 静态检查与功能验证

所有命令使用 env 检查通过的 wind-hub Python 3.11.16；不读取或输出 `.env.local`。真实 TDengine 集使用测试栈生成的临时连接信息与测试凭据，未回退 mock。重复测试项不累计为独立覆盖数量。

| 命令/检查 | 结果 | 退出码 |
|---|---|---|
| `python3 scripts/dev.py env` | PASS | 0 |
| `ruff check src tests scripts` | PASS | 0 |
| `ruff format --check src tests scripts` | PASS，1064 文件 | 0 |
| `mypy src` | PASS | 0 |
| `lint-imports` | PASS，22 kept/0 broken | 0 |
| `python scripts/generate_rpc.py` | PASS，全量生成 | 0 |
| `python scripts/generate_rpc.py --check` | PASS | 0 |
| `poetry check --lock` | PASS，保留既有 Poetry metadata 弃用警告 | 0 |
| `git diff --check` | PASS | 0 |
| Python importlib 导入九个业务包下除 __main__/生成物外的全部 Python 模块 | PASS，378 模块 | 0 |
| `pytest -q -ra tests/storage_service/unit tests/storage_service/component` | PASS，95 unit + 11 component = 106 | 0 |
| `pytest -q tests/configuration_service/component/test_deployment_selection.py` | PASS，4；缺 Service/Instance/selection 拒绝且 revision 不推进，历史 ServiceBundle 精确版本保持 | 0 |
| `pytest -q -ra tests/storage_service/unit tests/storage_service/component tests/configuration_service tests/cli tests/contracts tests/core tests/collector tests/sink_service tests/commander -m 'not integration and not system and not hardware and not performance and not soak'` | 最终代码 SHA 上 1894 passed，11 skipped，514 deselected；含新增 Deployment 4 项 | 0 |
| `pytest -q -rs tests/cli -m 'not integration and not system and not hardware and not performance and not soak'` | 119 passed，11 skipped，126 deselected；用于核实前项 skip 原因 | 0 |
| `pytest -q -ra tests/configuration_service/integration` | PASS，39，无 skip | 0 |
| `pytest -q -ra tests/storage_service/integration tests/configuration_service/integration/test_storage_deployment_flow.py` | PASS，13，无 skip | 0 |
| `pytest -q tests/tooling` | PASS，130，含新增 Storage 分类测试 | 0 |
| main/融合 CLI 叶子集合比较 | PASS，85→94，缺失/重复均 0 | 0 |

真实集成的 13 项为 TDengine 7、Kafka 恢复 1、JSON v1 拒收 1、CLI 重启 1、Configuration 部署 3。TDengine 外层栈由 `r5_stack.up_stack/down_stack` 创建/销毁；Kafka/F6 用例按原 fixture 自管独立容器。F3 NATS 广播使用其既有正式 fixture 的 loopback 真实 NATS（4222），未使用 mock。没有清理或干扰既有用户容器。

首次 format 检查退出 1（一个 shipped YAML 测试格式），修复后为 0。编码期间主回归曾为 1895 passed；移除孤儿认证的 5 项专属测试、增加 Deployment 4 项后，针对最终代码提交重跑，实际为 1894 passed（exit=0），以最终轮为准。所有最终静态命令均实际执行，不把 skip 计入 PASS。

补充边界回归与真实服务结果：

| 命令 | 结果 | 退出码 |
|---|---|---|
| `pytest -q -ra tests/collector/integration tests/commander/integration tests/sink_service/integration/test_grpc_contract.py tests/sink_service/integration/test_config_repository.py tests/sink_service/integration/test_historical_point_table_client.py` | 162 passed，7 个清理告警；无 skip | 0 |
| `WINDHUB_NATS_SERVER_BIN=/tmp/wind-hub-r4g-bin/nats-server pytest -q -ra tests/sink_service/integration/test_reliable_durable_ack_real_nats.py tests/sink_service/integration/test_redis_batch_contract.py` | 7 failed、5 passed；其中 Redis 两项通过，NATS 三项通过/七项既有 fixture 失败 | 1 |
| detached main，`PYTHONPATH=<baseline>/src WINDHUB_NATS_SERVER_BIN=/tmp/wind-hub-r4g-bin/nats-server pytest -q -ra tests/sink_service/integration/test_reliable_durable_ack_real_nats.py` | 7 failed、3 passed，同一组失败、同一缺字段根因 | 1 |

命令输出与原始退出码保存在执行环境 `/tmp/storage-integration-*.log`；失败节点与核心 traceback 已在第 9 节持久记录，本报告不依赖临时文件才能理解失败。该额外 Sink NATS 验证为 FAIL / UNRELATED，不把它计作“门禁通过”。

## 7. F5/F6 能力回归

| 验收能力 | 结果与当前证据 |
|---|---|
| F5 单元/组件、崩溃恢复、分页、配置历史归档、授权及超时 | 95 unit + 11 component 通过；相关生产代码与来源逐字节一致 |
| F5 TDengine schema/tag/六标量/复合键/回放/丢失响应/旧 generation fencing/重开映射 | 7 个 TDengine 真实用例全部通过，包含 DDL 参数读取/漂移检测 |
| F5 Kafka commit timeout/replay/restart/rebalance | 真实 Kafka 定向用例通过 |
| F6-A T-63 CLI 重启激活 | 真实 Configuration 与 Storage 双进程，正式 CLI start/stop/start、target/active、RESTART_REQUIRED、1000→999 参数生效全部通过 |
| F6-B T-15 JSON v1 严格拒收 | 真实 Kafka+TDengine 中验证拒收、不提交、不入库，通过 |
| F6-C T-16 DDL 参数回读 | 真实 TDengine 3.3.6.13 回读及漂移检测通过 |
| F6-D T-44/T-59 等级契约 | main Sink 正式复数编码器与 Storage 解码器五项通过；RAW 支持在 Storage 侧独立验证，Sink 继续有意拒绝伪 RAW |

## 8. 正式 CI 纳入

- 不引入 `ci-storage-f1-f4.yml`。
- `ci_scope.py` 增加 Storage 产品目录及 integration-storage 目标，覆盖 Storage Proto、Configuration Proto、deployment 核心及 Storage 配置路径。
- `ci_gate.py` 注册可消费的 integration-storage（Storage integration + Configuration 部署流）；Fast 的现有按 marker 跨业务测试会包含 Storage unit/component，静态检查包含 Storage。
- `ci-pr.yml` 仅补充 Storage scope 分类的可执行断言，原协议/Sink 分类断言保持；ci-fast 原本全量编译 tooling，无需修改。
- 当前正式 ci-pr/ci-fast 是 main 既有 Lightweight 工作流，仅做工具检查，**不声称远端执行产品或真实中间件门禁**。本轮没有扩大成远端全产品 CI 架构；如需自动供应 Kafka/TDengine/NATS 并执行产品目标，应另经团队明确配置。130 项 tooling 测试验证分类/消费闭环。

## 9. 缺陷、限制与最终裁决事项

| 级别/条目 | 状态/相关性 | 证据及处置 |
|---|---|---|
| 新增 P0/P1/P2 | 未发现 | Storage 生产文件原样，静态/定向真实集成通过；不将此结论外推为全仓无缺陷 |
| P2：既有 Sink NATS fixture 陈旧 | FAIL / UNRELATED | main 与融合均同样 7 failed；`make_batch()` 缺少七字段 v3 所属批次必填 `device_type`、`point_table_id`，构造即 ValidationError；对应测试/契约/生产目录与 main 相同。本轮不删测试、不放宽断言、不恢复废弃契约 |
| P3：边界回归 NATS 异步清理告警 | UNKNOWN | 162 项断言通过，但产生 7 个 PytestUnraisableExceptionWarning（Subscription/Client flusher 在 loop 关闭后清理）；Commander/Sink 代码与 main 一致。未复现 main 的该组清理告警，不将其宣称已证明无关或已修复；作为非断言失败限制保留 |
| P3：既有 marker 未登记 | UNRELATED | 广泛回归出现 nats/jetstream 未知 marker 警告，保持 main 原状，不放宽检查 |
| P3：远端 CI 产品执行范围 | 交付限制 | 现有 Lightweight workflow 不供应真实中间件；本轮实际本地验证与远端 CI 分别记录 |

Sink NATS 两侧一致失败用例（原文件 `tests/sink_service/integration/test_reliable_durable_ack_real_nats.py`）：

```text
test_independent_ack_when_one_delivery_succeeds_and_other_unknowns
test_both_success_ack_independently_with_shared_pool
test_stop_and_restart_keeps_unacked_responsibility_distinct
test_subject_identity_mismatch_goes_to_dlq_not_zero_match_ack
test_legitimate_zero_match_safely_skips_without_dlq
test_confirmed_sample_evidence_is_per_delivery_and_broker_bound
test_failed_redelivery_keeps_evidence_empty_until_target_confirms
```

失败核心证据：`test_reliable_durable_ack_real_nats.py:310`，`ValidationError: 2 validation errors for SampleBatch`，`device_type: Field required`、`point_table_id: Field required`。在 detached `origin/main` worktree 以独立 `PYTHONPATH=<baseline>/src` 和相同真实 nats-server 二进制复现：7 failed、3 passed、exit=1；融合相同模块亦为 7 failed、3 passed，另两项真实 Redis 通过。baseline 第一次因未设置 PYTHONPATH 被仓库来源保护拒绝（exit=2），设置正确源码路径后才形成有效对比。临时基线 worktree 已正常移除。

- NOT_RUN：containerd 专属 CLI Runtime 11 项，本机无可用 `/tmp/wh-run/containerd.sock`，原有条件跳过；验收归属为 containerd 平台矩阵，进程模式 Storage CLI 已真实通过。不新增 skip/xfail。
- NOT_RUN：真实 Sink 进程→Kafka→Storage 完整在线 System E2E（原 T-14）；本轮只验收 main Sink 编码契约及 Storage 真实入库。RAW 全链路线仍由架构裁决处理，不自动把 engineering 映射为 protocol。
- NOT_RUN：硬件/性能/长稳、全仓所有系统 E2E、远端 Release Gate；本任务未请求资格测试，不宣称覆盖。
- 最终 main 合并：未授权，待 ChatGPT 审阅本报告及既有失败范围后裁决。所有可确定实施的融合项已完成，无架构争议项被擅自重构。

## 10. 相对 main 的完整变更文件

以下由 `git diff --name-only origin/main <验收产品代码HEAD>` 生成，含代码、配置、依赖、测试、来源报告及设计文档；本验收报告为后续独立文档提交。

```text
.github/workflows/ci-pr.yml
configs/services/cli.example.yaml
configs/services/service_storage-main_v1.1.0.yaml
configs/services/storage_bootstrap_v1.0.0.yaml
docs/audit/storage_f5_final_acceptance_20261122.md
docs/audit/storage_f6_c2_final_acceptance_20261122.md
docs/audit/storage_main_merge_preflight_20261122.md
docs/design/Storage_Service_软件设计_v1_4.md
poetry.lock
pyproject.toml
scripts/ci_gate.py
scripts/ci_scope.py
scripts/generate_rpc.py
src/cli/main.py
src/cli/runtime.py
src/cli/service_rpc.py
src/cli/storage_commands.py
src/configuration_service/application/ports.py
src/configuration_service/application/schema_catalog.py
src/configuration_service/application/service.py
src/configuration_service/infrastructure/adapters/inbound/grpc/repository_server.py
src/configuration_service/infrastructure/adapters/outbound/sqlite/repository.py
src/contracts/common.proto
src/contracts/common_pb2.py
src/contracts/common_pb2.pyi
src/contracts/configuration_repository.proto
src/contracts/configuration_repository_pb2.py
src/contracts/configuration_repository_pb2.pyi
src/contracts/configuration_repository_pb2_grpc.py
src/contracts/configuration_repository_pb2_grpc.pyi
src/contracts/storage_service.proto
src/contracts/storage_service_pb2.py
src/contracts/storage_service_pb2.pyi
src/contracts/storage_service_pb2_grpc.py
src/contracts/storage_service_pb2_grpc.pyi
src/core/configuration/__init__.py
src/core/configuration/deployment.py
src/core/configuration/publication_identity.py
src/storage_service/__init__.py
src/storage_service/application/__init__.py
src/storage_service/application/config.py
src/storage_service/application/ingestion.py
src/storage_service/application/metrics.py
src/storage_service/application/offsets.py
src/storage_service/application/query.py
src/storage_service/application/tasks.py
src/storage_service/domain/__init__.py
src/storage_service/domain/naming.py
src/storage_service/domain/sample.py
src/storage_service/domain/tasks.py
src/storage_service/infrastructure/__init__.py
src/storage_service/infrastructure/adapters/__init__.py
src/storage_service/infrastructure/adapters/inbound/__init__.py
src/storage_service/infrastructure/adapters/inbound/grpc_server.py
src/storage_service/infrastructure/adapters/outbound/__init__.py
src/storage_service/infrastructure/adapters/outbound/configuration_archive.py
src/storage_service/infrastructure/adapters/outbound/configuration_grpc.py
src/storage_service/infrastructure/adapters/outbound/configuration_snapshot.py
src/storage_service/infrastructure/adapters/outbound/configuration_watch.py
src/storage_service/infrastructure/adapters/outbound/kafka_consumer.py
src/storage_service/infrastructure/adapters/outbound/metadata_store.py
src/storage_service/infrastructure/adapters/outbound/tdengine_backend.py
src/storage_service/infrastructure/adapters/outbound/tdengine_backup.py
src/storage_service/infrastructure/adapters/outbound/tdengine_schema.py
src/storage_service/main.py
src/storage_service/settings.py
tests/cli/test_command_tree.py
tests/configuration_service/component/test_deployment_selection.py
tests/configuration_service/component/test_schema_regressions.py
tests/configuration_service/component/test_shipped_yaml_repository.py
tests/configuration_service/integration/test_storage_deployment_flow.py
tests/contracts/test_generation.py
tests/core/architecture/test_rpc_boundary.py
tests/storage_service/component/test_aggregate_pagination.py
tests/storage_service/component/test_grpc_common.py
tests/storage_service/component/test_kafka_ingestion_reliability.py
tests/storage_service/integration/test_cli_restart_activation.py
tests/storage_service/integration/test_kafka_json_versions.py
tests/storage_service/integration/test_kafka_real.py
tests/storage_service/integration/test_tdengine_real.py
tests/storage_service/unit/test_configuration_archive.py
tests/storage_service/unit/test_configuration_grpc.py
tests/storage_service/unit/test_configuration_reconciliation.py
tests/storage_service/unit/test_device_binding.py
tests/storage_service/unit/test_kafka_json_contract.py
tests/storage_service/unit/test_kafka_retry.py
tests/storage_service/unit/test_partition_offsets.py
tests/storage_service/unit/test_sink_wire_contract.py
tests/storage_service/unit/test_storage_crash_faults.py
tests/storage_service/unit/test_tdengine_backup.py
tests/storage_service/unit/test_tdengine_decode.py
tests/storage_service/unit/test_tdengine_timeouts.py
tests/tooling/test_ci_scope_targets.py
```

## 11. 交付状态

- 验收代码提交 `ab38110b9d6376805784ed51c640f13ac9400e1c` 已 push 到融合分支，并经 `git ls-remote` 确认本地/远端一致。
- push 后远端 main 仍为 `8eaa3ef842bf49d9a54e45c965ef3e24c11fa89d`，来源 Storage 仍为 `d1d936822abab45083555d48030f5d5d0a23860e`，均未被修改。
- 产品提交后工作树只剩本验收报告；报告单独提交及 push，最终交付 HEAD 和干净工作树状态随交付摘要提供。报告提交不改变已验收产品代码。
- 已删除本次用于对比的 detached main worktree；本次创建的真实服务栈按 fixture 清理，未全局 prune 或操作历史用户容器。
- 停止在最终裁决点，未创建 PR、未 merge/squash main。
