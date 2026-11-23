# wind-hub 全模块最终联测与冻结前审查（2026-11-23）

## 1. 基线与审查边界

- 主分支：`origin/main@f91068748838103f547f1140d24b4eb93cbf7372`（本轮 GitHub 复核）。
- 设计分支：`docs/design-consolidation-20261123`。
- 总体架构：`wind-hub_总体逻辑架构设计_v1_27.md`，冻结且本轮未修改。
- 最新跨模块测试设计：`wind-hub_跨模块测试设计_v0_39.md`。
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
