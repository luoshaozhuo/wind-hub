# docs/design 设计报告归并审查记录（2026-11-23）

## 1. 范围与基线

- 来源：main @ `f91068748838103f547f1140d24b4eb93cbf7372`。
- 工作分支：`docs/design-consolidation-20261123`。
- 范围：总体架构、跨模块测试设计，以及 Contracts、Core、CLI、Configuration Service、Commander、Collector、Data Generator、Industrial Gateway、Sink Service、Storage Service。
- 排除：Web Server、Web Admin。
- 本轮仅修改设计文档，不修改 `src/`、`configs/`、`tests/`，不创建 PR、不合并 main。

## 2. 归并后的唯一版本

| 设计单元 | 保留文件 |
|---|---|
| 总体架构 | `wind-hub_总体逻辑架构设计_v1_27.md` |
| 跨模块测试 | `wind-hub_跨模块测试设计_v0_36.md` |
| Contracts | `Contracts_软件设计_v1_41.md` |
| Core | `Core_软件设计_v1_41.md` |
| CLI | `CLI_软件设计_v1_51.md` |
| Configuration Service | `Configuration_Service_软件设计_v1_18.md` |
| Commander | `Commander_软件设计_v1_55.md` |
| Collector | `Collector_软件设计_v1_39.md` |
| Data Generator | `Data_Generator_软件设计_v1_58.md` |
| Industrial Gateway | `industrial_gateway_软件设计_v1_48.md` |
| Sink Service | `Sink_Service_软件设计_v1_71.md` |
| Storage Service | `Storage_Service_软件设计_v1_5.md` |

## 3. 重复文件清理

- Contracts：移除无版本文件、`v1_38`、`v1_40`；保留单份 `v1_41`，以较新的无版本版为主体，合入 Deployment/服务闭包权威规则。
- Storage：移除 `v1_1` 与 `v1_4`；保留单份 `v1_5`。
- Sink：无版本文件转为带明确版本的 `v1_71`；避免版本不明。
- 其他九份设计按各自原版本单次递增并移除先前版本文件。

## 4. 关键冲突裁决

1. Collector→Sink 的 NATS 采样协议为 SampleBatch v3；Sink→Kafka 的单点输出为 JSON v2。删除将 v2/v1 误称现行的主要状态描述及 Sink Kafka v1 编码示例。
2. 数据等级规范为 RAW / PROTOCOL / BUSINESS；旧 ENGINEERING 不可自动等价 PROTOCOL；RAW 没有真实协议载荷时 fail-closed。
3. Configuration 由 CLI 全量目录发布；Deployment 选择精确 Service/Instance 版本，Service→Instances→Templates；服务独立拉取并热更新或等待重启，广播仅唤醒。
4. Contracts、Configuration Repository、Storage 的正式 RPC 实现以现行 `src/contracts/*.proto` 为准；Repository 已包括 `GetServiceConfigBundle`、`GetPointTableBundle`、`GetCurrentRevision`。
5. 内部 gRPC 的通用设计不启用 mTLS；Gateway 专属管理/注入端口权限单独依正式实现约束，不能泛化旧证书体系。
6. 跨模块测试设计更新唯一设计文档索引，包含 Storage 全链路。

## 5. 验证边界

- 已通过 GitHub 逐份读取、编辑和回读新版本，并检查题名版本、重复文件处理及关键旧格式表述。
- 未在本执行环境克隆仓库，未运行 Markdown 链接/图形渲染、Proto 生成、Ruff/Mypy/pytest 或完整 E2E；这些结果均为 NOT_RUN，不能以设计归并代替测试通过。
- 旧设计中的阶段性审查证据与验收状态只代表原时间点；后续涉及上线判断必须以本次 main 基线真实执行证据为准。
