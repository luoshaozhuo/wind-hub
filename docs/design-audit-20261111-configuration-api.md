# 共享配置与公共 Python API 源码核查

基线：`567e30ffb1aa1c1a519c0156991ca22811f389cf`。本报告逐项取自当前源文件文本，不把设计中“目标接口”自动当作已实现。

## Service 文件的实际配置来源

| 真实 Service YAML | `config_sources` 子字段（逐层解析） |
|---|---|
| configs/services/collector.yaml | instances, instances.site, instances.devices, templates, templates.root |
| configs/services/commander.yaml | instances, instances.devices, templates, templates.root |
| configs/services/industrial_gateway.yaml | instances, instances.devices, templates, templates.root |
| configs/services/data-generator-service.yaml | instances, instances.site, instances.devices, instances.scenarios, templates, templates.root |

**特别说明：**上表仅覆盖仓库当前四份 Service YAML；Sink Service 的配置来源另由其专属配置文件及私有 Schema 裁决，不得据此判定 Sink 缺失。

## Configuration 公开结构事实

`src/configuration/shared_documents.py::SharedConfigPaths` 字段：

- `instances: Path`
- `templates: Path`
- `site_file: Path | None = None`
- `devices_file: Path | None = None`
- `load_site: bool = True`
- `coordination_root: Path | None = None`

**判定：**其 Python dataclass 并非 Service YAML 的 `config_sources` 映射。Configuration v1_14 的“两层输入不可直接互换”规则与现有源码一致。任何文档如果直接将 `config_root` 当成上述 dataclass 构造参数，属于接口错误。

## 其他已读取 Python 文件中的顶层 API / Classes

| 源文件 | Classes | Public top-level def / async def |
|---|---|---|
| src/configuration/shared_documents.py | SharedConfigPaths, SharedDeviceDefinition, SharedDocuments, SharedDocumentLoader | — |
| src/configuration/versioned_templates.py | TemplateRef, ResolvedTemplate, VersionedTemplateRepository | config_hash |
| src/contracts/sample_batch.py | SampleItem, SampleBatch | validate_subject_token |
| src/core/infrastructure/config/__init__.py | — | — |
| src/collector/application/config_manager.py | 未取得源码（路径不存在） | 未验证 |
| src/commander/application/config_manager.py | ReloadReport, SettingsLoader, CommanderConfigManager | — |
| src/industrial_gateway/infrastructure/config/v1_schema.py | StrictModel, InstanceSources, TemplateSources, ConfigSources, TlsConfig, GrpcConfig, CommandMapping, ServerConfig, ControlConfig, MetricsConfig, GatewayServiceConfig | service_path |
| src/data_generator/infrastructure/snapshots.py | FileContent, SnapshotManager | file_sha256, recover_transaction |

## SampleBatch v2 源码字段

- `schema_version: Literal[2] = 2`
- `batch_id: str`
- `mode: DeliveryMode`
- `wind_farm_id: str`
- `collector_id: str`
- `task_id: str`
- `device_id: str`
- `publish_time: datetime`
- `samples: tuple[SampleItem, ...]`

设计文档必须区分批次 `schema_version=2` 与 Kafka 下游输出自身的编码格式，不能误将 Kafka payload 的版本当作 NATS 输入 Schema。

## 执行限制

此阶段为源码接口文本核对，尚未执行 Python 导入/类型检查、运行时授权、跨进程配置事务、工业现场硬件及 NATS 系统测试。Service YAML 的字段存在不等于全部消费调用方均符合设计。
