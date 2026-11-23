# 设计体系第二轮符合性审查（2026-11-23）

## 1. 审查基准与冻结边界

- main 来源：`f91068748838103f547f1140d24b4eb93cbf7372`。
- 被审设计工作分支：`docs/design-consolidation-20261123`。
- **冻结且本轮不改动**：`docs/design/wind-hub_总体逻辑架构设计_v1_27.md`。
- 范围：Contracts、Core、Configuration Service、CLI、Commander、Collector、Data Generator、Industrial Gateway、Sink Service、Storage Service、跨模块测试设计。
- 排除：Web Server、Web Admin。
- 本轮为设计静态审查与关键源码抽查，非独立系统测试，不修改设计文件、生产代码或配置。

## 2. 裁决方法

以总体设计 §3 模块职责、§4 数据流、§5.2/§5.3/§5.7 配置闭包、§5.3.4 数据等级、§6 分层及 §7 约束为主轴。若总体设计内部存在互斥定义，则不得用“符合总体”掩盖歧义；应记录为冻结基准内部缺陷，待单独裁决。现行 `src/contracts/*.proto` 和 `src/contracts/sample_batch.py` 用于识别文档历史状态，不用源码未经评审的实现反向覆盖已冻结的总体目标。

## 3. 阻断与不一致清单

| 编号 | 级别 | 文档及位置 | 发现 | 对照与处理建议 |
|---|---|---|---|---|
| A-01 | P1 | 总体设计 §3.1 表3-1 Sink 行；§5.3.4 | 同一冻结文档同时规定 RAW/ENGINEERING/BUSINESS 默认 BUSINESS 与 RAW/PROTOCOL/BUSINESS 显式配置 | 本轮不改总体；记录冻结文本内部矛盾，暂按后述正式三级契约审其他模块，不可称总体完全无冲突 |
| A-02 | P1 | 总体设计 §5.3.4 L1/L2 表及其后文 | 表格 L1 RAW 是“协议帧解出的原始标量”，后文却称 RAW 是协议解码前载荷；L2 PROTOCOL 也是协议解码值，两者界线不唯一；又称 RAW 不是完整帧 | 冻结基准内未形成唯一可实现的 L1/L2 判别规则；需独立语义裁决后才允许作为验收门禁 |
| M-01 | P1 | CLI v1_51 §10.4（约 2065 行） | 仍允许 `raw|engineering|business` 且默认 business | 与总体 §5.3.4 / Contracts v1_41 §11.9 的显式 `raw|protocol|business` 冲突；应清理旧规则 |
| M-02 | P1 | Sink v1_71 §3.1（约 775 行） | 将现行批消息写为 `SampleBatch(schema_version=2)` | main `src/contracts/sample_batch.py` 实际 Literal[3]、严格拒绝非3；应纠正全部当前状态陈述 |
| M-03 | P1 | Sink v1_71 §3.7 / §11 | 仍允许各容器直接通过本地 Get/Push/Apply 提交配置快照 | 与 Deployment 作为唯一业务配置选择、Repository 发布与业务模块自主对账冲突；本地 Common RPC 不可作为业务发布旁路 |
| M-04 | P1 | Configuration v1_18 §10.4（约1559行） | 仍要求 CLI 对业务模块执行 ApplyConfigSnapshot 并记录结果 | 违背总体 §5.2–5.3；须统一改为发布提交与模块激活状态分别观察 |
| M-05 | P1 | 跨模块测试 v0_36 §9（约560、572行） | 仍用 RAW/ENGINEERING/BUSINESS 三级测试，断言缺省 BUSINESS | 与正式等级、显式选择及 RAW fail-closed 不一致，将导致无效 E2E 验收 |
| M-06 | P2 | Commander v1_55、Collector v1_39、Gateway v1_48、Sink v1_71 附录 | 称当前 main 未实施全量目录和广播 | C7 已在 main 集成；旧附录“目标状态”与正文的主分支基线互相冲突；删除阶段性错误状态，保留真实规则 |
| M-07 | P2 | CLI v1_51 §11（约2391行） | `config get/push/apply` 被写成读取业务配置树的当前 CLI 入口 | 与根目录发布及取消逐模块 Apply 方向冲突；须按当前命令树核对并移除旧命令引用 |
| M-08 | P2 | Storage v1_5 §3、§8、§15–16 | 大量“开发分支尚缺 RPC”、设计分支 v1_40 冲突、S1/S2 阶段性欠账作为现状叙述 | 已有 `GetServiceConfigBundle`/`GetPointTableBundle`/`GetCurrentRevision` 的正式 Proto；历史实施日志应移至审查报告，不应混在现行规范 |
| M-09 | P2 | Contracts v1_41 §10、§12 | 部分 Repository 接口仍称“目标”、带旧 RPC 数量/未实施前提 | main `configuration_repository.proto` 定义15项 RPC；按 Descriptor 更新接口总览与验收清单 |
| M-10 | P2 | Core v1_41 §10、§13 | 工程换算用于 Sink 的目标清晰，但部分说明保留“工程等级”措辞及尚未实现的装配器示例 | 对齐正式 `protocol/business` 定义；区分现行接口、设计中的待实现 API 和非规范性示例 |
| M-11 | P2 | Sink v1_71 §14 及末尾附录 | 原 main SHA、R0/R1/R13 历史、Schema2迁移、Gateway mTLS 特例交织为现行规则 | 清理已过期阶段记录；保留独立管理面与 Sink 注入面安全边界，是否保留 mTLS 仅依正式 Gateway 配置，不把旧例提升为全局规范 |
| M-12 | P2 | 总体设计 §5.3.3/§5.7 的配置示例与各模块章 | 多处仍使用“目标 Schema / 迁移待做”语气，虽 main 已合并 | 冻结本文不改；其他文档应以现行运行 Schema 与配置解析实现逐字段对照，不能仅依设计样例断定可运行 |

## 4. 分模块审查结论

| 单元 | 静态符合性 | 主要偏差 |
|---|---|---|
| Contracts v1_41 | 部分符合 | Repository 接口的目标/现行状态混杂；RAW/PROTOCOL 定义受冻结总体内部矛盾影响 |
| Core v1_41 | 基本符合但有歧义 | 共享换算原语与三级等级术语的边界、非规范性目标代码示例 |
| Configuration v1_18 | 不符合 | CLI 逐模块 Apply 与独立 Deployment 发布互斥 |
| CLI v1_51 | 不符合 | 旧数据等级和默认值；旧配置命令路径 |
| Commander v1_55 | 部分符合 | 附录对 C7 已实施状态陈述过期 |
| Collector v1_39 | 基本符合但存在历史残留 | 主业务路径与现行 SampleBatch v3 一致，附录仍称配置方案未实施 |
| Data Generator v1_58 | 基本符合 | 需清理旧 Contracts 版本引用、区分已实现能力和测试要求 |
| Industrial Gateway v1_48 | 部分符合 | 附录 C7 状态过期；专属安全入口要与全局内网规则保持边界 |
| Sink v1_71 | 不符合 | Schema2 现行声明、本地配置旁路、等级语义和历史安全附录 |
| Storage v1_5 | 部分符合 | 主要数据方向符合，仍混有已过期的特性分支阻断及版本引用 |
| 跨模块测试 v0_36 | 不符合 | 等级与默认值错误，会使正式 System Test 得到错误验收结论 |

## 5. 现行源码抽查证据

- `src/contracts/sample_batch.py`：Schema 版本为 Literal[3]；decode 显式要求 version 3。
- `src/collector/application/batch_builder.py`：`_item` 直接使用 `ProtocolSample.value` 生成 `SampleItem.value`，未做 `engineering_value` 或单位标准化；按固定 `point_table_id` 封装。
- `src/contracts/configuration_repository.proto`：15 RPC，含 `GetServiceConfigBundle`、`GetPointTableBundle`、`GetCurrentRevision`。
- `src/contracts/common.proto`：公开状态定义 `target_revision` 与 `active_revision`，同时保留 Common 的 Get/Push/Apply ConfigSnapshot RPC；保留接口不等于允许通过它绕过 Deployment 唯一发布路径。
- `src/contracts/storage_service.proto`：StorageDataLevel 数值 RAW=1、PROTOCOL=2、BUSINESS=3。

## 6. 处理顺序和门禁

1. **冻结总设计不改**；首先对 A-01/A-02 出具“冻结文本内冲突”的解释性裁决，尤其 RAW 与 PROTOCOL 的物理语义。不得暗改总设计来取得虚假符合。
2. 优先修正 P1 模块冲突：CLI/Sink/Configuration/跨模块测试，再清理 Contracts 及其余业务文档的旧版本、已完成工作误标“未实施”。
3. 模块设计修正时保留当前第1–2章模板约定；不触碰生产代码，不把历史审查结论当成正式规范。
4. 修订后进行全局定向搜索：`ENGINEERING`、`schema_version=2`（区分 Kafka v2 合法用途）、`默认 business`、`CLI Apply`、`尚未实施 C7`、旧 SHA/旧版本文档引用；逐条人工裁决，不能机械全替换。
5. 复核与 `src/contracts/*.proto`、共享线格式、运行 Schema、CLI 命令和用例追溯矩阵的对应关系。
6. 本轮没有执行 repo-wide grep、Mermaid/Markdown 渲染、ruff/mypy/pytest 或独立 E2E，均为 NOT_RUN；静态审查不能冒充代码或运行验收。

## 7. 总体裁决

**FAIL（设计文档一致性，不是主分支生产代码运行裁决）**。冻结总体架构文件没有被修改，但其内部尚有互斥的数据等级规范，且多个模块仍有 P1 冲突。暂不可宣布“所有模块符合 v1_27”。建议先完成语义裁决，再在同一文档分支集中整改其他模块，之后独立审查复测。
