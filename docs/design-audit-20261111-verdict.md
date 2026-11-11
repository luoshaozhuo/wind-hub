# wind-hub 十份设计与源码契约审查裁决（2026-11-11）

## 1. 范围与基线

- 目标分支：`audit/design-contract-consistency-20261111`；审查基线：`main@567e30ffb1aa1c1a519c0156991ca22811f389cf`。
- 设计：`docs/design` 十份当前版本；Server/Admin 内部设计排除。
- 源码：六份 `src/contracts/*.proto`、`src/contracts/sample_batch.py`、Configuration Loader 和版本模板、四份 Service YAML、Collector/Commander/Gateway/Data Generator 共享配置调用方、Gateway gRPC 授权与租约、CLI Gateway 更新、Sink Reliable/Realtime Runtime。
- 方法：全文逐行章节/版本扫描、Proto service/message 静态提取与 45 RPC 对照、Schema/API 字段提取、关键调用路径源代码逐段人工校验。
- 报告：`docs/design-audit-20261111-crossrefs.md`、`docs/design-audit-20261111-proto.md`、`docs/design-audit-20261111-configuration-api.md`。

## 2. 已确认的静态结果

| 项 | 结果 | 证据 |
|---|---|---|
| 十份全文检查 | 10/10，24,504 行 | 交叉引用扫描报告 |
| 代码围栏成对闭合 | 10/10 | 全文围栏扫描 |
| 六 Proto RPC 总数 | 45 | `src/contracts/*.proto` |
| Commander | 7 RPC，15 消息，58 字段 | `src/contracts/commander.proto` |
| 相关模块的 RPC 名称在设计中出现 | 无缺漏 | Proto 对照报告 |
| Collector/Commander/Gateway/Data Generator `config_sources` 实际字段 | 与当前设计中明确列出的直接引用相符 | 四份 `configs/services/*.yaml` |
| `SharedConfigPaths` | Python dataclass 为 `instances/templates/site_file/devices_file/load_site/coordination_root`，并非 YAML 原型 | `src/configuration/shared_documents.py` |
| 批量输入 | `SampleBatch.schema_version=2`，身份/时间/质量/样本集合字段与契约表述一致 | `src/contracts/sample_batch.py` |

**注意：** RPC 名称覆盖只是存在性校验；不能代替字段语义、实际 Servicer、Client、运行时权限和消息 ACK 的闭环验收。

## 3. 已确认的不一致

### P1-01 Sink Reliable：多 Delivery 全局 ACK 与独立 Durable 实现冲突

- 设计：`docs/design/Sink_Service_软件设计_v1_49.md` §10.6 新增约束称“匹配多个 Delivery 时，源消息 ACK 是它们全部的聚合责任”“所有匹配 Delivery 的全部目标责任终结”才 ACK；Contracts v1_19 §10.6.4 也以源消息全量责任表述。
- 源码：`src/sink_service/infrastructure/reliable_runtime.py` `_durable_id` 按 `deployment_id:delivery_id` 生成 Durable；`start()` 为每个 Delivery 独立 `pull_subscribe`；`_run()` 仅对本 `_definition.delivery_id` 的 `_deliver_batch()` 结果执行 `message.ack_sync()`（源码约 L184～287、L666～744）。
- 影响：多个 Delivery 在 JetStream 中是**不同 Consumer 的独立 ACK 责任**，不存在代码定义的跨 Durable 统一 ACK。当前设计的全局 ACK 门禁不能直接按该实现验收。
- 建议：将设计统一为“每 Delivery Durable 独立 ACK；该 Delivery 的匹配点全部结算后 ACK；多个 Durable 对同一 Stream 源消息各自保留责任”。如果确实要求全局 ACK，则需新增跨 Consumer 原子协调（不建议，超出当前简化架构），并先决策再改代码。
- 验收：同源批命中两 Delivery，其中一成功、一失败/UNKNOWN，确认前者只 ACK 自己的 Consumer，后者保持未 ACK；分别检查重投、DLQ、Metrics 和多实例 Durable 隔离。

### P1-02 CLI Gateway 更新操作参数与设计不一致

- 设计：`docs/design/CLI_软件设计_v1_25.md` §10.7 等处要求 `gateway update GATEWAY_ID --values PATH --yes`，其中 PATH 为 JSON 数组。
- 实现：`src/cli/plant_commands.py` L172～179：`gateway_update(gateway_id, updates: list[str] = typer.Argument(...), owner_id: str = typer.Option(..., "--owner-id"), ...)`。L190～195 用多个 `variable_id=typed_value` 作为位置参数，**没有 `--values PATH` 选项**。
- 影响：用户按文档操作会得到参数错误，相关 CLI System Test 的设计输入与真实命令不一致。
- 建议：明确选定一种唯一接口。最小变更方案是改 CLI 设计与验收场景使其与已经实现的 Typer 接口一致；若产品决定 JSON 文件输入，则应先修改命令接口、测试和帮助，不能两套并存。

### P1-03 Gateway ClaimTargets 授权主体与自由 `owner_id` 的绑定未落实

- 设计：Gateway v1_26 §9、CLI v1_25 §9 明确未授权普通 CLI 不得冒用 Generation owner；要求服务端真实身份授权。
- 源码：`src/industrial_gateway/infrastructure/grpc/data_server.py` L28～47 `_authorize(...,"inject")` 根据 mTLS CN 权限或者开发环境 loopback 准入；L296～313 `ClaimTargets` 直接传入客户端自报 `request.owner_id`。租约状态核对 owner 字符串与 token（`src/industrial_gateway/infrastructure/variable_leases.py`），但未在所审查的 RPC 边界发现认证主体与 `owner_id` 的强绑定。
- 裁决：这是**认证-租约业务身份绑定缺口风险**，不是“所有用户都可无权限注入”的证明：生产 mTLS 仍有 `inject` ACL。若 `inject` 包含 CLI 等管理主体，其可自报 Generation owner，可能不符合设计。
- 建议：明确区分 Generation 与被授权运维主体，建立服务端对 `owner_id` 的可信映射或限定值域；增加越权、冒名、同名抢租等负面测试。开发 loopback 无 TLS 权限仅限封闭开发环境。

### P2-01 Configuration 文档错引章节

- `docs/design/Configuration_软件设计_v1_14.md` L907：引用不存在的 `§14.5`。当前第 14 章只有 §14.1、§14.2；应改为真实字段/测试子节，不可凭猜测修复为其他段号。

### P2-02 Collector 文档错引章节

- `docs/design/Collector_软件设计_v1_19.md` L3922：引用不存在的 `§12.4`；实际第 12 章只有 §12.1～§12.3。需按指标定义的真正权威位置重定向。

### P3-01 Contracts 历史版本文字

- `docs/design/Contracts_软件设计_v1_19.md` L1519 提及 CLI v1_4、v1_6，属于历史迁移说明而非现行依赖，建议移除以降低误用风险。

## 4. 排除的误报

- Commander 中 `Contracts v1_19 第 12.2.1 节` 是合法跨文档引用，而非 Commander 缺失章节。
- Configuration、Collector、Commander、Gateway、Data Generator 所引绘图规范的 §7.1/§8.1 属外部规范章节。
- Data Generator 引《软件设计模板_v1_1》§14.2，属于外部模板引用；未取回该模板原文，暂列未验证。
- 已确认 Gateway v1 首版仅要求 Modbus TCP Server，IEC104/ADS 为后续适配，不能以 `GetCapabilities.protocols=["modbus_tcp"]` 判定与当前目标冲突。

## 5. 尚未达到的全面验收门禁

以下工作**未执行或未覆盖完整范围**，不得表述为通过：
1. 六 Proto 的逐字段标签/presence/类型与所有生成 `pb2/pb2_grpc/pyi` 的机器级 Descriptor diff。
2. 全仓所有 RPC Caller、Servicer、配置消费者和公开 API 调用点的穷举静态分析；当前只抽取了确定的主要路径。
3. Sink 所有 Adapter 的真实交付语义、每种错误的 ACK/NAK 与进程崩溃恢复组合测试。
4. Configuration 的多进程单写、跨文件事务恢复、所有源码配置合成路径。
5. Ruff Lint、Ruff Format、项目 Mypy/Pyright、import-linter、pytest、独立 Claude 验收及物理 PLC/NATS 联调。
6. 外部《软件设计模板》和绘图规范原文逐节核对。

## 6. 当前裁决与修改范围

**裁决：FAIL（设计冻结门禁）**，因 P1-01/P1-02 已证实与当前源码行为矛盾，P1-03 需要安全策略澄清并复测。此结果不等于生产代码整体 FAIL。

本分支只提交只读审查证据，未改变已冻结的 Proto、Core、Configuration、业务源码或十份设计正文；建议先裁决 P1，随后递增受影响的设计版本并清理旧版，最后运行独立集中复测。
