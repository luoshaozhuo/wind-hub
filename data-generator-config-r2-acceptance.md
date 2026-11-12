# Data Generator 配置系统重构独立验收报告（R2）

- 验收分支：`feat/data-generator-physical-model-v1`
- 生产 HEAD（验收期间冻结）：`c79998b66c3824488d548503d1ff73fe674c077f`
- 验收提交（纯测试侧，已 push）：`8af1c8773`（夹具/套件迁移）、`01c58d152`（JSONL 录制专项）
- 验收后 HEAD：`01c58d15296c0cad35d72017fdc1f2b09ff742d0`（本地与 origin 一致）
- 工作树：干净（验收提交外无未跟踪/未提交文件）
- 日期：2026-11-12，本机 WSL2，conda 环境 `wind-hub`

## 1. HEAD 与 Git 状态

- 禁止项遵守：无 rebase、无 force push、未合并 main、未创建 PR。
- 生产代码零改动：两个验收提交均只触及 `tests/`；冻结目录 `src/core`、`src/configuration` 全分支对 main 零 diff；`src/contracts` 仅含本特性自有的 data_generator proto 生成物（历史提交，非本次重构、非验收改动）。
- 配置重构提交集（`e7e3dd739~1..c79998b66`，5 提交）文件面仅 4 个：DG 设计文档、两个服务 YAML、`src/data_generator/infrastructure/config.py`。

## 2. S1 配置/设计一致性（8/8 PASS）

| # | 核查项 | 证据 | 结论 |
|---|--------|------|------|
| 1 | instances/ 仅业务实例 | `configs/instances/` 只剩 site/devices/devices_sim/deliveries/sinks 五个业务文件；服务配置全部位于 `configs/services/`（8 个） | PASS |
| 2 | Scenario 归属 DG 服务 YAML | 旧 `instances/data-generator*.yaml` 已删除；`data-generator-sim.example.yaml` 内嵌 `scenarios`；设计 v1_37:1992 明确 scenarios 为服务专属 Instance | PASS |
| 3 | 真实/仿真型号独立且各自引用 Point Table | `wtg_gw2mw_1.2.0.yaml`（真实，仅 point_table）与 `wtg_sim_physical_1.0.0.yaml`（point_table + simulation）并存；devices.yaml 与 devices_sim.yaml 两套实例分离 | PASS |
| 4 | 固定物理参数仅存 simulation.parameters | bess 型号 13-19 行 `simulation.kind=device_kind/parameters` 单一来源；设计 v1_37:2706 明确服务 YAML 不得重复额定参数；加载器拒绝场景级 parameters/points 覆盖（config.py:139 等，提交 0827ce47c） | PASS |
| 5 | DG 无 feedback_mapping | 全仓仅剩 config.py:454 的**生产拒绝分支**与文档/测试注释中的历史说明；无任何生效映射数据 | PASS |
| 6 | Gateway 同源 device_id/point_id | Gateway 设计 v1_29:1973 明确变量 ID 与共享 Point Table 点 ID 相同、DG 只发 device_id+point_id 原始值 | PASS |
| 7 | 无旧路径/悬空引用/弃用兼容 | 生产、配置、五份设计文档对 `_sim_1.0.0`、`scenario_file`、`instances/data-generator*` 零命中（测试侧残留见 R2-D2，NOT_RUN 层） | PASS（附带 D2） |
| 8 | 冻结公共基座未改 | `src/core`、`src/configuration` 全分支零 diff；重构提交集不触及 contracts | PASS |

文档矛盾/死示例扫描：总体架构 v1_9、Configuration v1_15、CLI v1_31 对旧词条命中数均为 0。

## 3. 五道静态门禁退出码

| 门禁 | 命令 | 退出码 | 说明 |
|------|------|--------|------|
| G1 Ruff lint | `ruff check src tests scripts` | **1** | 仅 2 个 I001，全在生产文件 runtime.py/config.py（R2-D1）；测试侧 0 问题 |
| G2 Ruff format | `ruff format --check src tests scripts` | **1** | 同样仅上述 2 个生产文件；809 个其余文件达标 |
| G3 Mypy | `mypy src` | **1** | 340 文件仅 5 错，全在 core/tracing.py，全部 UNRELATED（见第 9 节）；RELATED=0 |
| G4 Import-linter | `lint-imports` | **0** | 15 contracts kept, 0 broken |
| G5 契约生成物 | `generate_api_contract.py --check` | **0** | DRIFT CHECK: PASS（openapi 44 paths） |

## 4. 分模块测试计数（全部在进程内，Gateway 依赖均为 Fake/Mock 或进程内真实栈+对端替身）

S2 Data Generator（260 passed，0 failed/xfail/skip）：

| 集合 | 用例数 |
|------|-------|
| tests/data_generator/unit（4 文件） | 46 |
| tests/data_generator/component（6 文件，含新增 JSONL 4 项） | 35 |
| integration/test_history_reader.py（进程内） | 11 |
| integration/test_proto_descriptor.py（进程内） | 10 |
| tests/src/unit/test_physical_models（9 文件，五模型+103 点契约+dt 敏感性） | 118 |
| tests/src/configuration（共享配置 4 文件，回归证据） | 26 |
| tests/src/performance（含 10/20Hz） | 6 |
| tests/src/reliability（3 个进程内文件） | 8 |

S3 Industrial Gateway 内部测试（159 passed）：根级 codec/leases 18、unit 37、component 74、integration 30。全部无 subprocess（已 grep 证实），`real_service` 标记指进程内真实网关栈，Commander/Modbus 对端为契约替身；system/ 双进程 6 文件与 performance 未纳入。

S4 CLI 自身行为/RPC 契约（71 passed）：command_tree 8、config_loader 20、local_tools 22（仅本机 loopback ping，带无 ping 自跳过）、diagnose_and_sinks 4、grpc_integration 17。`*_e2e.py`/test_system_real 未纳入。

**范围内合计 490 passed，0 failed。**

## 5. 配置/快照专项（test_config_manager.py，9 项全绿）

三 scope（DISK/CANDIDATE/RUNTIME）与未知候选拒绝；Push 修订号围栏与完整文件集要求；Core 共享文件只读（tamper 即 read-only 拒绝）；非法候选场景拒绝（新 Schema 下以重复 gateway_id 精确断言 "duplicate Gateway ID"，旧 feedback_mapping 路径已无表达）；场景热 Apply `restart_required=False` 且修订号变更、磁盘即时生效；服务级文件变更 `restart_required=True`；场景版本不可变（同版本改正文报 immutable scenario version）；事务日志 active 回滚与 committed 标记胜出两条恢复路径。

## 6. 103 点契约与物理守恒

- `test_point_table_contract.py`：出厂场景精确 103 点（`sum(counts)==103`、发射值 `len(emitted)==103`），holding 点仍属注册表可写控制输入。
- 守恒/敏感性：bess/ppc/turbine 内能量/功率守恒用例与 `test_timestep_sensitivity.py` 五参数化（dt<0/0/NaN/±Inf 拒绝且内部状态不变，R1 由 strict xfail 转 PASS）全部通过；性能运行实测 10300=100×103、20600=200×103 个有限值，无 NaN/Inf。

## 7. JSONL 真值文件专项（新增 4 项，全绿）

文件：`tests/data_generator/component/test_generator_recording_jsonl.py`，真实 Runtime + 进程内 FakeWriter，真实 Core 配置树。

1. 默认关闭：不创建 artifacts 目录、无任何制品；
2. 开启后每批单行 JSONL：行数==交付批次数，sequence 从 1 单调且唯一，produced_at/source_time 均为带时区 ISO 时间戳，点键集精确为 {device_id, point_id, source_time, value}，值为有限 int/float（allow_nan=False 拒绝 NaN/Infinity）；
3. bytes 值编码为 `{"encoding":"base64","value":...}` 且可解码往返；
4. 磁盘失败（artifacts 预置为普通文件）：Generation 确定性进入 FAILED、last_error 非空、无静默吞错、无半截制品目录。

录制发生在交付计数与 writer.send 之前（runtime.py:584），不依赖 Gateway ACK。

## 8. 10/20Hz 性能同机对比（零回归）

| 工况 | p50 步长 | p99 | 最大预算占比 | 超预算步数 | R1 同机基线 |
|------|---------|-----|-------------|-----------|-------------|
| 10Hz/100 步/103 点 | 286.7µs | 362.8µs | 0.47% | 0 | p50 ≈286µs、0 超预算 |
| 20Hz/200 步/103 点 | 290.9µs | 433.9µs | 0.97% | 0 | p50 ≈291µs、最大 1.67%、0 超预算 |

RSS 增长 0.0MB；两工况 lag p99/max 均为 0。

## 9. 缺陷台账

### RELATED

- **R2-D1（P1，门禁阻断，生产侧，交 ChatGPT）**：`src/data_generator/application/runtime.py:3` 与 `src/data_generator/infrastructure/config.py:3` 存在 I001 导入块排序问题且不满足 ruff format。复现：`ruff check src tests scripts`（exit 1，2 errors）、`ruff format --check src tests scripts`（exit 1，2 files）。纯导入顺序/空白问题、无语义变更。建议修复（生产侧执行）：`ruff check --fix src/data_generator/application/runtime.py src/data_generator/infrastructure/config.py && ruff format src/data_generator/application/runtime.py src/data_generator/infrastructure/config.py`，修复后须重跑 G1/G2 与 DG 组件集回归。验收方按任务边界未修改生产文件。
- **R2-D2（P2，测试债，NOT_RUN 层，交 ChatGPT）**：`tests/data_generator/system/test_config_system.py:85,285`、`tests/data_generator/system/test_simulation_system.py:91` 仍引用已删除的独立文件 `instances/data-generator.yaml`。新 Schema 下场景内嵌服务 YAML，迁移属语义重写且只能在真实双进程链路验证，本次 NOT_RUN 不具备执行条件，未做盲改。

### UNRELATED

- G3 的 5 个 mypy import-not-found 全部位于 `src/core/infrastructure/crosscutting/tracing.py:14-18`（opentelemetry.* 缺失）。证据链：该文件对 main 零 diff；本机 `python -c "import opentelemetry.sdk"` 复现 ModuleNotFoundError；main 同环境同态失败。与本次重构无关，不得为此放松 mypy。

### NOT_RUN（不计入通过数）

跨模块 E2E、真实双进程链路（DG system/ 10 文件、gateway_integration、grpc_server、Gateway system/ 6 文件、CLI 17 个 e2e/system_real）、NATS、Kafka、真实 PLC/Modbus 硬件、netem、长稳 soak、多机。

## 10. 验收结论

**PASS WITH LIMITATIONS（有条件通过）**。

- S1 八项一致性全部成立；范围内 490/490 用例通过，五模型物理守恒、103 点契约、dt 守卫、快照事务语义、JSONL 真值录制、10/20Hz 性能零回归均有实证；测试侧迁移未削弱任何断言，无新增 skip/xfail/ignore。
- 合并前置条件：ChatGPT 关闭 R2-D1（两条机械 ruff 命令，或显式授权验收方代行机械格式化）；R2-D2 在下一轮真实链路系统验收前完成迁移。
- 5 个 OTel mypy 错误维持 UNRELATED；硬件/NATS/Kafka/双进程/长稳结论不在本次背书范围内。
