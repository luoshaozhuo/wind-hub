# wind-hub 设计报告符合性修订记录（2026-11-23）

## 1. 审查基线与范围

- main 基线：`f91068748838103f547f1140d24b4eb93cbf7372`
- 文档工作分支：`docs/design-consolidation-20261123`
- 冻结总体架构：`docs/design/wind-hub_总体逻辑架构设计_v1_27.md`，内容 Blob SHA `891486c006c5f964ffa2e61e950104102004f9ab`，本轮未修改
- 排除 Web Server、Web Admin；生产代码、配置和测试文件均未修改
- 第 1～2 章编制规范保持原样，本次改动在各文档业务正文并统一提升文档版本

## 2. 修订交付

| 单元 | 修订前 | 修订后 |
|---|---|---|
| Contracts | v1_41 | v1_42 |
| Core | v1_41 | v1_42 |
| Configuration Service | v1_18 | v1_19 |
| CLI | v1_51 | v1_52 |
| Commander | v1_55 | v1_56 |
| Collector | v1_39 | v1_40 |
| Data Generator | v1_58 | v1_59 |
| Industrial Gateway | v1_48 | v1_49 |
| Sink Service | v1_71 | v1_72 |
| Storage Service | v1_5 | v1_6 |
| 跨模块测试 | v0_36 | v0_37 |

总体设计继续冻结 v1_27，旧版模块文件已由对应新文件替换。跨模块测试设计 §3.3 的模块引用同步更新。

## 3. 主要已修订矛盾

1. CLI 的 Sink 输出等级由 `raw|engineering|business` 且默认 business 更改为显式 `raw|protocol|business`；跨模块测试相应调整，RAW 无可靠来源时不得判通过。
2. Sink 对 Collector 的现行 Batch Schema 2 声明更正为 Schema 3；Collector 相同过期声明同步更正。Kafka JSON v2 与 SampleBatch v3 仍是两份独立线格式。
3. Configuration 发布完成不等同所有服务激活完成；CLI 不逐模块协调 Deployment 业务配置 Apply；Sink 多实例配置也以 Repository/Deployment 为唯一权威。
4. Contracts Repository 现行 15 RPC、Gateway 10 专属 + Common 9、Commander 8 专属 RPC 的描述按主分支 Proto 对齐。历史版本引用进行部分更新。
5. 修正 Commander/Collector/Gateway/Sink 对 C7 已集成功能错误标注“main 尚未实施”的陈述。
6. 清理 Storage 中部分旧分支依赖阻断及失效版本引用，说明当前 RPC 依赖已进入 main。
7. 区分 Core 的内部 `engineering_value` 数值计算能力与退役的 ENGINEERING 对外数据等级。
8. **修订过程中发现实际 CLI 仍注册 `config push/apply`**：在 CLI 设计中保留事实，明确它们是 CommonService 本地快照命令，不是 Repository 全量业务配置发布手段；对服务端是否形成写入旁路须进一步检查，不能因文档写了限制就宣称代码隔离有效。

## 4. 未关闭问题与风险

| ID | 状态 | 说明 |
|---|---|---|
| A-01 | OPEN / 总体冻结内部矛盾 | 总体 §3 表 3-1 仍有 RAW/ENGINEERING/BUSINESS 默认 BUSINESS，与 §5.3.4 的正式数据等级冲突；按用户要求不改总体 |
| A-02 | OPEN / 总体冻结内部歧义 | 总体 §5.3.4 对 RAW 同时描述为协议解码后的标量与解码前载荷，而 PROTOCOL 也描述为协议解码值；此边界未形成可独立验收的单义定义 |
| I-01 | VERIFY_CODE | main CLI `config push/apply` 仍可访问服务 Common RPC；需要追踪服务端授权/作用域，防止绕过 Repository 的业务配置变更 |
| I-02 | VERIFY_CODE | Gateway 管理端口 mTLS 与内部 Sink 注入端口的实际监听、鉴权、网络范围须与冻结总体内部 gRPC 无 mTLS 原则分区核对 |
| D-01 | DOCUMENT_DEBT | 若干长篇模块设计仍包含历史阶段性测试、快照 RPC 旧流程及早期 Contracts 版本说明；当前仅清理已明确的规范冲突，尚未对全部 Mermaid、每张接口表、每个交叉引用做完整机审 |
| T-01 | NOT_RUN | 未执行 Git 工作树级全文扫描、Markdown/Mermaid/LaTeX 渲染及全仓自动测试；版本回读和重点 Proto/代码抽查不替代全面复核 |

## 5. 代码证据

- `src/contracts/sample_batch.py` 强制 `schema_version=3`
- `src/collector/application/batch_builder.py` 使用来源 `ProtocolSample.value`，未在采集侧调用工程/业务换算
- `src/contracts/configuration_repository.proto` 现有 15 RPC，含 Service/PointTable 闭包与修订查询
- `src/contracts/industrial_gateway.proto` 现有 10 RPC
- `src/contracts/commander.proto` 现有 8 RPC
- `src/cli/configuration_commands.py` 注册 `config publish/get/push/apply`；`src/cli/snapshot_commands.py` 中 push/apply 实际调用 Common 的快照 RPC

## 6. 阶段裁决

**PARTIAL PASS — 已按冻结总体架构修订主要模块规范冲突，但不得宣布完全符合。** 总体架构内部 RAW/PROTOCOL 语义仍矛盾，且 CLI 快照写入旁路的代码约束尚未审计。后续先处理 A-01/A-02 的统一语义裁决及 I-01 服务端限制，再全面逐节检查所有遗留历史约定。未创建 PR、未合并 main。
