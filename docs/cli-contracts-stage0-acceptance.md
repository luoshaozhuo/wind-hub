# wind-hub CLI v1_0 阶段 0 Contracts 独立验收报告

- 分支：`feat/cli-v1-contracts`
- 任务预期 HEAD：`116607e742de65591ac064bcd7a45931efb71bef`
- 验收起点：`f0af9d04`（预期 HEAD 为其祖先；期间 13 个提交为 ChatGPT 清理旧生成物）
- 验收后 HEAD：分支 tip（修复提交 `88681a07`，随后为测试与本报告提交）
- 设计依据：`docs/CLI_软件设计_v1_0.md` 第 9 章（并遵守第 1、2、14 章）
- 日期：2026-11-01

## 1. Git 基线与工作区

- Fetch 最新远程后本地与 `origin/feat/cli-v1-contracts` 同步；工作区开始为 clean。
- 验收产生 2 个提交：生成器/mypy 清单修复、阶段 0 契约测试（本报告）。
- 生成物（24 个 `*_pb2*.py[.i]`）按本分支既有约定**不入库**（分支 13 个 chore
  提交已将全部生成物移出 git），由 `scripts/generate_rpc.py` 按需生成；
  `.gitignore` 未列入生成物，工作区出现 untracked 属预期。
- `docs/CLI_软件设计_v1_0.md` 在工作区为 untracked（ChatGPT 未提交设计书），
  本次未改动、未提交。

## 2. RPC 覆盖矩阵（41/41）

| Proto / package | Service | RPC 数 | 名称（与 9.2-9.7 逐条一致） |
|---|---|---|---|
| common.proto `windhub.common.v1` | CommonService | 9 | GetServiceInfo, GetServiceStatus, GetCapabilities, GetConfigSnapshot, PushConfigSnapshot, ApplyConfigSnapshot, GetMetrics, GetEvents, Diagnose |
| collector.proto `windhub.collector.v1` | CollectorService | 11 | ListDevices, GetDevice, ListPoints, GetPoint, ReadPoints, ListTasks, GetTask, StartTask, StopTask, GetPublishingStatus, InterrogateDevice |
| commander.proto `windhub.commander.v1` | CommanderService | 5 | ListDevices, GetDevice, ListPoints, SubmitCommand, GetCommandResult |
| sink_service.proto `windhub.sink.v1` | SinkService | 4 | ListDeliveries, GetDelivery, StartDelivery, StopDelivery |
| industrial_gateway.proto `windhub.gateway.v1` | IndustrialGatewayService | 6 | ListGateways, GetGateway, StartGateway, StopGateway, ListVariables, ReadVariables |
| data_generator.proto `windhub.data_generator.v1` | DataGeneratorService | 6 | ListDevices, GetDevice, ListScenarios, GetGeneration, StartGeneration, StopGeneration |

全部 41 个方法均为 Unary（无 streaming）；输入/输出消息全名经 Descriptor
逐方法断言（`tests/contracts/test_service_matrix.py`）。9.8 服务进程管理为
本机 containerd/isula/systemd 契约，非 gRPC Proto，正确地未进入 41 计数。

## 3. 第 9 章字段级符合性

逐条核对 9.1 通用约定与 9.2-9.7 输入输出表，结论：**设计要求的输出字段全部
可表达**，要点：

- ScalarValue 六种 oneof 标量（bool/sint64/uint64/double/string/bytes）。
- optional/存在性：PointMetadata.minimum/maximum、InterrogateDeviceRequest.
  common_address、StartGenerationRequest.scenario_version、Sample.value
  均为 `optional`，零值/未设置可区分；ReadPoints「不得用零替代无数据」由
  optional Sample + 逐项 error_code/error_message 承载。
- 时间：started_at/observed_at/sample_timestamp/queried_at/expires_at/
  updated_at/started_at/sampled_at 均为 google.protobuf.Timestamp。
- 分页：10 个 List 请求 page_size/page_token，响应 next_page_token；
  GetEvents 同形。
- 版本：OperationRequest/Push/Apply/SubmitCommand/Start/StopGeneration
  均携带 expected_revision；OperationResult/Task/各对象携带 revision。
- 快照三作用域枚举 DISK/RUNTIME/CANDIDATE（含 UNSPECIFIED=0，数值钉死）；
  SnapshotFile(relative_path/content/size_bytes/sha256)；Push 报告
  valid+ValidationIssue+base_revision（校验失败候选可空）；Apply 分别报告
  file_commit 与 apply_results[]、effective_revision、restart_required、
  application_id；运行视图 application_id/apply_results/last_error 可复查。
- 诊断：DiagnosticStage(stage/status/duration_ms/error_code/message/
  evidence[]/observed_at/source) + overall_status + limitations[]，
  可表达「缺消息级关联证据不判端到端成功」。
- Commander：SubmitCommand 含 command_id/accepted/state/outcome，
  GetCommandResult 含 state/outcome/io_settled/updated_at 及设备测点信息，
  UNKNOWN 与 I/O 收敛语义可表达；command_id 由 Commander 产生（响应字段）。
- Data Generator：StartGeneration 的 scenario_version optional，
  返回 generation_id；Scenario 含 active_version/available_versions/
  device_ids；Generation 含 scenario_id/scenario_version/started_at/
  last_error。
- ReadPoints/ReadVariables 逐项 Sample（值、质量、采样/查询时间、新鲜度、
  错误）；InterrogateDevice 含 missing_point_ids。
- 公共消息只在 common.proto 定义一次（ScalarValue/Sample/PointMetadata/
  OperationRequest/OperationResult/ObjectId/Empty），其余 Proto 经
  `import "contracts/common.proto"` 跨包引用，跨引用类型经描述符断言。
- 无故障注入接口（RPC/消息名称无 fault/inject/chaos）。

Proto3 wire 层不能强制、必须由 Server 承担的 6 项运行时校验已在
`test_server_responsibilities.py` 中固化清单：point_ids/variable_ids 非空、
CANDIDATE 作用域 candidate_id 必填、快照相对路径安全（拒绝 `..`/绝对路径/
symlink/重复）、page_size 上限、Push 失败不产生可应用候选、版本冲突走
gRPC ABORTED（传输层状态码，不入消息）。

非阻断观察项（P3，不影响冻结，留待 Client/Server 阶段约定取值域）：

1. health/state/outcome/severity/channel/scope 等状态量为自由 string，
   设计未要求枚举，但冻结后需以文档或 Server 校验约束取值。
2. DiagnoseRequest.correlation_id 为普通 string（无 field presence），
   以空串表达「未提供」可满足 9.2.2「可选关联标识」。
3. Get*/Start/Stop 请求复用公共 ObjectId(id)/OperationRequest(id)，
   字段名为通用 id 而非 task_id/delivery_id/gateway_id/generation_id；
   wire 等价、RPC 语境无歧义，与设计表字段命名存在表面差异。
4. ApplyConfigSnapshotResponse.file_commit 为单一 string，无结构化逐文件
   错误码；当前「分别报告文件提交/运行生效」可表达，若后续要求逐文件错误
   明细需演进（字段编号冻结后只能新增字段）。
5. collector Device/Task 消息以身份+协议+endpoint+状态字段表达「配置」，
   无完整配置正文；与 9.3.2 文字描述相容但偏精简。

## 4. Proto 编译与生成

`python scripts/generate_rpc.py`：六份 Proto 全部编译通过，生成 24 个文件
（6 份 × `_pb2.py/_pb2.pyi/_pb2_grpc.py/_pb2_grpc.pyi`）；12 个模块全部可
导入；六个 Servicer 类与 add_*_to_server 注册函数齐全。连续执行两次字节级
sha256 完全一致（可重复）。删除全部生成物后，契约测试 conftest 在收集前
自动重新生成并全绿。旧 collector_v2/sink/gateway 生成物磁盘上不存在，
无多余 pb2 模块。

## 5. 静态检查（本次范围内）

| 检查 | 命令 | 结果 |
|---|---|---|
| ruff check | `ruff check scripts/generate_rpc.py src/contracts tests/contracts` | PASS |
| ruff format --check | 同范围 | PASS |
| mypy（strict） | `mypy src/contracts scripts/generate_rpc.py tests/contracts` | PASS, 0 errors in 22 files |
| import-linter | `lint-imports` | 11 contracts kept, 0 broken |

项目级 `mypy src` 仍有 83 errors / 23 files，**全部位于旧 Service**
（web_server 28、collector 19、seahorse 14、sink_service 13、
industrial_gateway 7、cli 4），构成为：旧 pb2 模块缺失导致的
attr-defined/name-defined（契约替换的预期破坏）与 main 既有的
core.infrastructure.config 重构漂移（YamlConfigAdapter/collector_documents
等 import-not-found，与本阶段无关）。按任务要求只记录、不修改旧 Service。

## 6. 契约测试执行

`pytest tests/contracts/ -q`：**87 passed**（marker: contract）。覆盖：
生成物完整/导入/注册函数/字节级可重复/旧产物清理；41 RPC 描述符矩阵与
全 Unary；ScalarValue 六类型编解码与 oneof 互斥；四个 optional 字段
presence（含显式零值与未设置区分）；无数据不以零替代；时间戳/分页/
版本字段；快照全字段与三作用域枚举数值；诊断阶段/证据/来源/局限；
命令受理/执行/io_settled；跨 Proto 公共类型引用；字段编号与字段名
逐消息唯一、枚举数值唯一；无故障注入接口；无旧包/旧 Service；
Server 校验责任清单（正向+反向）。

## 7. 遗留生成物检查

`git ls-files src/contracts/` 仅含 6 份 proto、__init__.py、sample_message.py；
RETIRED_PROTO_NAMES（collector_v2/sink/gateway）四种后缀文件均不存在；
测试断言磁盘 pb2 集合恰为权威 24 文件。

## 8. 受影响组件清单（仅记录，未修改）

生产代码仍引用旧契约（运行期 ImportError / mypy 报错，待各组件阶段迁移）：

- `src/collector/infrastructure/grpc/v2_server.py`：collector_v2_pb2(_grpc)、旧 common_pb2
- `src/cli/collector_control.py`、`src/web_server/adapter/outbound/grpc/collector.py`：collector_v2_pb2(_grpc)
- `src/industrial_gateway/infrastructure/grpc/data_server.py`、
  `src/sink_service/infrastructure/adapters/grpc_sink.py`、
  `src/seahorse/infrastructure/grpc_publisher.py`：gateway_pb2(_grpc)
- `src/seahorse/infrastructure/grpc_command_server.py`、
  `src/web_server/adapter/outbound/grpc/commander.py`：旧 commander_pb2(_grpc)/common_pb2（包名复用，消息形态已变）
- 另约 14 处 `common_pb2` 引用分布于 collector/sink_service/industrial_gateway
  的 grpc/nats 适配层（ScalarValue 等公共消息形态需重新适配）。

测试侧 17 个文件引用旧接口（tests/collector、tests/industrial_gateway、
tests/web_server、tests/system、tests/support/control.py 等）；当前
tests/collector 在更早的 collection 阶段即被 main 既有 core 配置重构
漂移（CollectorDocumentLoader）阻断，属 UNRELATED 基线问题。

构建/部署：deploy/、configs/ 无旧名引用；`pyproject.toml` 的 mypy
stub 模块清单本次已更新（见第 9 节）。CI（ci-fast.yml）当前未接线
generate_rpc 步骤，后续阶段需在门禁/镜像构建中生成产物（P2 流程项）。

## 9. 小范围修复（2 处，已提交）

1. `scripts/generate_rpc.py`：`int(protoc.main(options))`，修复 strict
   mypy no-any-return；grpc_tools 无 py.typed，在 pyproject 既有 grpc
   动态模块 override 中增补 grpc_tools.*（与既有 grpc 处理方式一致）。
2. `pyproject.toml` mypy override：生成物 stub 豁免模块清单从旧名
   （collector_v2_pb2/gateway_pb2/sink_pb2 等）更新为权威六 Proto 的
   12 个模块。修复前 46 个新生成 stub 的 unused-ignore/overload-cannot-match
   错误；修复后 contracts 范围 mypy 0 错误。未触碰任何旧 Service，
   未为旧契约保留别名/兼容层。

## 10. 未验证事项与最终裁定

未验证：mTLS/真实 gRPC 链路/Server 行为属后续阶段（Proto 无法承载）；
真实 containerd/isula/systemd 不属阶段 0；旧 Service 全量回归本阶段不要求
通过且既有 core 重构漂移已独立存在；生成物不入库的 CI 接线为 P2 流程项。

**裁定：PASS WITH LIMITATIONS**

- 六份 Proto、41 个 RPC、第 9 章全部规定输出字段经描述符与编解码实证
  完整、可表达；编译/生成/可重复/旧产物清理/四门静态检查/87 项契约测试
  全部 PASS。
- 限制：5 项 P3 取值域/命名/结构表达观察（第 3 节），不阻断冻结，
  建议冻结前由 ChatGPT 对第 3 节 P3-1（状态量 string 取值域）给出
  正式约定；6 项 Server 运行时校验责任已在测试中固化，须在各 Server
  阶段验收；旧 Service 迁移与 CI 生成接线不在阶段 0 范围。
