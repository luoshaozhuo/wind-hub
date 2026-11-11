# 六份 Proto 字段与 Contracts 文档字段行机械核对

基线：`main@567e30ff`；审查目标 `Contracts_软件设计_v1_19.md`。将 Proto 的每个 `message` 字段名和字段号与 Contracts 文档中包含同名消息的 Markdown 表格行核对，排除注释及非 message 常量。

| Proto | 消息数 | 字段数 | 文档中未定位到字段号的消息数 | Proto 内重复字段号的消息数 |
|---|---:|---:|---:|---:|
| common.proto | 32 | 122 | 0 | 0 |
| collector.proto | 20 | 56 | 0 | 0 |
| commander.proto | 15 | 58 | 0 | 0 |
| sink_service.proto | 4 | 15 | 0 | 0 |
| industrial_gateway.proto | 15 | 49 | 0 | 0 |
| data_generator.proto | 13 | 42 | 0 | 0 |

## 待复核字段清单

### common
- 未发现字段名/字段号在文档中缺失

### collector
- 未发现字段名/字段号在文档中缺失

### commander
- 未发现字段名/字段号在文档中缺失

### sink_service
- 未发现字段名/字段号在文档中缺失

### industrial_gateway
- 未发现字段名/字段号在文档中缺失

### data_generator
- 未发现字段名/字段号在文档中缺失

## 解释与验收限制

此扫描只检查消息字段在 Contracts 表格内可定位；未进行运行时 Descriptor、字段类型与标记的完备语义比对。若某字段使用不同表格列格式、未用反引号、或拆分为其他语义章节，可能成为误报。必须结合人工逐条复核，并在运行时重新生成并比较 `*_pb2.py`、`*_pb2_grpc.py`、`*.pyi` 的 Descriptor/Stub 才能最终验收。
