# wind-hub 全模块最终联测与冻结前审查（2026-11-23）

## 1. 基线与审查边界

- 主分支：`origin/main@f91068748838103f547f1140d24b4eb93cbf7372`（本轮 GitHub 复核）。
- 设计分支：`docs/design-consolidation-20261123`。
- 总体架构：`wind-hub_总体逻辑架构设计_v1_27.md`，冻结且本轮未修改。
- 最新跨模块测试设计：`wind-hub_跨模块测试设计_v0_41.md`。
- 纳入 Contracts、Core、Configuration、CLI、Commander、Collector、Data Generator、Industrial Gateway、Sink、Storage 及全部跨模块接口；暂排除 Web Server/Admin。
- 本次为远程文件级源码/设计/测试的**静态准入审查**，未执行 ruff/mypy/pytest、真实 Broker/TDengine、CLI 进程 E2E 或容量实验。所有未执行项为 NOT_RUN，不将现有测试源码等同已执行且通过。

## 2. 准入裁决

**NOT_READY_TO_FREEZE / P1 阻断未关闭。**

系统设计目标已经明确：DG→Gateway→Collector→NATS/JetStream→Sink→Kafka→Storage→TDengine；并行的 Realtime Gateway/Modbus/Redis 出口分别验收。最终裁决必须以真实 CLI 驱动的跨服务进程和独立 SQL/Storage RPC 结果为证据，不能以 Kafka ACK、Publisher ACK 或测试代码构造的 StorageSample 替代。

## 3. 可复核的主要风险与缺口

| ID | 优先级 | 证据位置（main） | 问题/影响 | 关闭要求 |
|---|---|---|---|---|
| F-01 | P1 | `tests/system/e2e/full_chain/conftest.py` 开头与 FullChainHarness；`test_e2e_smoke.py` | Fixture 仍描述六进程、四 Sink 出口，Docker 栈为 NATS/Redis/Kafka；尚未把 Configuration/Storage 作为完整进程链的独立参与者和 TDengine 作为最终目标。当前 smoke 主要核对首批物理值；不能证明持久化 E2E。 | 从正式 CLI 启动 Configuration、Gateway、DG、双 Collector、Commander、Sink、Storage，基础设施单独启动 NATS/JetStream/Redis/Kafka/TDengine，执行三等级最终入库及独立 SQL 回读。 |
| F-02 | P1 | `tests/system/e2e/full_chain/test_e2e_surface_matrix.py`、`test_e2e_semantic_matrix.py` | 强断言 51 个 RPC、73 CLI、六个 Common 部署视图；这是 Storage 集成前的固定分母。仅 StorageService 就有 9 专属 RPC。历史 51/73 成功不能宣称全模块 100%。 | 运行时枚举正式 Proto Descriptor/CLI --help，按服务接口逐项生成动态分母、执行与状态；包含 Storage 9 RPC 和所有 Common RPC。 |
| F-03 | P1/产品语义裁决 | `src/sink_service/infrastructure/adapters/outbound/kafka_wire.py` L28–30；`src/sink_service/application/config.py` L414–418；`tests/storage_service/unit/test_sink_wire_contract.py` | 现行 Sink 拒绝 RAW，理由是没有未解码协议载荷；Storage 能解析一个人工构造的 RAW JSON 不等于生产链具备 RAW。冻结总体 §3.1 和 §5.3.4 对 RAW 定义亦互相矛盾。 | 明确 RAW/PROTOCOL 可执行语义；若最终冻结必须三等级全链生产支持，则需扩展真正来源和线格式并测试；否则须获批准的明确范围豁免，仅声明 PROTOCOL/BUSINESS 全链准出，不得宣称 RAW PASS。 |
| F-04 | P1 | `src/cli/configuration_commands.py` 仍注册 `config push/apply`；`src/cli/snapshot_commands.py` 实际调用 Common 的 Push/Apply；Commander/Collector/Gateway/Sink 文档旧快照写入表格仍存在 | 业务配置统一 Deployment 发布，与旧服务本地快照写入两条写入途径的边界未获得全服务服务端的实证闭合；仅修改文档“不得使用”不足以保障不会旁路更改业务配置。 | 逐一真实调用各服务的 Get/Push/Apply，证明集中业务配置不可绕过 Repository 写入、鉴权 fail-closed；有必要时同步修订生产实现与设计、全部调用方/Fixture。 |
| F-05 | P1/验收依赖 | `tests/storage_service/integration/test_tdengine_real.py`、`test_kafka_json_versions.py`；Storage v1_6 §14 | 存在真实 TDengine 3.3.6.13/schema/幂等/故障测试，部分测试在无真实 Endpoint、凭证或 Docker 时 skip；分段测试并不证明完整 DG→TDengine 链，且缺独立长期运行的最新证据。 | 在目标环境执行并记录数据库真实版本、DDL/Tag/类型回读、Kafka offset、异常恢复及最终逐键对账，不允许用 skip 计 PASS。 |
| F-06 | P1/容量裁决 | `tests/sink_service/system/test_capacity_r14_sustained.py`；主分支 C7 merge commit 明确记录 Sink R14 limitation | 最终吞吐需按实际持续速率测量；R14 限制尚未由本轮独立复测关闭，单模块合成容量不能替代完整 Storage 落库容量。 | 在独立性能层复测 Sink Realtime/Reliable、Storage/TDengine；报告 30×10×500=150,000 点/s 的单等级理想目标与实际最大稳定吞吐、Lag、CPU/RSS/P99。未达标须如实给出优化后的参数与限制，不可以测试未运行宣称通过。 |
| F-07 | P2 | `src/storage_service/infrastructure/adapters/inbound/grpc_server.py` L340–387 | Storage Get/Push/ApplyConfigSnapshot 等多个 Common RPC 明确授权后返回 UNIMPLEMENTED；合同存在与功能实现不是同一含义。 | 为每个 Common RPC 形成受支持/显式不支持能力表；测试对应规范性预期；若总体要求所有 Common 必须具备功能，则必须设计和代码整改。 |
| F-08 | P2 | 多个模块 v1_4x～v1_7x 设计正文仍含本地 Get/Push/Apply 文件提交/生效流程、历史 Contracts 版本与早期阶段性现状 | 文档虽然换版，但长篇正文仍存在与当前集中部署/数据等级不一致的描述；用前置“以新基线为准”不等于清理矛盾。特别应复核 Commander §9/§11、Collector §9/§11、Gateway §9/§11、Sink §9/§11。 | 逐条归类规范/历史：删除过期规范，保留合法本地引导操作；改动后同步所有测试、CLI、配置、RPC 调用方。 |
| F-09 | P2 | `tests/system/e2e/full_chain/conftest.py` 仍有证书/mTLS 历史注释及单机双轨派生点表；新总体设计 §5.3.2 内部简化无 TLS | 测试证书、受控令牌与 Gateway 专用双入口身份边界可能混淆，导致用旧安全方式通过或跳过；测试专用派生配置要证明与正式 Deployment/PointTable 身份一致。 | 明确管理面与注入面，按现行安全配置和网络边界分别做令牌/租约/拒绝/隔离测试；禁止匿名或环境回退冒充成功。 |

## 4. 模块级逻辑与测试充分性矩阵

| 模块 | 已看到的正面证据 | 最终联测仍需验证 | 设计与测试状态 |
|---|---|---|---|
| Contracts | `tests/src/unit/test_contracts/test_sample_batch.py` 覆盖 v3、非法 JSON、批次身份、TTL、数值边界等 | 全部 Proto 与生成物一致；Sink Kafka v2/Storage v2 严格互通；历史身份和非法字段端到端拒绝 | 部分覆盖，需正式 Descriptor 全量矩阵 |
| Core | PointTable/BusinessPoint 与共享转换设计的界线已定义 | 点表语义一一映射、类型和单位、协议换算只在正确模块调用；多版本历史闭包 | 单模块结果未重新执行，NOT_RUN |
| Configuration | Service/PointTable 精确闭包 RPC 已存在；Storage 单元测试有 Deployment 及固定历史束检查 | 全目录原子发布、CAS、广播丢失与乱序、服务离线不阻塞、目标与活动版本、历史不漂移 | 设计路线明确，跨模块真实联测待补 |
| CLI | `src/cli/storage_commands.py` 有 Storage 查询、聚合、导出、备份、统计和重试入口 | 所有当前叶子命令均真实执行/合法拒绝，按角色的状态码和副作用；移除固定 73 分母 | 旧六模块覆盖不能直接复用 |
| Commander | 正式 Proto 8 专属 RPC；已有控制安全设计与分段测试 | 真实控制链的时序、UNKNOWN、断连、SQLite 恢复、独立隔离证明、跨重启幂等、批指令命令号 | 安全高风险链必须独立复测 |
| Collector | `src/collector/application/batch_builder.py` 封装 v3 原始协议解码值、固定 point_table_id | 双通道采样一致性、故障时 PubAck 边界、队列溢出、配置切换、按设备身份来源可追踪 | 数据入口具备基础代码证据，E2E 不充分 |
| Data Generator | 既有 full-chain smoke 覆盖真实物理输出起始点，录制 Golden Data | WTG/PV/BESS/GRID/PPC 确定性控制与数据真值、CSV/随机重放、真值落盘持久性 | 当前 E2E 场景覆盖需按 v0_38 扩充 |
| Industrial Gateway | Proto 10 专属 RPC；现有 Gateway 租约/变量/控制规则 | 管理口与 Sink 注入口鉴权、Lease fencing、Modbus 读写事件、隔离与失效重放 | 独立权限链需实测 |
| Sink Service | `tests/sink_service/unit/test_kafka_wire_parity.py` 验证 PROTOCOL/BUSINESS 严格编码和拒绝假 RAW；Kafka v2 编码 | 四个目标真实交付、三进程生命周期、可靠 ACK、故障重投、RAW 范围与 R14 容量 | RAW 不可用，完整链路及性能有门禁 |
| Storage Service | `tests/storage_service/integration/test_tdengine_real.py` 真实 TDengine schema/幂等/UNKNOWN 测试，`test_sink_wire_contract.py` 覆盖两级 Sink 互通 | CLI 启动、Kafka→TDengine 逐键最终查询、RAW/PROTOCOL/BUSINESS、Offset、Rebalance、查询/导出/备份、长稳 | 分段基础较好，但不是全链 E2E |
| 跨模块测试设计 v0_38 | 已定义 OUT-05、ST-E2E-01～08、E2E-11/12、Golden Data 三级对账、真实 CLI | 当前 `tests/system/e2e/full_chain` 尚未同步实施；测试分母和环境仍旧 | 设计覆盖目标比实际测试更全 |

**表中“已有测试代码”不代表运行通过**。所有目标环境执行状态需要新独立测试报告。

## 5. 最终联测测试门禁

### G0 基线与设计冻结前置

1. Fetch 最新 main，固定工作分支及测试 HEAD，检查工作树/已提交文件差异；单一版本设计文件及引用一致。
2. 自动枚举全部 `src/contracts/*.proto`、生成 Stub、实际 Service 注册、CLI `--help` 与运行实现。逐个标记 SUPPORTED/UNIMPLEMENTED/DEPRECATED/BLOCKED，并形成 **RPC×服务、CLI×服务矩阵**，禁止旧 51/73 固定分母。
3. 先关闭 F-03 RAW 语义/范围裁决与 F-04 配置发布旁路验证；否则进入全链测试只能标阻断，不得最终冻结。

### G1 单模块/接口

- 实际执行 generate_rpc --check、ruff check/format、mypy src、lint-imports、pytest 收集与所有模块单元/组件/集成检查；对每项记录命令、退出码、Passed/Failed/Skipped/Errors/NOT_RUN。
- 单模块覆盖测试设计的每条 UR/UC/ST，确保正常、非法输入、超时、异常、恢复、取消/重启、权限和资源限额；无证据不计覆盖。

### G2 真实基础设施 Integration

- NATS Core/JetStream、Kafka、Redis、TDengine 由测试管理器分别启动与清理；Configuration/Gateway/DG/Collector/Commander/Sink/Storage 经 CLI 进程管理。
- 必含 Storage 实库、DDL/KEEP/Precision、Kafka JSON v2 消费、冲突、Offset、历史闭包、正式 RPC 与独立 SQL。
- 测试过程中不可重用生产配置/数据库、不可破坏非测试资源。外部组件版本与配置哈希归档。

### G3 真正完整 E2E

- 执行 v0_38 E2E-01～12、OUT-01～05、ST-E2E-01～08 与全部 CLI/RPC 矩阵，正常/拒绝/恢复都按独立证据判断。
- 每条要能从 DG JSONL→Gateway→Collector SampleBatch v3→Sink Kafka JSON v2→Storage QuerySamples→TDengine SQL 完整定位。
- PROTOCOL/BUSINESS 全链逐值及时间/质量核验；RAW 不具备生产来源不得 PASS，须由产品语义裁决决定是否为冻结阻断。
- 重新发布/回滚 Deployment 后重放旧 PointTable，不用最新推荐版本覆盖；重复、异值冲突与重启前后结果确定。

### G4 性能、故障与长稳

- 基础负载、30×10×500 理想单等级负载（150,000 点/s）及三等级受支持情况下的独立极限负载；R14 回归、Kafka lag、Storage Offset、水位、CPU/RSS、P99 与唯一记录数。
- TDengine 写后 ACK 丢失、Kafka Broker 中断、Consumer Rebalance、Storage/Collector/Sink/Commander 各自重启、Gateway 租约失效、Configuration 广播丢失，验证收敛与不越权。
- 目标达不到时记录最大稳定容量及限制，禁止在运行时自动降低验收门槛。

### G5 独立复测与冻结

- Claude 作为独立测试/复测方，对同一固定 HEAD 出具运行命令和原始证据，发现测试缺陷可修改测试/Fixture 小范围修复，生产问题交 ChatGPT 统一整改。
- ChatGPT 完成生产修复后再次提交，Claude 在最终 HEAD 重测全部受影响模块及 G0～G4；禁止拿旧 SHA 的 PASS 代替新 HEAD 验收。
- 无 P0/P1/P2 设计冲突及可重复软件缺陷，所有必需模块/接口/CLI/E2E 证据真实执行、无未解释失败；环境缺失的关键项目不得标 READY_TO_FREEZE。性能未达到理想值需明示“批准的限制及生产承载上限”，不得作功能全通过的替代。
- 冻结交付包含固定 SHA、唯一设计/测试书版本、配置与 Proto 快照、测试覆盖矩阵、运行原始日志、性能边界、剩余例外批准记录。**不自动创建 PR、不自动合并 main**。

## 6. 立即后续动作

1. 在现有设计分支保留本审查报告；不修改冻结总体设计。
2. 先正式裁决 F-03，验证 F-04，完善现行 CommonService 能力表，然后修复其他设计文档的冲突表格与旧状态。
3. 更新/新增 `tests/system/e2e/full_chain` 实例链路，将 Storage/TDengine 纳入最终验收与动态 CLI/Proto 分母；不得把新增测试自身宣称成已运行成功。
4. 完成集中测试与代码整改、独立复测后，再申请最终冻结裁决。

**本报告是准入审查，不是验收报告。** 本轮没有通过运行性检查、不能宣称全部设计和测试充分。

## 7. 配置全集静态审查与 E2E 隔离整改（2026-11-23）

**实测边界**：已读取远程 `configs/` Git Tree 及相应 YAML 正文，现有树共 **72 份 YAML、1 份 CSV**；其中 19 份属于新建 E2E 专用配置。检查了文件名与 file_version、Service/Template ID、三个 Deployment 的选择、E2E Service→Instance 引用、全部 PointTable 的 BusinessPoint/Units 存在性与显式身份。检查仅为源码静态分析；未运行完整 PyYAML 严格 Loader、Configuration Candidate/Publish 或真实 CLI 启动。

**已实施：** 创建 `deployment_e2e-final_v1.0.0.yaml`，选择 9 个 E2E Service、6 类 Instance；独立的双 Collector (100 ms)、Generator (TI=0 + Recording)、Commander、源/目标 Gateway、Sink、Storage、CLI；所有新服务以 `service_<info>_v1.0.0.yaml` 命名，Instance 保持 `<type>_<info>_v<semver>.yaml`。源仿真 Modbus 端口固定隔离到 15221～15225、Sink Gateway 目标 15121，独立证书目录、SQLite/TDengine 元数据、Kafka Topic `windhub.e2e.history`、Redis namespace 和 CLI 进程目录。Configuration 与 Storage 引导文件留在 `deploy/e2e/`，不作为业务 YAML 发布。E2E 的正常基线 Delivery 版本 `v1.0.1`，未选用的草稿 `v1.0.0` 已删除；业务/RAW 不以伪造数据冒充已支持。当前生产 Deployment 文件未被修改。

**逐项结果：**

| 检查项 | 状态 | 证据/限制 |
|---|---|---|
| 72 份文件的文件名、顶层 file_version 与模板/服务身份 | 静态检查基本通过 | 采用 GitHub 文件正文抽查/批量规则核对；非完整 Schema 执行 |
| E2E 9 Service→6 Instance 精确目标 | 静态匹配 | 9 个 Service ID 与文件名对应，Service 引用的 Instance 均指向 e2e-final |
| 9 个 PointTable 的业务 ID 与单位存在性 | 静态检查通过 | BusinessPoint 与 Units 标准目录存在；但存在严重重复语义 |
| GW2MW ADS 的 BusinessPoint 唯一性 | **P1 FAIL** | 111 Point 中 109 Point 涉及 22 组重复 BusinessPoint；见 `point_table_ads_wtg_gw2mw_duplicate_business_points_20261123.csv` |
| prod-a 完整采集→Storage 部署目标 | **P1 FAIL** | `deployment_prod-a_v1.3.0.yaml` 未选择 `storage-main`；E2E 独立配置不替代生产升级 |
| Sink BUSINESS 生产闭包 | **P1 BLOCKED** | 必须取得 Repository 的真实历史 manifest_SHA256 后生成下一版候选，严禁伪造；实验 Template 已提供但未默认激活 |
| Sink RAW 生产闭包 | **P1 BLOCKED** | 当前 Kafka 编码器明确拒绝无原始未解码载荷 RAW；不能以 Storage 接受合成 RAW 推定全链通过 |
| 内部安全与证书/令牌 | 需真实联测 | 新 E2E 使用隔离证书路径；现行 Gateway/Commander/Generator 部分运行配置仍要求 mTLS，与冻结总体无 mTLS 原则须明确例外或代码修订 |
| 生产文件头版本号 | **P2** | `device_farm-a_v1.4.0.yaml` 注释仍写 `v1.3.0`；不能直接修改已发布版本的内容哈希 |
| kafka_history v1.0.0 / v1.1.0 | 保留两版 | 两版本均有现存 Sink 单元/组件测试引用，不能误判为无用版本；后续清理由调用方/实际发布闭包共同决定 |
| 配置审查脚本 | **P2** | `scripts/audit_business_points.py` 仍使用不带版本的 standard 目录路径和旧 `devices*` 文件名判定，无法按当前 `device_*` 目录完成权威全集验收；须修订脚本及其测试 |
| 真实 Repository 全量校验/发布、进程、E2E | **NOT_RUN** | 本轮没有可执行目标仓库/依赖环境，不能宣称最终配置冻结通过 |

**冻结门禁**：E2E 执行前先以已选版本构造临时独立 `configs/` 根目录，按官方 CLI 发布，并检验所有依赖闭包、统一状态和 SQL 终点。生产冻结前必须解决 GW2MW PointTable 的物理语义冲突，另行升级 `prod-a` 的 Storage 版本选择并验证，关闭 RAW 范围裁决以及业务点真实 manifest；即使 E2E 基线 PROTOCOL 成功，也不得将整体状态标 READY_TO_FREEZE。

## 8. 配置审查工具整改（同日后续提交）

本轮生产工具修订 `scripts/audit_business_points.py`，保留所有 PointTable 物理映射，不触碰生产 YAML 及冻结总体设计：

- 修复 `business_points_standard_vX.Y.Z.yaml` 和 `units_standard_vX.Y.Z.yaml` 的版本化定位；历史不带版本的固定路径不再作为现行合法入口。
- 修复 `device_<info>_vX.Y.Z.yaml` 实例识别；检查 Device→DeviceModel→PointTable/Protocol 精确版本。
- 遍历全部 Repository YAML，对照文件名、`file_version`、`service_id`、`template_id` 和 `deployment_id`，明确排除本地专用 `storage_bootstrap_vX.Y.Z.yaml`。
- 校验 Deployment→Service→Instance 精确引用和 Sink/Delivery Instance→Template、Delivery→Sink、Delivery→Device 身份引用。缺失或引用错配直接 fail-closed。
- 同步更新 `tests/tooling/test_audit_business_points.py` 为版本化 Fixture，并添加 Deployment 正常闭包、未选择 Instance、缺失 Service/Template、Delivery 错目标和文件身份/版本错误回归用例。
- 采用每行不超过 100 字符的静态检查整理新增代码；**没有执行 Ruff、mypy、pytest 或真实配置发布**，门禁状态为 NOT_RUN，代码变更仅代表已提交待复测。

独立复测的最低指令（在具备项目 Python 环境的实际仓库执行；用 CLI 或原命令行均可触发纯离线审查，不启动业务服务）：

```bash
ruff check scripts/audit_business_points.py tests/tooling/test_audit_business_points.py
ruff format --check scripts/audit_business_points.py tests/tooling/test_audit_business_points.py
pytest -q tests/tooling/test_audit_business_points.py
python -m scripts.audit_business_points --root configs --output /tmp/wind-hub-config-audit.csv --strict
```

最后一条按当前 GW2MW 的已知重复业务点问题**预期非零退出**；必须保存完整 CSV 和实际统计，不能通过取消 `--strict` 将 P1 假报关闭。如果此前静态审查判断有误，以实际严格 Loader、Schema 和逐 Point 结果重新定级。

**延续阻断**：GW2MW 物理语义 22 组重复、RAW 正式数据来源、BUSINESS 历史 manifest 与 E2E Storage 真实入库、prod-a Storage Deployment 选择。配置代码检查通过也不意味着可直接冻结。

## 9. GW2MW PointTable 规则与实际 Core 源码核查

Core v1_42 §业务语义明确要求同一继承展开后的 PointTable 内，`business_point_id` 不得重复；不同物理位置、叶片编号、统计窗口与设定/反馈含义必须属于不同的精确业务身份。现行 GW2MW ADS 点表中，111 个 Point 仅有 24 个不同 BusinessPoint ID，22 个重复组涉及 109 个 Point（统计仅作物理语义审定候选，必须查验地址和 OEM 定义再修订）。

`main@f9106874` 的 `src/core/configuration/shared_point_tables.py` 已要求每 Point 显式 `business_point_id`，不存在默认使用 `point_id` 的旧回退；但 `src/core/configuration/validation.py` 在逐点校验单位与业务引用时没有同一 PointTable 的 BusinessPoint 唯一性断言。后续必须与 PointTable 语义整改同步补上运行时检查及对应正/反例；不能仅依赖离线审查脚本来保护正式配置发布。由于旧生产点表仍违反新契约，本轮暂不做会立即拒绝现有生产配置的无配套代码变更。

本轮 `scripts/audit_business_points.py` 的静态审查实现已经修正并提交；是否与当前 Core/Configuration 真正的加载、发布及 Schema 全部一致，仍需独立在目标环境执行，并记录实际命令、退出码与完整发现 CSV。

## 10. 240 点静态单位维度交叉核验与配置隔离补充

按 `business_points_standard_v1.4.0.yaml` 与 `units_standard_v1.2.0.yaml` 的 `unit → quantity` 显式定义，对 `configs/templates/point_tables/` 中现存 9 个模板、240 个 Point 的 `business_point_id`、源单位以及标准单位进行静态关联：

- **240/240 Point 的业务 ID、源单位和业务标准单位均能找到有效目录定义**。
- **源单位 quantity 与标准业务单位 quantity 的不一致数量为 0**。
- 对标准业务数据类型为布尔/文本的点，静态检查非恒等 scale/offset 的异常为 **0**。
- 上述仅证明引用存在及基础量纲，不证明 OEM 点的物理名称准确、每个点的标度正确、不同统计窗口语义、地址字节序、类型量化与 22 组业务身份重复已解决。GW2MW P1 原裁决不变。

进一步将 `service_commander-e2e_v1.0.0.yaml` 的 gRPC 监听从继承的 `0.0.0.0` 收窄为 `127.0.0.1`，避免本机最终联测的管理口被无意暴露。当前只有静态配置证据，仍需在真实 CLI/Socket 中检查所有 E2E 监听端口无生产冲突、凭证身份正确及服务 READY。

以上为 GitHub 文件级脚本化静态核验，**没有执行远程 Python、真实 Schema Loader 或服务启动**，结果不作为准入 PASS 的替代。

## 11. BusinessPoint 设计及 configs 候选整改（2026-11-23）

本轮按用户明确的 **PointTable 内 Point 与 BusinessPoint 一对一** 完成设计与候选配置文件修订：不修改生产模块代码，不执行 Ruff、pytest、Repository 发布或联测；未发布候选不得作为生产活动版。涉及的 10 份设计文档已相应换版，旧设计文件已清理；冻结总体逻辑架构文件保持原样。

**文件链**：

| 类型 | 旧版本 | 新候选版本 |
|---|---|---|
| BusinessPoint Standard | `business_points_standard_v1.4.0.yaml` | `business_points_standard_v1.5.0.yaml` |
| GW2MW ADS PointTable | `point_table_ads_wtg_gw2mw_v1.1.0.yaml` | `point_table_ads_wtg_gw2mw_v1.2.0.yaml` |
| GW2MW DeviceModel | `device_model_wtg_gw2mw_v1.4.0.yaml` | `device_model_wtg_gw2mw_v1.5.0.yaml` |
| farm-a Device Instance | `device_farm-a_v1.4.0.yaml` | `device_farm-a_v1.5.0.yaml` |
| prod-a Deployment | `deployment_prod-a_v1.3.0.yaml` | `deployment_prod-a_v1.4.0.yaml`（未激活） |

**静态文件级校核**：原 293 个 BusinessPoint 的 ID、数据类型和标准单位未改变；新增 107 个新候选定义，共 400 个。原 111 个 ADS Point 的 Point ID、非业务映射字段（包括地址/符号、类型、单位）未改变，候选中的 111 个 `business_point_id` 两两不同；全部目标 ID 均在候选目录中存在，数据类型和标准单位相较原映射未变化。原 22 组重复业务身份的**结构性冲突已在新候选消除**，但物理解释并未获 OEM 核定。映射台账：`docs/audit/gw2mw_businesspoint_semantic_mapping_candidate_20261123.csv`，全部 111 条为 `PENDING_OEM_SEMANTICS`，不得宣称 BUSINESS 生产准入已通过。

新 PointTable 使用 `config_sources` 精确固定 BusinessPoint 标准目录 1.5.0 与 Units 1.2.0，新标准目录固定 Units 1.2.0。新 DeviceModel/Instance/Deployment 依此逐级精确引用。新 `device_farm-a_v1.5.0.yaml` 的旧文件头错误已修正。

**遗留硬约束**：原 v1.4.0 标准目录仍被 `src/data_generator/infrastructure/config/service_settings.py`、`src/collector/infrastructure/config/snapshot_repository.py` 及现有测试显式引用；历史版本及相关生产配置尚不能简单删除，否则违反同步调用方和历史闭包约束。Git `configs/` 中暂保留新旧标准文件，因此**当前全目录不能直接作为无歧义的正式 Configuration 发布根**：旧 PointTable 未显式固定标准目录，存在多版本歧义。后续编码阶段必须统一变更固定路径加载方和审查脚本，给旧历史闭包准确归档，构造受控的无歧义完整发布根后才能正式发布。该项不能用“设计文件已更新”代替程序准入。生产旧 Deployment 1.3.0 不变。

仍开放：OEM 实际 PLC 符号语义及运行时唯一性验证、RAW 的真实线协议载荷支持、BUSINESS 的真实不可变 manifest、prod-a 的 Storage 选择以及全链 CLI/TDengine 和容量实测。本轮验证状态：**静态结构审阅；Ruff、mypy、pytest、发布、E2E 均 NOT_RUN**。


## 12. 设计/配置阶段收口与 Claude 独立复核交接（2026-11-23）

**本次阶段目标**：将设计与配置资产交给 Claude 进行只读专项复核。软件代码审查、模块测试、联测、生产运行时闭包改造与最终生产冻结另立专题。上文历史发现和旧基线描述保留为审计证据，不代表本次已实测通过。

- GitHub 设计分支：`docs/design-consolidation-20261123`；继续使用该分支，不创建 PR、不合并 main。
- 当前权威跨模块测试设计：`docs/design/wind-hub_跨模块测试设计_v0_41.md`，其中 §15.1 为本轮唯一复测范围。冻结总体设计 `wind-hub_总体逻辑架构设计_v1_27.md` 未修改。
- 已补充 `configs/instances/deliveries_prod-a_v1.4.0.yaml`，使 `deployment_prod-a_v1.4.0.yaml` 所选择的 Delivery 精确版本在仓库内存在。该配置行为沿用 v1.3.0 的 Delivery 条目，仅新候选版本号、修订身份及说明发生变化；不意味生产激活。
- 对仓库 `configs/deployments/` 四份 Deployment 的 Service/Instance 文件目标进行了 GitHub 目录级静态检查：补充缺失版本前仅识别上述一个缺口；补充后仍需 Claude 在真实仓库独立复核 YAML 解析、逻辑闭包、Schema 规则和所有模板引用。
- BusinessPoint 新目录 1.5.0、GW2MW PointTable 1.2.0、DeviceModel 1.5.0、Device Instance 1.5.0、prod-a Deployment 1.4.0 均是**未批准的候选**。必须保留 111 条 `PENDING_OEM_SEMANTICS`，不能把映射单射性视为物理语义批准。
- 历史 PointTable 旧版本未普遍明确绑定原 BusinessPoint 目录，和候选新标准目录的多版本共存仍须作为专项复核的重要发现。不能通过默认选最新或修改历史 YAML 绕过。
- 当前生产 `prod-a` 未提供完整 Storage Service 选择，只有隔离 `e2e-final` 涵盖 Storage。生产级完整链路仍 BLOCKED。
- 已存在的 Core/Configuration 生产源码修改不在本次 Claude 复测范围；不得推断它们已经通过静态/组件/系统验证。

**本次执行状态**：GitHub 文件读取、提交、目录与四份 Deployment 文件路径检查已完成；Claude 独立复核 `NOT_RUN`；全部 Python Schema/Loader、Ruff/mypy/pytest、业务进程、Broker、TDengine、容量与真实数据链路均 `NOT_RUN`。

**交付裁决**：`READY_FOR_CLAUDE_DESIGN_CONFIG_REVIEW`，而非 `READY_TO_FREEZE` 或 `READY_FOR_PRODUCTION`。若 Claude 发现设计/配置 P1/P2，先回交 ChatGPT 处理文件级缺陷；与生产代码有关的 P1/P2 进入独立代码审查台账，不在本次擅改源码。

## 13. P0 裁决与候选配置闭包收口（2026-11-23）

### 13.1 冻结总体设计的受控语义勘误

总体设计 v1_27 继续保持原字节，不改其冻结正文。由于 §5.3.4 同时把 RAW 解释为“已解码标量”“解码前载荷”和“不是原始字节”，本节明确**废止其中不一致的旧解释**，作为本轮统一设计修订的裁决文本，供设计冻结评审批准：

- RAW：仅指带来源、边界、时刻和字节编码证据的**协议解码前字节/帧**；现行 Collector 仅持有协议解码标量，不能提供 RAW 时必须报告 NOT_SUPPORTED。不能从已解码数值虚构字节、标示 RAW 或默认选择其它等级。
- PROTOCOL：协议字序、标量类型等解码完成，尚未按 PointTable 的 scale/offset 或 Units 换算的来源标量。Collector 发布 SampleBatch v3，其源数据等级为 PROTOCOL。
- BUSINESS：Sink 在精确且不可变的历史 PointTable、BusinessPoint、Units 闭包下，一次性执行比例/偏移、工程单位至标准单位换算及明确业务身份映射。PointTable 的已展开 Point→BusinessPoint 映射为单射；未经 OEM 审定的身份不可生产激活。
- ENGINEERING 不是交付等级；不再默认 BUSINESS。Core 内部函数参数 `raw` 不定义对外 RAW 类型。
- 业务配置唯一写入路径是 CLI 全量目录候选发布→Configuration Repository 原子保存→NATS 广播提示→各模块自行读取 Deployment/Service 固定闭包并安全生效；不存在业务服务的第二套 Push/Apply 发布权威。
- RPC 字段以 `src/contracts/*.proto` 当前存在的字段号为准；`GetServiceConfigBundleRequest.revision_id=0` 明确表示当前已发布 revision，不能误用其他请求的 presence 语义。

上述勘误**不修改总体原文**，评审时须明确批准其对冲突段落的替代效力；未批准前，总体设计的文字冲突仍需登记为设计冻结 P0，不得直接裁定 PASS。

### 13.2 配置版本迁移和活动根边界

本轮新建不覆盖的候选版本链：

| 部署域 | 新候选 Deployment | Device Instance | 业务标准目录 | 额外条件 |
|---|---|---|---|---|
| prod-a | 1.5.0 | farm-a@1.6.0 | BP@1.5.0 / Units@1.2.0 | GW@1.2.0、TZ@1.2.0 PointTable；Storage-main@1.2.0、Sink-prod-a@1.3.0、Delivery-prod-a@1.5.0 |
| sim-a | 1.2.0 | sim@1.3.0 | BP@1.5.0 / Units@1.2.0 | 5 个物理仿真 Model@1.2.0、对应 PointTable@1.2.0；CLI-sim@1.3.0 |
| e2e-final | 1.0.1 | e2e-final@1.0.1 | BP@1.5.0 / Units@1.2.0 | 5 个物理仿真 Model@1.2.0、对应 PointTable@1.2.0；Storage/四出口目标保持 E2E 隔离 |

保留全部旧版 YAML，以历史对象身份和原始 Git 字节追溯；旧 PointTable 未显式固定 BP/Units 的历史解释仍需证明其当时的发布闭包，不能靠新候选版本重新解释旧样本。**整个仓库 configs/ 目录不是可直接整体发布的活动根**：含多个同 deployment_id 的不同版本、未固定旧点表及不同语义候选。正式 CLI 发布必须从已选 Deployment 精确闭包构造独立、无歧义、完整的临时根目录，并经实际 Repository 校验；本阶段仅交付候选源，未发布、未运行。

prod-a 候选中 Kafka Sink 模板 `kafka_history@1.1.0` 与 Storage-main@1.2.0 使用一致的 Topic `windhub-history` 和示例 Broker `192.168.1.20:9092`；该地址为未实测现场候选，不代表实际 Kafka 服务可达或认证准入。Source collector_id 采用所选 Service ID 的候选假设，须在后续代码/运行时专题核验真实消息头；Gateway STOP 目标的租约和权限未完成，不得启用。

### 13.3 本轮延期事项

生产代码未作进一步整改：Core/Configuration 历史闭包多版本读取实现、hard-coded BP@1.5.0 严格规则、全部 Service/CLI 的实际载入路径、TLS 旧配置、真实 Kafka/Storage 对接与 Storage 幂等实现均属于单独的软件审查专题。新候选设计与 YAML 必须由 Claude 只读审查确认引用闭包，文档/配置通过不代表这些代码已通过。

RAW 真实协议字节、BUSINESS 历史 Manifest 与 OEM 的 111 个点审核均未满足生产运行门禁。文档/配置阶段可以申请结构冻结，**生产激活仍为 BLOCKED**。

## 14. 独立可验证的候选配置发布根（2026-11-23）

为了不更改历史已提交 `configs/`，且避免同一 deployment_id 多个版本出现在单次全量 Repository 发布，新增以下静态隔离根：

- `deploy/config_roots/prod-a/`：29 份版本化 YAML，唯一 prod-a@1.5.0。
- `deploy/config_roots/sim-a/`：23 份版本化 YAML，唯一 sim-a@1.2.0。
- `deploy/config_roots/e2e-final/`：35 份版本化 YAML，唯一 e2e-final@1.0.1。

三根使用同一源 YAML Git Blob（内容 SHA 未改变）构造；根中不包含旧未固定 PointTable、第二版同身份 Deployment 或不属于该闭包的历史文件。以各根为 `config publish --root` 目标时仅代表**准备了候选输入**，不代表已执行 Candidate、实际 Repository 发布或业务进程启动。

仍需独立验证 Source file_version/配置身份、完整图引用、重复键、单位、类型和 Core 真实 Loader；本次 GitHub 上完成的是文件选择与复制，不是 Python YAML/Schema 执行。所有生产启用项仍受 OEM、BUSINESS Manifest、RAW 真实来源和现场端点鉴权限制。

## 15. 第二轮 FAIL 后的集中设计及候选配置整改（2026-11-23）

基线：Claude 第二轮独立报告 `docs/audit/design_config_freeze_retest_round2_20261123.md`，42 项中 19 CLOSED、18 OPEN、3 等待批准、1 延期代码审查、1 NEEDS_OEM，新发现 N-01 P1/N-02 P2/N-03 P2。本节仅记录已提交的整改和 GitHub 静态核对，不将其冒充第三轮独立 PASS。

### 15.1 版本化配置与不可变性

- **N-01**：恢复原 `configs/templates/sinks/sink_kafka_history_v1.1.0.yaml` 的 Git Blob `460fb5d96235b4a6713c85af6102efbde9f85f78`；Topic/并发变化放到新 `kafka_history@1.2.0`。同时新建 `sinks_prod-a@1.4.0`、`deployment_prod-a@1.6.0` 并更新独立 prod-a 活动根。未更改已有发布对象字节。
- **N-03/C-06**：新建 `cli-sim@1.4.0`，仅使用隔离 `53060/53051/53052/53073/53061` 管理端点和 `/var/lib/wind-hub/sim-a` 进程、日志目录；移除 CLI TLS profile。Gateway-sim@1.1.4、Commander-sim@1.1.0、Generator-sim@1.0.4、Device-sim@1.4.0、Scenario-sim@1.0.4 及 `deploy/sim-a/configuration_bootstrap_sim-a_v1.0.0.yaml` 同步选版。此为**新设计候选**；security.token_sources 等运行时 Schema 和服务实际支持情况须在后续代码专题核验，不能因文件存在就声明真正具备无 TLS 安全链。
- **C-07**：建立 `docs/audit/config_version_release_ledger_20261123.csv`，记录 112 个源配置的当前 Blob、基线 Blob、候选/历史状态。基线 78 份文件的真实“是否曾生产发布”没有环境清单证据，保持 UNKNOWN；34 份新增文件标为 UNPUBLISHED_CANDIDATE。只有可证明发布登记后才能赋予不可变正式发布状态。

### 15.2 设计正文集中消歧

按前轮 D-05/D-08/D-09/D-11/D-14/D-16/D-20～D-25 证据位置集中更正原业务规则、接口表、流程图和测试描述：本地 Common Push/Apply 不再具有 Repository 托管业务配置写入权限；Service→Instance→Template 精确版本；Collector SampleBatch v3；CLI 统一 `commander batch`/`commander recover-device`；Gateway 10、Generator 9 个专属 RPC；状态 `target_revision`(4)、`active_revision`(5)；协议总量由完整 Descriptor 动态统计。上一轮报告指出的旧正文须再次由 Claude 逐行独立核验，不能仅以本节的修订声明关闭问题。

12 份设计文档只保留各模块唯一最新版；总体架构 v1_27 的 Blob `891486c006c5f964ffa2e61e950104102004f9ab` 保持原样。当前最新版：CLI v1_55、Collector v1_43、Commander v1_59、Configuration v1_22、Contracts v1_44、Core v1_45、Data Generator v1_62、Gateway v1_52、Sink v1_75、Storage v1_9、跨模块测试 v0_43。

### 15.3 候选根文件级校验与未执行项

独立根：`deploy/config_roots/prod-a/`（29 YAML，唯一 prod-a@1.6.0）、`sim-a/`（23，唯一 sim-a@1.3.0）、`e2e-final/`（35，唯一 e2e-final@1.0.1）。GitHub Tree Blob 检查：87/87 与对应 `configs/` 原始字节一致，各根仅有一个 Deployment 版本；prod-a 的 Kafka 模板已升级、sim-a 管理端口互不复用生产。文件级结果不替代 YAML 严格解析、Schema/Domain Loader、Repository Candidate/Publish 及实际业务安全控制测试；这些本轮仍 **NOT_RUN**。

**不可提前关闭**：总体 v1_27 RAW/旧 ENGINEERING/通用 Bundle 正文冲突的勘误尚待有权批准（D-01/D-04/D-06）；C-08 Gateway 注入授权未验收，C-10 软件硬编码规则待代码专题，C-11 E2E 物理基础设施共享仍需隔离运行，C-12 历史注释保留并备案，C-13 111 点 OEM 未批准，C-14 RAW 真值和 BUSINESS 获批 Manifest 未具备，历史原始发布证据仍缺。实际生产激活依旧 BLOCKED。

**本轮交付裁决**：`READY_FOR_ROUND3_DESIGN_CONFIG_RETEST`；不能宣布 `DESIGN_CONFIG_REVIEW=PASS`、`DESIGN_FREEZE_APPROVED` 或 `PRODUCTION_ACTIVATION=READY`。

#> 第三轮独立复测后事实更新（2026-11-23）：§15.1 的 112 份台账、§15.3 的 sim-a@1.3.0 均为当时的历史审查记录，不能视为当前活动版本。第三轮复测时版本化源 YAML 及台账为 **117/117**，活动根为 prod-a@1.6.0、sim-a@1.4.0、e2e-final@1.0.1；以第三轮报告和该轮证据为历史基线。此后的本分支设计收口又新增两份不可变候选，当前源台账为 **119 项**，sim-a 活动候选升级为 **1.5.0**（Generator Service 1.2.0，移除 gateway_tls）；新状态尚待独立复测。此前的历史审查结果不被追溯改写。\n\n## 15.4 sim-a Schema v2 后继勘误及台账刷新

发现 sim-a 第一代后继 Service 中使用了未纳入当前 Configuration Schema 的 `token_sources`，因此未将其覆盖为同版本修订，而是创建 Gateway-sim@1.2.0、Commander-sim@1.2.0、Data Generator-sim@1.1.0（`schema_version:2`，通过 `security.callers[].token_env` 声明身份），以及 CLI-sim@1.5.0、sim-a Deployment@1.4.0。旧候选保留在源 `configs/`，独立活动根只选择最新版。后续代码专题必须核验各模块运行时解析器是否接受该 Schema，实际受控令牌权限是否执行，未经验证不能宣称可投产。

`config_version_release_ledger_20261123.csv` 随新候选重新生成，其 Git Blob 是客观字节证据；历史是否曾发布仍保持 UNKNOWN，不把该台账等同生产批准记录。


## 16. 第一阶段设计与配置收口进度（2026-11-23）

> 本节是开发分支上的阶段性设计审查记录，不替代第三轮独立复测或正式冻结批准。历史记录保留原有基线。

### 16.1 已提交的定向修订

- Collector：将遗留本地 Candidate/Push/Apply 文件提交事务从规范性配置应用流程中移除，改为 Repository 精确闭包查询和设备级独立生效；同步清理 UR、业务触发及一部分测试描述。
- Commander：Service 业务依赖不再以本地 pathname、模板根选择；区分引导路径和 Repository 精确业务闭包，校正 Common 快照和 Repository 发布 CAS 的所有权。
- Sink：正式修订仅由 Configuration Repository 提交，Main 仅调和 Worker；修正本地 Push/Apply 测试期待和 require_dedup 证据门禁。
- Industrial Gateway：10 个专属 + 9 个 Common；只读快照、旧写入口拒绝；管理令牌和业务角色不由本轮放宽。
- Data Generator：9 个专属 + 9 个 Common；目标变量身份严格使用所选 Gateway PointTable 的 point_id；允许一源多目标（包括同一 Gateway），但每个 `(gateway_id,variable_id)` 只允许一个来源。该约束与 PointTable 内 Point→BusinessPoint 一对一独立。
- Storage：现有 Common target_revision（4）/active_revision（5）为正式字段；CLI/Configuration 文档引用当前 Contracts v1_44。
- sim-a 新增不可变 Service data-generator-sim@1.2.0 和 Deployment sim-a@1.5.0，移除 gateway_tls，活动根切换为此后继版本，旧源 YAML 不变。新增两项台账后为 119 项；对应根文件 Blob 已静态匹配。
- 跨模块测试设计 §15.5 已明确本分支最新根、映射规则、静态和运行时独立门禁。

### 16.2 不能提前关闭的边界

| 门禁/问题 | 第一阶段处理决议 |
|---|---|
| D-01 / D-04 / D-06 | 总体 v1_27 字节保持冻结。§13.1 勘误已给出明确替代语义；未经授权批准仍为 DESIGN_FREEZE_APPROVAL_PENDING。 |
| N-04 | sim 三项 Schema v2 与第一方解析器不兼容，归下一阶段生产代码及测试整改；不得仅放宽解析器绕过令牌鉴权。 |
| N-05 | e2e 的 simulation_targets 与 grpc.sink_listener_port 尚未纳入已注册 Schema；Configuration 设计 §9.1.1 已规定后继证书身份 Service Schema v3，须代码阶段实现、创建新 YAML/Deployment 版本及复测。不得原地修改历史 Schema 或宣称当前 35/35 PASS。 |
| C-10 | 现行代码硬编码标准业务目录版本，留待独立代码审查；本轮禁止更改 src。 |
| C-08/C-11/C-13/C-14 | 实际 Listener 安全、现场基础设施/OEM、RAW/BUSINESS 数据及容量依赖真实执行证据，独立保留生产激活阻断。 |
| 第三轮原有静态问题 | 本轮仅作定向修订，尚未执行相同分母的严格第四轮审查；不能把文档编辑等同 CLOSED。 |

### 16.3 当前结论

- DESIGN_CONFIG_REVIEW = PENDING_RETEST（不是 PASS）
- DESIGN_FREEZE_APPROVAL = DESIGN_FREEZE_APPROVAL_PENDING
- PRODUCTION_ACTIVATION = BLOCKED
- SOFTWARE_TESTS = NOT_RUN
- 本阶段所有 GitHub 文件写入仅在 feat/production-readiness-final-20261123，不更改 main、src 或 tests。
- 报告模板.md 在仓库未能定位；本节沿用已有审计文档结构，模板合规性尚未验证。

### 16.4 第一阶段设计书正式换版（2026-11-23）

本分支规范性内容整改后，九份受影响的设计书已**新建后继版本并删除原版工作文件**；历史文档/验收记录中的旧版本名仍仅作为当时事实保存。总体设计 v1_27、Core v1_45 不变；Contracts 后续因 D-05 发现规范性冲突单独升级至 v1_45（见 §16.5）。

| 模块 | 当前唯一工作文件 |
|---|---|
| CLI | `docs/design/CLI_软件设计_v1_56.md` |
| Collector | `docs/design/Collector_软件设计_v1_44.md` |
| Commander | `docs/design/Commander_软件设计_v1_60.md` |
| Configuration | `docs/design/Configuration_Service_软件设计_v1_23.md` |
| Contracts | `docs/design/Contracts_软件设计_v1_45.md` |
| Core | `docs/design/Core_软件设计_v1_45.md` |
| Data Generator | `docs/design/Data_Generator_软件设计_v1_63.md` |
| Industrial Gateway | `docs/design/industrial_gateway_软件设计_v1_53.md` |
| Sink Service | `docs/design/Sink_Service_软件设计_v1_76.md` |
| Storage Service | `docs/design/Storage_Service_软件设计_v1_10.md` |
| 跨模块测试 | `docs/design/wind-hub_跨模块测试设计_v0_44.md` |
| 总体架构 | `docs/design/wind-hub_总体逻辑架构设计_v1_27.md` |

**冻结前最终待核查**：上述文档自身及交叉引用、三套根唯一 Deployment、119 份版本台账、Source→Root Blob 恒等、sim-a Listener/Token/状态目录隔离、e2e Schema v1/v3 迁移策略；不能因文档换版宣称校验脚本已 PASS。旧报告中引用已删除工作版本的链接属于历史证据定位问题，不能把旧版本当作现行权威。D-01/D-04/D-06 总体勘误仍待有权批准。

**阶段判定**：DESIGN_CONFIG_REVIEW=PENDING_RETEST；DESIGN_FREEZE_APPROVAL=PENDING；PRODUCTION_ACTIVATION=BLOCKED；SOFTWARE_TESTS=NOT_RUN。

### 16.5 Contracts 写入权威勘误（2026-11-23）

进一步对照冻结审查 D-05，发现原 Contracts v1_44 §4.3 仍将 Common 三项快照 RPC 描述为各服务的独立候选提交/业务生效机制，和 §4.1 以及模块设计的 Repository 唯一发布路径矛盾。已升级唯一现行工作文件为 `docs/design/Contracts_软件设计_v1_45.md`，删除 v1_44 工作文件；保留原 Proto 方法与历史字段号，只从契约语义上禁止业务模块第二套写入。CLI、Commander、Configuration、Data Generator、Gateway、Storage、跨模块测试的现行契约引用已对应更新；Collector/Sink 按现行 Proto 及 Repository 准则实施。

此修订是**设计语义修订而非生产代码修改**。正式冻结前须对 CommonService 全体实现和测试做后续独立代码审查。第三轮报告中的 Contracts v1_44 属于历史审查证据，不将过去的结论反向修改。D-05 需下一轮设计复测后才能 CLOSED。


### 16.6 ChatGPT 自主配置整改与静态核对（2026-11-23）

本节由 ChatGPT 负责设计及配置文件整改，不把设计复测移交 Claude。Claude 在后续软件阶段仅负责独立测试、测试代码/Fixture 和小范围修复。

**安全架构定案**：Configuration 现行设计 §9.1.1 已将 Service Schema v3 明确为基于令牌 Schema v2 的严格新增版本，不继承 v1 证书式身份。业务模块间 TLS/mTLS 不再进入新候选。Gateway 的管理与 Sink 注入 Listener 仅允许 loopback、应用令牌、独立权限隔离；真实验证前仍为 STOP。

**E2E 新候选（不修改历史文件）**：

| 对象 | 新版本 | 设计变化 |
|---|---|---|
| Commander Service | 1.1.0 / Schema 3 | 令牌调用方与仿真目标 token_env |
| Gateway source/target Service | 各 1.1.0 / Schema 3 | 移除 mTLS，明确控制/注入令牌，target Sink Listener 为独立受控口 |
| Data Generator Service | 1.1.0 / Schema 2 | 移除 gateway_tls 和证书身份，声明双向令牌 |
| CLI Service | 1.1.0 / Schema 1 | 移除 TLS Profile，进程文件参数引用新 Service |
| Sink Instance | 1.0.1 / Schema 1 | Gateway 目标 credential_ref 使用专用环境变量 |
| Deployment | e2e-final@1.0.2 | 精确选中上列新版本 |

E2E 独立根保持 35 份 YAML，旧七个被选版本仅从独立活动根移出，仍保留在 configs/ 供不可变历史回溯。prod-a 仍选 1.6.0（29 YAML），sim-a 仍选 1.5.0（23 YAML）。源 YAML 台账增至 **126** 项，新增候选全部为 `UNPUBLISHED_CANDIDATE`。

**已实际执行的静态核对**（GitHub 读取源和独立根）：E2E 七个新配置的 Source/Root Git Blob 为 7/7 一致、file_version/schema_version 与文件名/预定版本 7/7 对应、历史 TLS 结构化字段 0 个、七个源 Blob 在台账均存在；Gateway→Commander、Commander→Gateway、Gateway→Generator、Generator→Gateway、Sink→Gateway 的令牌变量双端对应 **5/5**。sim-a 两个新增候选此前验证 Blob 一致，Generator 无 gateway_tls。台账 126 行且无重复 path。

**严格限制**：上述是已选新配置的**定向文件静态验证**，不是 YAML 全根解析、Configuration Repository v3 校验、Core Loader、运行时/CLI 启动或端到端测试。尝试通过 GitHub Connector 批量完整逐条读取 126 个源文件时触发工具单次调用数上限，未获取 126/126 完整重新校验结果，不能宣称通过。先前独立第三轮 117/117 台账结果只对其当时 HEAD 有效。

**剩余软件实现门禁**：N-04 三模块 v2 运行时拒绝；N-05 新 e2e v3 Schema 未注册（当前发布将被拒绝）；C-10 业务目录硬编码；Gateway Sink Listener 令牌权限与测试真实性；全部由第二阶段代码及测试解决。D-01/D-04/D-06 的总体 v1_27 规范性勘误仍维持用户批准门禁，未改动原总体设计。

**阶段状态**：第一阶段文件级设计收口已交付候选；`DESIGN_CONFIG_REVIEW=PASS_WITH_LIMITATIONS` 仅针对本次定向整改的文件级自审，**不等于**全仓正式冻结；`DESIGN_FREEZE_APPROVAL=PENDING`；`SOFTWARE_TESTS=NOT_RUN`；`PRODUCTION_ACTIVATION=BLOCKED`。


### 16.7 ChatGPT 配置台账及独立根逐文件哈希复核（2026-11-23）

**核验输入**：第三轮独立复测 `root-source-hash-manifest.csv` 所列 87 个逻辑配置根路径、当前 `config_version_release_ledger_20261123.csv` 的 128 项源 Git Blob SHA、当前 GitHub 开发分支文件内容。当前分支源配置较第三轮新增 11 个不可变候选（sim-a 4、e2e-final 7）；未覆盖原有 configs/ 已登记 YAML。

**已执行结果**：

| 校验 | 实际结果 | 方法与边界 |
|---|---|---|
| 版本台账源文件 | **128/128 PASS** | 分批逐一从当前分支读取全部 128 个 `configs/` 路径，与台账记录的 Git Blob SHA 精确比较；无读失败或不一致。先于最后新增的 sim CLI/Deployment 两文件完成 126/126 检查；两新增文件在版本更新后另行读取并与活动根相等。 |
| 独立根与源 Blob | **87/87 PASS** | 以前次 87 项 Manifest 的路径为基数，按 11 项候选替换对应的 11 个活动选择（e2e 7、sim 4）；逐项读取 `deploy/config_roots/{deployment}/{relative_path}` 的 Git Blob SHA，与当前台账所列源 SHA 比较；末次 sim 的两条候选路径随后独立验证一致，旧活动根路径确认不存在。 |
| 不可变选版链 | **定向 PASS** | prod-a@1.6.0、sim-a@1.6.0、e2e-final@1.0.2；sim CLI@1.6.0 与 Generator@1.2.0 绑定一致，e2e CLI 的进程配置与 Deployment 所选 Service 版本无冲突。 |
| 跨服务令牌配置 | **5/5 静态 PASS** | Gateway→Commander、Commander→Gateway、Gateway→Generator、Generator→Gateway、Sink→Gateway 双端环境变量引用相同；实际授权、令牌是否存在、真正出站元数据的构造仍待代码和进程验证。 |

**新发现并修复（设计/配置）**：sim-a@1.5.0 已选 Generator@1.2.0，但 CLI-sim@1.5.0 的启动 argv 仍硬编码 `service_data-generator-sim_v1.1.0.yaml`。新增不可变 CLI-sim@1.6.0、Deployment sim-a@1.6.0 并同步活动根和台账，不改历史源 Blob；CLI/Deployment 版本配对的静态核查通过。

**严格区分未运行项目**：未在本工具环境中执行完整 YAML 解析、Configuration Repository Schema 校验、Core Domain Snapshot、业务运行时解析器、任何服务启动/测试。已知 e2e v3 尚无正式 Schema 注册，真实 validate_only 不可能 PASS；sim-a v2 一方解析器仍不兼容。因此本表的 Blob 一致性不能替代配置语义或运行时准入。

**阶段自审裁决**：静态版本台账与三个配置根的源 Blob 一致性 **PASS**；设计文件及测试设计中配置写入权威、数据等级和控制安全条款已完成本轮候选整改，但总体 v1_27 的 D-01、D-04、D-06 有权批准仍 **PENDING**。设计冻结仍 **PENDING**；代码阶段实施需保留该正式批准门禁，不能将候选审核等同全面生产就绪。


### 16.8 第二阶段 C1：Configuration Schema v3 与 src/tests 清理专题（2026-11-23）

**职责与范围**：ChatGPT 负责生产代码修改、设计偏差修复以及废弃/冗余代码清理；Claude 后续负责独立软件测试、测试代码与 Fixture 维护及小范围修复。固定分支 `feat/production-readiness-final-20261123`，不涉及 Web Server/Admin，不新建 PR，不合并 main。

**本轮已提交生产实现**：

- `src/configuration_service/application/schema_catalog.py`：保留 v1/v2 定义不变，注册独立 Service Schema v3；v3 使用 token_env 调用方、受控 Gateway `grpc.sink_listener_port`，并声明 Commander 的严格动态仿真目标对象。新候选禁用历史证书/TLS 字段和第二个 `grpc.permissions` 权威。
- `src/configuration_service/application/service.py`：递归验证带 schema 的动态字典值；此前只有 `properties` 才进行嵌套验证，无法限制 `simulation_targets.<device_id>` 对象，现增加规则对象验证且保留旧布尔型 `additionalProperties` 语义。
- `tests/configuration_service/component/test_schema_regressions.py`：新增 v1/v2 不可变性、v3 真正选版候选、目标必填/未知字段/类型和重复 gRPC 权限源的负例；测试专用自注册 Schema 不再错误抢占正式 v3 的注册位，将原自由版本槽迁至 v5。所有测试仍待执行，不能宣称 PASS。

**新的未发布精确配置候选**：Gateway-source/target@1.1.1、CLI@1.1.1、E2E Deployment@1.0.3，单独消除早期 v3 YAML 内重复的 `grpc.permissions` 配置；旧文件保留在源配置历史，活动 E2E 根只选后继版本。台账从 128 增加至 **132** 项；三套活动根数量保持 29/23/35，总计 87。针对实际新候选的 Source/Root Blob 和全量台账应重新验证。

**正式纳入后续生产代码整改及冗余清理的闭环矩阵**：

| 专题 | 生产代码 | 关联测试 | 删除/保留约束 |
|---|---|---|---|
| CFG-01 v3/无重复权限源 | Configuration Schema Catalog/Schema 验证 | `tests/configuration_service/` | 已编写首轮实现和测试，待测试执行后确认；不得覆写历史 v1/v2 |
| CFG-02 Commander Schema v2/v3 | `src/commander/infrastructure/config/service_settings.py` 与模拟 Gateway RPC 调用方 | `tests/commander/`、`tests/system/rpc/` | 移除证书型运行时分支前先替换鉴权调用、凭据装配和测试；不能只删除证书字段而失去认证 |
| CFG-03 Gateway Schema v2/v3 | `src/industrial_gateway/infrastructure/config/schema.py`、鉴权及双监听适配器 | `tests/industrial_gateway/`、跨模块 E2E | Sink/管理口必须令牌互斥且仅受控监听；删除旧证书代码后确保无调用者/Fixture 残留 |
| CFG-04 Generator Schema v2 与出站调用 | `src/data_generator/infrastructure/config/service_settings.py`、`gateway_tls.py`、Gateway RPC 适配器及 main | `tests/data_generator/integration/test_gateway_writer_mtls.py` 等 | 原 mTLS 测试与代码在令牌 RPC 链路可证明之前不能单纯删掉，迁移后集中移除 |
| CFG-05 退役业务 Push/Apply | Collector/Commander/Sink/Gateway/DG/Storage 的 Common RPC Adapter、CLI 旧快照写入命令 | `tests/cli/test_snapshot_e2e.py`、`tests/sink_service/component/test_config_snapshot.py`、跨模块 Fixture | 保持正式 Proto 9 RPC 注册，业务写入必须 fail-closed；迁移调用方、测试与注释后删除第二套业务配置写入实现 |
| CFG-06 硬编码目录及过期生成物 | `src/data_generator/infrastructure/config/service_settings.py`、其他 Core/Configuration 选版调用 | Configuration/DG/Collector/Sink 组件和 E2E | 移除 BusinessPoint/Units 固定版本路径前须由 Repository 精确依赖闭包替换；生成 Proto 仅用统一生成器再生并执行一致性检查 |

**验收规则**：清理以“证明没有真实调用者、配置引用、RPC 契约或测试依赖”为前提；不为兼容旧实现保留额外分支；每次改变接口/Schema 同步所有调用方、Fixtures、配置文件和测试，未经真实执行不标记 CLOSED。下一工作次序 CFG-01 实测 → CFG-02/03/04 生产整改 → CFG-05/06 安全清理 → 全仓静态门禁及跨模块 E2E。

**本次测试执行状态：NOT_RUN**。当前执行环境无法直接在项目实际工作树中运行 Ruff、mypy、pytest、完整 132 源 YAML 和 87 根 YAML 解析；已实现的代码需 Claude 后续独立执行测试，也由 ChatGPT 在实现阶段持续进行可执行的局部检查。设计冻结批准 D-01/D-04/D-06 仍为 PENDING，不通过编码替代批准。


### 16.9 第二阶段 C2：Commander 令牌化与废弃代码迁移（2026-11-23）

**提交状态：集中开发中，非验收通过。** 本节覆盖冻结设计中的 Commander 内部通信取消 mTLS、E2E 令牌链路及废弃测试迁移；仅影响固定分支，不修改 main 或 Web Server/Admin。

本轮实际修改：

- `src/commander/infrastructure/config/service_settings.py`：Service v2/v3 令牌 Schema、严格 loopback 监听、调用方角色及仿真目标环境引用校验，删除此解析路径对证书 CN/密钥路径的依赖。
- `src/commander/application/settings.py`：仿真 Gateway Target 使用 `token_env`，不再存储内部 mTLS 证书参数。
- `src/commander/infrastructure/adapters/inbound/grpc_transport.py` 与 `grpc/server.py`：仅在 loopback 绑定内部 gRPC，启动时读取环境令牌，拒绝缺失、重用、非安全 ASCII 或不足 32 字符的令牌；每个 RPC 从单个 Authorization Bearer 字段在恒定时间对比后做调用方权限和查询隔离。可信恢复外部证明方的独立证书/隔离证据要求不变。
- `src/commander/infrastructure/adapters/outbound/gateway.py`：删除旧的内部证书通道，改用 loopback + 原有唯一 command_id/双端认证 Bearer 元数据，执行确认语义不变。
- `tests/commander/component/test_gateway_simulation_session.py`：废弃 mTLS Gateway 测试搭建，改为真实本地 gRPC + Bearer 鉴权伪 Gateway，保留命令执行证明、重试身份、超时的组件断言。
- `tests/commander/unit/test_token_transport.py`：新增令牌加载、校验、非法监听、证书字段拒绝、权限边界的正反例。**未运行**。

**配置中发现并修复的严重候选缺陷**：E2E Gateway-source@1.1.1 的 `servers[].control.simulation` 存在两个重复 `token_env`，严格 YAML 应拒绝。新建 Gateway-source@1.1.2、CLI@1.1.2、Deployment@1.0.4 并精确重选；原源 YAML 保留未发布历史，当前三根工作文件数仍 29/23/35，版本台账升至 **135** 源项。新配置根是未发布候选，必须在下一轮严格校验中核对。

**仍须由 ChatGPT 完成的开发及清理**：Gateway 目前不符合 Token v2/v3 Schema，且其原本 loopback 明文权限退化逻辑未解决；Generator 仍有 mTLS 客户端实现；CLI 与系统测试大批证书型 Fixture 尚未迁移；prod-a 服务仍引用历史配置；Common RPC 的业务写入旁路及版本硬编码需后续安全删除。**本次不将 C2 宣称完成**，更不应在服务尚未端到端鉴权时开放 Gateway/Commander。

**软件质量状态**：本执行环境没有项目可执行工作树，未运行 Ruff、mypy、pytest 或完整 E2E；不得把 GitHub 成功写入视为代码质量验收。待 ChatGPT 完成各模块生产代码与 Fixture 集中整改后，再由 Claude 独立测试、小范围修复与复测。


### 16.10 第二阶段 C3：Gateway / Data Generator Token 化及冗余清理（2026-11-23）

**仍是集中编码中的中间状态，尚未形成测试准入。** 本轮完成配置及生产实现的以下定向变更：

- Gateway：严格 Service Schema v2/v3、`security.callers[]` 单一授权来源、loopback Listener、受控 Sink 注入权限；删除内部 mTLS 和对明文 loopback 匿名放行；Gateway→Commander、Gateway→Generator 使用独立的 `token_env` Bearer 元数据，所有 RPC 必须鉴权。新增 `tests/industrial_gateway/unit/test_token_security_schema.py`，迁移 `test_generator_rpc_client.py` 至真实令牌 gRPC。
- Commander：此前 C2 已迁移管理及仿真 Gateway 双向令牌链路，仍有大量旧证书式系统测试 Fixture 尚待整改。
- Data Generator：新增 `infrastructure/gateway_auth.py`，移除 `infrastructure/gateway_tls.py`；Gateway Writer、GatewayRpcClient、Diagnostics 和 CLI `--check` 统一以 `output.gateway_token_env` 授权且限定 loopback；Service 仅接受 Schema v2，删除 `gateway_tls`；所有 9 项专属 RPC 与 Common 9 RPC 由 `GeneratorAuthorization` 统一 Bearer 权限拦截。原 `GetCapabilities` 证书门禁删除。
- 测试：删除不再适用的 `tests/data_generator/integration/test_gateway_writer_mtls.py`，新增真实 Bearer Gateway RPC 链的 `test_gateway_writer_token.py`、所有 RPC 权限拦截测试 `tests/data_generator/unit/test_grpc_authorization.py`；未完整覆盖删除的旧诊断测试，需要继续补足。

**精确配置候选**：sim-a 最终选择 Deployment@1.8.0（Commander@1.3.0、Gateway@1.3.0、Generator@1.3.1、CLI@1.8.0）；e2e-final 选择 Deployment@1.0.5（Commander@1.1.0、Gateway source@1.1.2/target@1.1.1、Generator@1.1.1、CLI@1.1.3）。这些都是未发布的新文件版本。台账已添加至 **146** 源 YAML，独立根仍目标为 prod-a 29、sim-a 23、e2e-final 35；未经严格配置闭包、Loader 或运行时测试，不宣称 PASS。

**高优先级未关闭项**：prod-a 旧 v1 服务与新运行时代码不兼容；CLI / Commander / Gateway / Generator / Sink 多处测试 Fixture 仍以证书为契约；Generator 改动涉及所有运行时配置测试，需逐调用方同步；Gateway Sink/管理端口虽有角色校验，但物理监听与服务暴露隔离需独立证明；Storage 末端 RAW/PROTOCOL/BUSINESS E2E 和 30×10Hz×500 点性能未跑；旧快照写入及固定目录版本清理仍未结束。

**验证说明**：本轮通过 GitHub 确认生产代码、测试与配置提交，未实际执行 Ruff、mypy、pytest、严格 YAML 解析、真实服务启动或跨模块 E2E。Claude 仅在集中编码收口后做独立测试及小范围修复；ChatGPT 继续承担主要实现及失效测试的迁移修正。所有软件门禁 `NOT_RUN`，生产准入 `BLOCKED`。


### 16.11 第二阶段 C3 补充：全 RPC 鉴权与旧测试清理

补充生产代码 `src/data_generator/infrastructure/adapters/inbound/grpc_auth.py`：Generator 专属及 CommonService 共 18 个 Unary 方法统一走 loopback + Bearer 拦截器，按只读/写入分别要求 `variables_read`/`variables_update`；缺失、重复或越权 Token 拒绝，旧 Common Push/Apply 仍明确拒绝发布业务配置。该拦截器在 `make_server` 注入，独立读取配置时按服务端真实 Token 对照。

补充测试 `tests/data_generator/integration/test_gateway_writer_token.py`，用实际本地 gRPC Server 验证 Writer→Gateway 读点、租约、更新及错误 Token 拒绝；删除 `test_gateway_writer_mtls.py`。新增 `tests/data_generator/unit/test_grpc_authorization.py` 覆盖只读、写入和匿名 Common RPC。修改 `tests/data_generator/conftest.py` 的共享 Service Fixture 为 Schema v2/出站授权令牌，配置测试专用环境变量。Gateway 服务端在进程启动及运行中令牌身份歧义均拒绝。

**限制**：其余 Commander/Gateway/CLI/E2E 旧证书 Fixture 尚未整体迁移；删除的历史 mTLS 诊断用例所覆盖的诊断语义需以新的 Bearer 诊断测试补齐；生产候选 Schema 与权限实现也未跑静态门禁。该专题仅能记为 `IMPLEMENTED_PENDING_TEST`，不能记为 `PASS`。依照当前分工，ChatGPT 后续集中修正代码及测试，Claude 最终独立执行测试与小修复。
