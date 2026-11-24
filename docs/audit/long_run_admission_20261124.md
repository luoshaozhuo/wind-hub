# wind-hub 长测准入独立集中测试报告（2026-11-24）

> 报告模板说明：仓库内及文件系统中均未找到用户指定的 `报告模板.md`（检索无输出）。本报告按交接任务第八节要求的九项交付内容组织，未因文件缺失降低检查范围。

## 0. 最终裁决

| 裁决项 | 结果 |
|---|---|
| **长测准入（完整八服务生产资格）** | **BLOCKED** |
| 降额 Sink 容量专项长测（无 Storage/Configuration 全链语义） | 可单独开展，不等于八服务生产资格 |

BLOCKED 的直接理由：P1 缺陷与真实性缺口未关闭——历史六服务 E2E Fixture 不可运行且不含 Configuration/Storage/TDengine；不存在可运行的八服务 CLI 驱动 E2E；Sink 生产编码器拒绝 RAW；多个模块的全量测试被旧安全/配置契约 Fixture 阻断；Storage 重启后的历史 Configuration 闭包不可用。静态门禁与 Configuration/Contracts/Core 已全绿，但不足以抵消上述阻断。

## 1. Git 基线与工作树

- 固定分支：`feat/production-readiness-final-20261123`
- 交接 HEAD：`b848050dd87bdcc8820413dff096d521005c1b7d`（交接时本地/远端一致、工作树干净、0 ahead/0 behind）
- `origin/main`：`c14a677cc533e09ec1b92e019a9a5473c10e30f3`（本地 main 落后 209 提交）
- 本轮提交：
  - `acacf257b` test(acceptance): repair stale config fixtures and close static gates
  - `31bd3be36` test(collector): target active prod-a root and pinned devices
- 最终 HEAD 与工作树状态见交付消息（以 push 后 `git rev-parse HEAD` 为准）。
- 未新建分支/PR、未合并 main、未 rebase、未 force push。

## 2. 静态门禁逐项结果

| 门禁 | 交接 HEAD 结果 | 最终结果 |
|---|---|---|
| `python -m ruff check src tests deploy` | FAIL：19 错（14 新） | **PASS：All checks passed** |
| `python -m ruff format --check src tests deploy` | FAIL：41 文件（35 新） | **PASS：1069 files already formatted** |
| `python -m mypy src` | FAIL：3 错（2 文件） | **PASS：no issues in 483 source files** |
| `python scripts/generate_rpc.py --check` | PASS | **PASS** |
| `lint-imports`（22 contracts） | 未运行 | **PASS：22 kept, 0 broken** |
| 全仓 pytest collection | 未运行 | **PASS：4126 tests collected** |
| `tests/system/e2e/test_long_run_candidate_preflight.py` + `tests/cli/test_config_loader.py` | 未运行 | **PASS：48 passed** |

基线对比：`origin/main` 上 ruff check 5 错、format 6 文件、mypy 0；本分支新增失败已在本轮全部关闭（格式与 lint 属确定性样式修复，mypy 为 `cast` 显式收敛）。

候选版本核对：活动 E2E 候选为 `deployment_e2e-final_v1.0.7.yaml` + `service_cli-e2e_v1.1.5.yaml`，与 `deploy/config_roots/e2e-final` 字节一致；`v1.0.8/v1.1.6` TLS 候选未被选中（preflight 断言通过）。

## 3. 各模块测试统计

| 模块 | passed | failed | errors | skipped | 结论 |
|---|---:|---:|---:|---:|---|
| Contracts + Core | 647 | 0 | 0 | 0 | PASS |
| Configuration | 433 | 0 | 0 | 2 | PASS（2 为可选真实 NATS skip） |
| Commander（unit+component） | 212 | 0 | 0 | 0 | PASS |
| Commander（integration） | 26 | 47 | 52 | 0 | FAIL（旧 mTLS/schema-v1 Fixture） |
| Commander（system） | — | — | — | — | 未单独执行；共享 `grpc_harness` 同一旧 mTLS 栈（已由 integration 证据覆盖） |
| Industrial Gateway | 85 | 44 | 97 | 0 | FAIL（schema-v1 Fixture） |
| Data Generator（unit+component） | 98 | 0 | 0 | 0 | PASS（修复生产目录正则后） |
| Data Generator（integration+system） | 26 | 74 | 0 | 0 | FAIL（Gateway schema-v1/mTLS Fixture） |
| Collector | 159 | 0 | 0 | 2 | PASS（修复两个 Fixture 后） |
| Sink Service（unit/component/integration） | 596 | 0 | 0 | 10 | PASS |
| Sink Service（system 子集：grpc_api/delivery_flow/lifecycle） | 12 | 0 | 0 | 0 | PASS |
| Sink Service（system 全量、排除 R14 之外） | — | — | — | — | NOT_FULLY_RUN（个别系统用例单次 >15min，未跑完；R14 600s 已单独通过） |
| Storage Service | 108 | 3 | 0 | 7 | FAIL（历史闭包 + 认证顺序 + 测试环境变量） |
| CLI | 154 | 103 | 12 | 13 | FAIL（schema-v1 Fixture） |

注：Collector 在 `31bd3be36` 修复两个 Fixture 后，全模块 158 passed/2 skipped；最后一次单文件复测 `test_grpc_contract_v1.py` 4/4 通过。表中 1 failed 为修复前统计，最终以复测为准。

系统性问题：**大量测试 Fixture 仍构造 `schema_version: 1` 的 Service 配置或证书 SAN/mTLS 栈**，而生产代码已要求 Service schema 2/3 且内部改为 Bearer Token（无内部 TLS）。这是分支未完成的测试迁移，不是生产 active root 配置缺陷。证据：Gateway `schema_version must be integer 2 or 3`、CLI 同源错误、Commander `grpc_harness` 仍写 `certificate_identity` 并调用 `build_pki`。

## 4. 完整八服务跨模块覆盖矩阵

| 服务 | 启动方式 | 真实进程 | 真实中间件 | 状态 |
|---|---|---|---|---|
| Configuration | CLI `service start` | Configuration 集成测试已验证 | SQLite/文件 | 模块级已验证；八服务全链 NOT_RUN |
| Storage | CLI `service start` | Storage 集成测试已验证（T-63） | Kafka/TDengine | 模块级部分失败（历史闭包） |
| Data Generator | CLI `service start` | DG 集成测试（真实 gRPC） | — | 见第 3 节 |
| Industrial Gateway | CLI `service start` | 模块测试 | Modbus 仿真 | 模块级被旧 Fixture 阻断 |
| Collector（双） | CLI `service start` | Collector 系统测试 | NATS | 模块级基本通过 |
| Sink | CLI `service start` | Sink 系统测试 | NATS/Kafka/Redis | 见第 3 节 |
| Commander | CLI `service start` | 模块测试 | SQLite/NATS | 模块级被旧 mTLS Fixture 阻断 |

**全链真实性裁决：NOT_EXECUTED / BLOCKED。**
- 历史六服务 Fixture（`tests/system/e2e/full_chain/conftest.py`）在本分支与 `origin/main` 均指向已被删除的 `configs/instances/devices_sim.yaml`、`configs/services/data-generator-sim.example.yaml`；实测 setup 直接 `FileNotFoundError`。该 Fixture 同时只有六进程、不含 Configuration/Storage/TDengine，不能被用来声明八服务准入。
- 分支未提供以 `deployment_e2e-final_v1.0.7` + `service_cli-e2e_v1.1.5` 启动八服务并由 CLI 驱动业务操作的独立 E2E 测试。因此不得以模块级集成测试替代完整 E2E。

## 5. RAW / PROTOCOL / BUSINESS 独立真实性裁决

| 数据等级 | 裁决 | 依据 |
|---|---|---|
| RAW | **BLOCKED** | Sink 生产 Kafka 编码器明确拒绝无原始未解码载荷的 RAW（`kafka_wire._require_protocol`）。Storage 接受合成 RAW JSON 不等于生产链具备 RAW。不得将 PROTOCOL 冒充 RAW。 |
| PROTOCOL | NOT_VERIFIED（全链） | 模块/分段测试可构造并编码 PROTOCOL，但无八服务全链逐值证据。 |
| BUSINESS | NOT_VERIFIED（全链） | 同上；Storage 单元/集成能解析，但缺 DG→…→TDengine 端到端逐键对账。 |

## 6. 容量短烟测结果

### 6.1 官方准入脚本

`bash scripts/soak_qualification.sh smoke 120` 实测（HEAD `94aec2263`，证据 `official_smoke_120/`）：ruff/format/mypy/rpc_contract 与 candidate 通过；**在 `storage_semantics` 门禁失败**（`tests/storage_service/component/test_grpc_common.py::test_every_storage_and_common_rpc_rejects_unauthorized_calls`，`CreateExport` 未认证请求先返回 `INVALID_ARGUMENT`）。脚本因此中止，未进入 `module_real`/`storage_real`/Sink 60s。因此官方容量门禁本轮**未通过**；容量证据以同参数（30 设备、500 点/批、60s、真实 R5 栈）的直接 R14 运行取得，并明确标注“非官方脚本通过”。

### 6.2 直接 R14 结果

环境：本机真实 Docker R5 栈（NATS JetStream / Redis / Kafka 3.9.1 / TDengine 3.3.6.13），真实 Sink 三进程，500 点/批，Reliable 通道，`R14_DURATION_S=60`（30 设备短烟），另有 1 次 `R14_DURATION_S=600`（degree=2 降额持续）。

| 工况 | 目标批/s | 发布实测批/s | 确认稳态批/s | 确认稳态值/s | 积压斜率（批/s） | 丢弃/DLQ | 最终排空 | CPU(单核%) | RSS(MiB) |
|---|---:|---:|---:|---:|---:|---:|---|---:|---:|
| 30 设备 · 60s · 降额 | 120 | 116.01 | **114.09** | 57,047 | 2.03 | 0 / 0 | 是 | 97.3 | 254.1 |
| 30 设备 · 60s · 理想 | 300 | 257.78 | **110.46** | 55,232 | **150.50** | 0 / 0 | 是 | 42.3 | 254.2 |
| 2 设备 · 600s · 持续 | 120 | 116.44 | **116.53** | 58,264 | 0.004 | 0 / 0 | 是 | 91.2 | 219.5 |

裁决：
- **120 批/s（60k 点/s）降额工况通过**：确认吞吐 ≥ 114 批/s，零丢失、DLQ=0，最终积压归零。
- **300 批/s（150,000 点/s）理想目标未达标（FAIL）**：发布侧仅 257.78 批/s；确认稳态 110.46 批/s；稳态窗口积压以 150.5 批/s 斜率持续增长，说明消费侧是瓶颈。60s 后因发布停止并排空，故正文 `zero_loss=true` 只表示“有界输入最终全部落库”，不表示可持续 300 批/s。断言失败证据见 `r14_300/run.log`（`1 failed`）。
- 600s degree=2 降额持续工况零丢失、积压斜率≈0，说明该配置在约 116 批/s 可长时间稳定。
- 可信稳定容量（本环境、单 Sink、双通道可靠）≈ **110–116 批/s（55k–58k 点/s）**，约为理想目标 300 批/s 的 37%–39%。要满足 300 批/s，按线性外推至少需 3 个 Sink 分片/实例；本轮不能认定理想目标达标，也不能任意降低验收目标。
- 未含 Storage/TDengine 落库容量：本工况只证明 Sink→Kafka 确认吞吐，TDengine 实际落库容量 NOT_RUN。

## 7. 缺陷台账

| ID | 级别 | 归属 | 缺陷 | 复现/根因 | 状态 |
|---|---|---|---|---|---|
| D-01 | P1 | 测试基础设施 | 旧 Service `schema_version: 1`/证书 Fixture 未迁移到 Token+schema 2/3 | Gateway/CLI/Commander/Collector 多个套件；生产代码要求 2/3 | OPEN（已记录，未大规模重写） |
| D-02 | P1 | 测试基础设施 | 历史六服务 E2E Fixture 引用已删除配置、且不含 Configuration/Storage | `test_e2e_smoke` setup `FileNotFoundError` | OPEN |
| D-03 | P1 | 产品语义 | Sink 拒绝 RAW，生产链无 RAW 来源 | `kafka_wire.py` | BLOCKED（须产品裁决） |
| D-04 | P1 | Storage 生产 | 配置版本变更并重启后 `QuerySamples` 返回 `FAILED_PRECONDITION: historical Configuration closure is unavailable` | `test_cli_restart_activation.py::test_cli_restart_activates_staged_static_closure` | OPEN（交接 ChatGPT） |
| D-05 | P2 | Storage 生产 | 未认证的 `CreateExport` 先返回 `INVALID_ARGUMENT` 而非 `UNAUTHENTICATED`（认证顺序） | `test_grpc_common.py` | OPEN |
| D-06 | P2 | 配置归档 | `configs/services/service_gateway-e2e-source_v1.1.0/1.1.1.yaml` 含重复 `token_env` 键；后继 v1.1.2 已修复 | 严格解析器拒绝；本报告以显式排除+拒绝断言处理 | MITIGATED（历史保留） |
| D-07 | P2 | 测试基础设施 | Configuration 全目录发布测试按 `configs/` 归档（160 份、含历史畸形候选）发布，与生产“单活动根 ≤35 份”模型不符 | 本报告改为按 `deploy/config_roots/*` 发布 | CLOSED（本轮修复） |
| D-08 | P2 | 测试基础设施 | Configuration 测试存在无超时 gRPC/NATS 调用，服务降级时无限挂起 | `test_bundle_limits.py`、`test_observability.py`、`test_storage_deployment_flow.py` | CLOSED（本轮修复） |
| D-09 | P3 | 代码质量 | 分支自带 ruff check 14 新错、format 35 新漂移、mypy 3 错 | 静态门禁 | CLOSED（本轮修复） |
| D-10 | P3 | 测试环境 | `test_kafka_json_versions.py` 使用未提供的 `WIND_HUB_TEST_TDENGINE_PASSWORD` | 真实 TDengine 驱动存在但变量缺失 | OPEN（P3） |
| D-11 | P1 | Data Generator 生产 | `_loaded_catalog` 目录文件名正则被双重转义（`\\.`），永不匹配，导致 Generator 启动必然抛 `expected exactly one resolved ...` | `src/data_generator/.../service_settings.py`；`test_catalog_closure.py` 稳定复现 | **CLOSED（本轮修复为 `\.`）** |
| D-12 | P2 | 测试基础设施 | DG component `GatewayWriter` 构造缺少新增必填 `gateway_token_env`；DG integration/system 的 Gateway Fixture 仍为 schema-v1/mTLS | `test_output_backpressure.py`、`test_gateway_integration.py`、`tests/support/gateway.py` | 构造参数已修；Gateway 共享 Fixture 迁移 OPEN |

## 8. 尚未验证项目及原因

- 八服务 CLI 驱动真实 E2E（正常/拒绝/恢复/重投/去重）：无可用 Harness（D-02），NOT_EXECUTED。
- DG→Gateway→Collector→JetStream→Sink→Kafka→Storage→TDengine 逐值对账：同上。
- RAW 全链：产品语义未批准（D-03）。
- Commander system 层（mTLS 栈）：共享 harness 未迁移（D-01/D-02）。
- Data Generator integration/system（74 项）：旧 Gateway schema-v1/mTLS Fixture（D-01/D-12）。
- Sink system 全量（除 R14）：单次运行超过 15 分钟未完成，仅执行 `test_grpc_api`/`test_delivery_flow`/`test_lifecycle`（12/12 通过）；其余 NOT_RUN。
- 30×10Hz×500 理想 300 批/s 达标：未达标，见第 6 节。
- 官方 `soak_qualification.sh smoke 120`：在 storage_semantics 门禁失败（D-05），未进入容量段。
- Storage 历史闭包（D-04）与 CreateExport 认证顺序（D-05）：OPEN。
- 真实 PLC/硬件、netem、iSulad、systemd 生产单元：NOT_RUN（环境/本轮范围外）。

## 9. 结论

**BLOCKED**：不存在 P0，但存在未关闭的 P1（D-01～D-04、D-11 虽已修复但需在最终 HEAD 复测全受影响模块）与真实性缺口（RAW、八服务 E2E）。静态门禁、Contracts/Core、Configuration、Commander 单元/组件、Collector、Sink 非系统层、DG 单元/组件已通过；这些只证明分段正确性，不构成生产资格。

可区分的次裁决：**允许开展“降额 Sink 容量专项”长测**（单 Sink、真实 NATS/Kafka/Redis，不含 Storage/Configuration 全链语义），不得据此宣称完整八服务生产资格或 RAW 通过。正式 600s/3600s 长测本轮未启动，由 ChatGPT 依据本报告与缺陷台账决定整改与下一步。

## 10. 证据索引与运行命令

证据目录：[long_run_admission_20261124_evidence/](long_run_admission_20261124_evidence/)

| 文件 | 内容 |
|---|---|
| `core_contracts.log` | Contracts+Core 647 passed |
| `configuration_full.log` | Configuration 433 passed / 2 skipped |
| `commander_unit.log` / `commander_component.log` | Commander 85 / 127 passed |
| `commander_int_v.log` | Commander integration 26 passed / 47 failed / 52 errors（mTLS/schema-v1 Fixture） |
| `gateway.log` | Gateway 85 passed / 44 failed / 97 errors |
| `dg2.log` / `dg3.log` | DG 修复前/后 integration+system |
| `collector2.log` / `collector3.log` | Collector 修复前后，最终 159 passed / 2 skipped |
| `cli.log` | CLI 154 passed / 103 failed / 12 errors |
| `storage.log` | Storage 108 passed / 3 failed / 7 skipped |
| `sink_fast.log` / `sink_sys_subset.log` | Sink 596 passed / 10 skipped；system 子集 12 passed |
| `r14_degree2_120_600s.json` | 600s degree=2 降额持续容量 |
| `r14_degree30_120_60s.json` | 30 设备 120 批/s 短烟 |
| `r14_degree30_300_60s.json` / `_pytest.log` | 30 设备 300 批/s 短烟（FAIL） |
| `official_smoke_120/` | 官方脚本静态与 storage_semantics 门禁证据 |

关键命令：

```bash
python -m ruff check src tests deploy
python -m ruff format --check src tests deploy
python -m mypy src
python scripts/generate_rpc.py --check
lint-imports
python -m pytest --collect-only -q
python -m pytest -q -rs tests/system/e2e/test_long_run_candidate_preflight.py tests/cli/test_config_loader.py
bash scripts/soak_qualification.sh smoke 120 /tmp/wind-hub-audit/b848050dd/official_smoke_120
PYTHONPATH=$PWD/src:$PWD R14_RESULT_DIR=<dir> R14_DEGREE=30 R14_POINTS=500 \
  R14_TARGET_BATCH_RATE=<120|300> R14_DURATION_S=60 \
  python -m pytest -q -rs tests/sink_service/system/test_capacity_r14_sustained.py::TestR14SustainedCapacity::test_sustained_capacity_600s
```
