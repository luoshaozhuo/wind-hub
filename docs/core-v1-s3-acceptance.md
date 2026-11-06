# Core v1_20 S3 集中独立验收报告

- 验收对象：仓库 `luoshaozhuo/wind-hub`，分支 `refactor/core-v1`
- 验收起始 HEAD：`15f738ab9cb4ee1b7b2c72448883952f41503cfa`（与授权起点一致）
- 设计基线：[Core_软件设计_v1_20.md](design/Core_软件设计_v1_20.md)（第 14 章测试设计、追溯与验收）
- 验收角色：S3 仅执行测试、静态检查与测试侧修复；**未修改任何生产代码**。生产缺陷保留失败证据，移交 ChatGPT 修复。
- 执行环境：WSL2（Linux 6.18.33.2-microsoft-standard-WSL2），conda 环境 `wind-hub`，Python 3.14.7
- 报告日期：2026-11-06

> 结论速览：**FAIL**。核心库 `tests/core` 自身已收敛到 508 passed / 1 failed（该 1 项为 origin/main 既存门禁失败），但设计 §14 规定的四层测试组织、ST-01～ST-60 可定位 System 测试、场景 Marker 体系整体未落地；跨模块存在 P0 级 `YamlConfigAdapter` 删除后调用方未迁移（全仓 45 个测试模块收集失败，其中 24 个属该 P0，余为 main 既存 pb2 与环境缺包）；五条静态门禁中四条失败。详见第 8、10 节。

---

## 1. Git HEAD、环境与依赖版本

### 1.1 Git 状态

| 项 | 值 |
|---|---|
| 分支 | `refactor/core-v1` |
| 验收起始 HEAD | `15f738ab` `refactor(core): remove broad typing suppressions from new observability interceptors` |
| 工作区变更 | 仅测试代码（22 改 + 2 删，清单见第 9 节），生产代码 `src/`、`scripts/` 零修改 |
| 合并/变基/强推 | 均未执行；未创建 PR |

### 1.2 运行环境与关键依赖

| 组件 | 版本 | 备注 |
|---|---|---|
| Python | 3.14.7 | conda env `wind-hub`（`scripts/dev.py env` 检查通过） |
| pytest | 8.4.2 | |
| pytest-asyncio | 0.24.0 | |
| pyads | 3.6.0 | ADS 协议栈；测试对端为 pyads AdsTestServer，非 TwinCAT |
| pymodbus | 3.15.0 | Modbus TCP 真实服务端/客户端同栈 |
| hat-drivers / hat-aio | 0.10.10 / 0.7.13 | IEC104 独立协议栈 |
| grpcio / grpcio-tools / protobuf | 1.84.0 / 1.81.1 / 6.33.6 | |
| prometheus_client | 0.21.1 | 指标依赖已装 |
| PyYAML | 6.0.3 | |
| ruff / mypy / import-linter | 0.8.6 / 1.20.2 / 2.15 | 静态门禁工具 |
| prompt_toolkit | **未安装** | 致 2 个 CLI 测试模块收集失败（环境缺失，见 D-09） |
| opentelemetry-api/sdk/exporter-otlp | **未安装且不可安装** | `poetry.lock` 无任何 opentelemetry 条目，tracing extra 与锁文件不一致（见 D-07） |

---

## 2. 执行命令与退出码汇总

所有命令在仓库根目录、`wind-hub` 环境下执行；原始证据留存于 `/tmp/s3-acceptance/`。

| # | 命令 | 退出码 | 结果摘要 |
|---|---|---|---|
| 1 | `python -m ruff check src tests scripts` | 1 | 修复测试侧后 **88 errors**（全量基线 102，其中 tests/ 14 已由 S3 清零） |
| 2 | `python -m ruff format --check src tests scripts` | 1 | **20 files would reformat**，全部位于 `src/`（tests/ 已清零） |
| 3 | `python -m mypy src/core` | 1 | **27 errors in 6 files**（60 source files） |
| 4 | `python -m mypy src/configuration` | 0 | Success: no issues found in 18 source files |
| 5 | `lint-imports` | 1 | 配置错误：`include_external_packages=True` 缺失 |
| 6 | `pytest tests/core --strict-markers --collect-only -q` | 0 | **509 collected，0 error**（验收初期为 516 节点 / 13 收集错误） |
| 7 | `pytest tests/core -ra` | 1 | **508 passed, 1 failed**（失败为 main 既存 pb2 门禁） |
| 8 | `pytest tests/core/unit -ra` | 0 | 37 passed（仅 7 个 config 测试文件；四层目录名存实亡） |
| 9 | `pytest tests/core/component -ra` | 4 | path not found，no tests ran |
| 10 | `pytest tests/core/integration -ra` | 4 | path not found，no tests ran |
| 11 | `pytest tests/core/system -ra` | 4 | path not found，no tests ran |
| 12 | `pytest tests --collect-only -q` | 2 | **1719 tests collected, 45 errors**（S3 修复后当前值；修复前为 48） |
| 12b | `pytest tests --continue-on-collection-errors --junitxml=all-junit-cont.xml` | 1 | **1517 passed, 16 failed, 33 skipped, 3 deselected，174 junit error 条目，1568.69s**（该跑次启动于 S3 三项跨模块修复之前，含 48 个收集错误模块；修复后收集错误为 45，见 2.2） |
| 13 | 分协议 `pytest tests/core/protocol/{ads,modbus,iec104} -m 'real_service and not hardware'` | 0 | ADS 3 / Modbus 14 / IEC104 18，**35 passed** |
| 14 | `pytest tests/core -m reliability` | 0 | 26 passed, 483 deselected |
| 15 | `pytest tests/core -m safety / performance / concurrency / reload / 'timeout or deadline' / 'recovery or restart' / hardware` | 0 收集但 **0 选中** | 设计 §14.6.3 专题/场景筛选在 tests/core 全部选空（hardware 为预期选空） |

### 2.1 全量套件结果（收集错误不中断模式）

最终跑次（junit：`/tmp/s3-acceptance/all-junit-cont.xml`）：

| 指标 | S3 修复前基线 | S3 修复后最终跑次 |
|---|---|---|
| passed | 1416 | **1517**（+101） |
| failed | 46 | **16**（27 个 core 失败由 S3 测试修复消除，3 个 web_server 导入错误随收集分类变化） |
| skipped | 33 | 33（全部为显式环境守卫，无 xfail，全仓 xfail=0） |
| 收集错误模块 | 187（首次基线口径）/ 48（默认收集中断口径） | **45**（当前 collect-only 实测） |
| 耗时 | 1631s | 1569s |

16 个失败的归因见第 8 节 D-08/D-10/D-12a-c 与 7.3；174 个 junit error 条目中 48 个为收集错误模块本身，其余 126 个为 CLI/system e2e 子进程因同一根因（P0/pb2/缺二进制）级联产生的 error 记录，非独立缺陷。

### 2.2 当前 45 个收集错误的固定归因

| 根因 | 模块数 | 归因 |
|---|---|---|
| `YamlConfigAdapter` / `core.infrastructure.config.adapter` 已删（D-01） | 24 | 分支 P0 |
| `core.application.config_diff` 已迁（D-02，且同时被 D-01 链阻断） | 2 | 分支 |
| `contracts.collector_v2_pb2` 不存在（D-10） | 17 | main 既存 |
| `prompt_toolkit` 未安装（D-09） | 2 | 环境 |

---

## 3. 静态检查与架构门禁结果

### 3.1 Ruff Lint（88 errors，EXIT=1）

按规则分布（`src/` + `scripts/`，tests/ 已清零）：

| 规则 | 数量 | 性质 |
|---|---|---|
| I001 unsorted-imports | 47 | 格式类，大量集中在本分支新增/重构模块 |
| E501 line-too-long | 23 | 格式类 |
| SIM117 multiple-with | 6 | 简化建议 |
| SIM105 suppressible-exception | 5 | 简化建议 |
| F401 unused-import | 3 | 含 `observability.py` 未用 `Sequence/get_request_id`、`tracing.py` 未用 `time` |
| UP038 non-pep604-isinstance | 2 | |
| F821 undefined-name | 1 | **`scripts/protocol_servers.py:550` `YamlConfigAdapter` 无导入直接引用（P0 家族，D-01）** |
| B905 zip-without-strict | 1 | |

未使用任何 `noqa`/ignore/skip-file 掩盖；S3 仅对 `tests/` 执行了 `ruff --fix`（纯 import 排序）与单文件 format，变更列入第 9 节。

### 3.2 Ruff Format（20 files，EXIT=1）

20 个待格式化文件全部在 `src/`（core 新模块、sink_service 适配器等），属生产代码，S3 未触碰。

### 3.3 Mypy（src/core：27 errors / EXIT=1；src/configuration：干净）

按归因分三类：

1. **OpenTelemetry 缺失类 11 项**（import-not-found）：`diagnostic/tracing.py` 7、`diagnostic/observability.py` 2、`rpc/client_correlation.py` 2。根因是 tracing extra 未安装且锁文件不可安装（D-07）；生产代码以函数内 try/except ImportError 降级，运行期语义诚实，但静态门禁在当前环境必然失败。
2. **grpc 拦截器子类无 stub 类 6 项**（Class cannot subclass ... grpc.aio interceptor）：tracing.py:33、observability.py:312、client_correlation.py:147/156/165/177。
3. **真实类型错误 10 项（分支生产缺陷，D-05）**：
   - `protocol/ads/subscription.py:255`：`self._blocks` 声明为 tuple，赋值 list；
   - `protocol/ads/driver.py:183`、`protocol/iec104/driver.py:187`：`resource_state` 三元表达式推断为 `str`，与 `Literal['READY','DEGRADED','FAULTED']` 不兼容；
   - `protocol/iec104/driver.py:249`：`type_id` 为 `str | None` 时直接 `.startswith`（union-attr）；
   - `diagnostic/observability.py:120`：`LogRecord` 无 `request_id` 属性（attr-defined）；
   - `diagnostic/observability.py:452`：`_Recorder | None` 未判空即 `record_drop`（union-attr）；
   - `rpc/client_correlation.py:202-205`：4 个拦截器构造参数 `dict[...]` 传给 `bool` 形参（arg-type ×4）。

### 3.4 import-linter（EXIT=1）

```
The top level configuration must have include_external_packages=True
when there are external forbidden modules.
```

合约配置自身不可用，所有契约未实际执行（D-06）。

### 3.5 架构守护测试（tests/core/architecture，4 文件 25 节点）

- `test_core_layers.py` 9 项：S3 已将断言更新到 v1_20 目标布局（generic_yaml 存在、application/port/config.py 与 domain/config 不存在），全部通过；
- `test_new_packages_isolation.py` 5 项、其余通过；
- `test_rpc_boundary.py::test_pb2_only_in_allowed_zones` **失败**：7 个 `_pb2` 越界引用（collector/assembly.py、industrial_gateway/infrastructure 下 6 处）。已用 `git show origin/main:<file>` 逐一证实相同引用在 main 即存在 → **origin/main 既存失败（UNRELATED）**，未做白名单放宽。

---

## 4. 四层测试结果（设计 §14.1 / §14.6.2）

### 4.1 目录结构与设计要求的偏差

设计要求按 `tests/core/{unit,component,integration,system}/` 四层组织。实际：

- `tests/core/` 按**技术包**组织：`application/ architecture/ config/ diagnostic/ domain/ infrastructure/ protocol/{ads,modbus,iec104}/ rpc/ unit/config/`；
- `component/`、`integration/`、`system/` 三个层级目录**不存在**（pytest 退出码 4）；
- 存在的 `tests/core/unit/` 仅含 7 个 config 测试（37 节点），并非全量 unit 归集目录；
- 设计表 14-4 列出的 21 个目标文件**全部不存在**（12 个 unit/component/integration 目标文件逐一核实 MISSING；9 个 system 目标文件中仅同名文件零散存在于其他包，路径均不匹配）。

### 4.2 tests/core 实际执行结果

| 维度 | 数值 |
|---|---|
| 收集节点 | 509（strict-markers 下 0 未注册标记、0 收集错误） |
| 通过 | 508 |
| 失败 | 1（main 既存 pb2 边界守护，见 3.5） |
| 跳过/xfail | 0（不存在以 skip/xfail 冒充 PASS 的情况） |
| 层级 Marker 分布 | unit 423 / contract 38 / integration 22 / **component 0 / system 0** |
| 无任何层级 Marker 的节点 | **26**（protocol 的 fault_injection/concurrency/stability 文件只被打专题 `reliability`，违反“每项测试有且仅有一个层级 Marker”） |

---

## 5. ST-01～ST-60 追溯矩阵

### 5.1 审计方法与裁决口径

1. 设计 §14.6.5 约定测试函数名包含 `st_XX`。全仓正则 `st_[0-9]{2}` 审计（剔除 `test_64bit` 这类假阳性)：**真实 ST 命名测试 = 0**。
2. 设计 §14.3 执行门槛：ST-31～60 须落地为独立可定位 System 测试，且 mock 对端不能代替协议互操作证据。
3. 因此**形式裁决上 60 个 ST 全部为 NOT_RUN（System 测试不存在）**；下表同时记录 S3 核实到的**低层级等价技术覆盖**及其真实结果，供 ChatGPT 评估补测缺口，不等同于 ST 通过。

状态列：`等价覆盖`＝存在通过的低层级测试覆盖主合同；`部分`＝仅覆盖部分分支/仅 mock；`缺失`＝无测试；`NOT_RUN`＝环境/硬件阻断。

| ST | 设计要点 | 最接近的现存测试（层级/真实性） | 现存结果 | 形式 ST 裁决 |
|---|---|---|---|---|
| ST-01 | YAML 往返、空/非空映射 | `unit/config/test_repository_config_files.py`（经 read_yaml 读版本化仓库）；**generic_yaml 无直接测试** | PASS（间接） | NOT_RUN（部分） |
| ST-02 | 重复键/错键型/非法值定位 | `tests/cli/test_config_loader.py::test_duplicate_yaml_keys_rejected` 等（CLI 进程级）；config/validation 4 项 | PASS（CLI 级） | NOT_RUN（部分） |
| ST-03 | 嵌套差异/列表/类型变化 | `tests/cli/test_diff_e2e.py` 5 项（进程级）；`configuration.diff` 无 core 单测 | PASS（CLI 级） | NOT_RUN（部分） |
| ST-04 | 替换前故障、临时文件清理 | 无 | — | **NOT_RUN（缺失）** |
| ST-05 | 替换后 fsync 故障、不虚报回滚 | 无（生产 `generic_yaml.py:153,158` 有 fsync 但零测试） | — | **NOT_RUN（缺失）** |
| ST-06 | ADS 连接/健康/读写/原生通知 | `protocol/ads/test_integration.py` 2 项（pyads AdsTestServer，真实 AMS/TCP 回环，非 TwinCAT） | PASS | NOT_RUN（部分；hardware NOT_RUN） |
| ST-07 | Modbus 四区域/FC01-06/16 | `protocol/modbus/test_integration.py` 7 项（真实 pymodbus TCP Server） | PASS | NOT_RUN（等价覆盖，缺 system 定位） |
| ST-08 | IEC104 读/监听/召唤 | `protocol/iec104/test_integration.py` 7 项（hat 独立栈控制对端） | PASS | NOT_RUN（等价覆盖，缺 system 定位） |
| ST-09 | 三协议超时/取消/断连、UNKNOWN 不重发 | modbus fault_injection 6（含 write_ack_lost→UNKNOWN）、iec104 fault 3、ads fault 4、三协议 concurrency 6 | PASS | NOT_RUN（部分） |
| ST-10 | 观测开关业务一致 | 无开关对照测试；仅 cli observability e2e 相邻覆盖 | — | NOT_RUN（部分） |
| ST-11 | 并发关联、后端故障不阻塞 | `infrastructure/test_correlation.py` 5、`test_grpc_correlation.py` 6；无后端故障注入 | PASS（部分） | NOT_RUN（部分） |
| ST-12 | 线程池重复装配/冲突 | `infrastructure/test_executor.py` 3（幂等/冲突保留旧池/事件循环隔离） | PASS | NOT_RUN（等价覆盖） |
| ST-13 | Watchdog 重连/停止/代际竞争 | `application/test_watchdog.py` 16（含并发不重复重连、停止取消在途探测） | PASS | NOT_RUN（等价覆盖） |
| ST-14 | 跨模块对象交换语义 | domain 套件 + `unit/config/test_shared_snapshot.py` | PASS | NOT_RUN（部分） |
| ST-15 | 非法引用/边界稳定拒绝 | domain/test_task 14、config/validation、business_points 5 | PASS | NOT_RUN（等价覆盖） |
| ST-16 | TCP/ICMP/端口诊断三态 | `diagnostic/test_network.py` 42 + `tests/cli/test_network_e2e.py` 5 | PASS | NOT_RUN（等价覆盖） |
| ST-17 | 防火墙证据不足→UNKNOWN | `diagnostic/test_firewall.py` 16 | PASS | NOT_RUN（等价覆盖） |
| ST-18 | 协议只读诊断、独立 Session | `diagnostic/test_protocol_diag.py` 15（S3 重写；FakeDriver，写/订阅类方法显式爆炸） | PASS（mock 级） | NOT_RUN（部分；真实对端只读诊断未测） |
| ST-19 | 诊断样本/间隔/Deadline 极值 | `diagnostic/test_benchmark.py` 16 + protocol_diag 边界 3 参数化 | PASS | NOT_RUN（等价覆盖） |
| ST-20 | ADS Symbol/Notification 热更新 | **ADS 无任何 update_point_table 测试** | — | **NOT_RUN（缺失）** |
| ST-21 | Modbus 缓存命中/淘汰/相关点更新 | `modbus/test_functional.py::test_modbus_read_plan_cache_reuse_eviction_and_reload`（S3 重写，fake session） | PASS（mock 级） | NOT_RUN（部分） |
| ST-22 | IEC104 更新并发/Listener 队列满 | iec104 functional 2 更新测试（fake session）+ concurrency 2（真实栈）；**队列满过载未测** | PASS（部分） | NOT_RUN（部分） |
| ST-23 | Watchdog 两次失败/指数退避/恢复回调失败 | watchdog 16 项含单次失败不重连、失败证据重置、回调失败可观察；退避时钟序列未注入验证 | PASS（部分） | NOT_RUN（部分） |
| ST-24 | 整体 Deadline、I/O 与关闭并发 | concurrency 6 项；`deadline` 标记测试 = 0，无整体截止时间断言 | PASS（部分） | NOT_RUN（部分） |
| ST-25 | 新/变/删/不变分类、逐点错误 | 仅 SUCCESS/VALIDATION_FAILED 两类被测（2+1 处），分类合同未断言 | — | NOT_RUN（部分） |
| ST-26 | NOT_READY/TIMEOUT/部分应用真实性 | 无（代码枚举存在 8 态，测试仅触达 2 态） | — | **NOT_RUN（缺失）** |
| ST-27 | 重连缓存保留、不调 update_point_table | watchdog 重连测试存在；“重连不触发点表更新”无显式断言 | PASS（部分） | NOT_RUN（部分） |
| ST-28 | 协议/单位/型号/模板校验 | `unit/config/` 7 文件 37 节点（版本化模板安全、引用图、生效设备） | PASS | NOT_RUN（等价覆盖） |
| ST-29 | 诊断 JSON/异常/关闭失败 | `application/test_diagnostic_result.py` 4 + protocol_diag 关闭失败降级 UNKNOWN | PASS | NOT_RUN（等价覆盖） |
| ST-30 | 只改装配启用观测、业务零改 | 无对应装配测试 | — | **NOT_RUN（缺失）** |
| ST-31 | ADS SumRead 边界/跨块/逐点 BAD | ads codec 27 + functional 分块测试（纯计算/fake）；真实对端 SumRead 边界未测 | PASS（mock 级） | NOT_RUN（部分） |
| ST-32 | ADS SumWrite 三态逐点保留 | `test_sum_write_error_vector_is_exact_length` 等（fake，非事务语义） | PASS（mock 级） | NOT_RUN（部分） |
| ST-33 | Notification 编块/空洞/重叠拒绝 | functional 相对偏移/容量拆分/重叠拒绝 | PASS（mock 级） | NOT_RUN（部分） |
| ST-34 | Notification 周期/队列容量/溢出 | fault_injection 订阅 2（mock）+ planner soak；溢出策略可观测性未测 | PASS（部分） | NOT_RUN（部分） |
| ST-35 | ADS 不支持操作立即拒绝零流量 | `test_ads_rejects_unsupported_interrogation` + capabilities；零出站流量未验证 | PASS（部分） | NOT_RUN（部分） |
| ST-36 | Modbus 同/异 Unit 区分组合并 | functional 规划器 + integration 原生分组读 | PASS | NOT_RUN（等价覆盖） |
| ST-37 | max_gap=0/2/8、稀疏重叠 | `test_read_planner_respects_span_and_gap` 等 | PASS（mock 级） | NOT_RUN（部分） |
| ST-38 | 寄存器上限/越限 | `test_invalid_address_span_is_rejected` | PASS（mock 级） | NOT_RUN（部分） |
| ST-39 | 重复点/缓存容量 0/1/64 | 重复点规划测试 + LRU 有界（实测上限 32）；**0/1/64 取值未测** | PASS（部分） | NOT_RUN（部分） |
| ST-40 | 动态调参后旧计划失效 | 无动态配置变更测试 | — | **NOT_RUN（缺失）** |
| ST-41 | FC05/06/16 回显/异常码/超时 | integration FC 全套 + fault `write_ack_lost_returns_unknown_without_replay`（真实栈） | PASS | NOT_RUN（等价覆盖） |
| ST-42 | Modbus 未实现操作零流量拒绝 | contract 能力拒绝 + `failed_local_encoding_never_sends_to_plc` | PASS（部分） | NOT_RUN（部分） |
| ST-43 | C_RD 关联/错误 COT/CA/IOA 隔离 | functional 29（编解码/索引）+ fault `wrong_cot_and_wrong_ca_cannot_complete_active_read` | PASS | NOT_RUN（部分） |
| ST-44 | listen 自发上送/过滤/批次质量 | integration `test_iec104_spontaneous_listen`（真实栈）+ concurrency 路由测试 | PASS | NOT_RUN（等价覆盖） |
| ST-45 | Listener 容量/满队列/关闭 | 无 | — | **NOT_RUN（缺失）** |
| ST-46 | 站召/组召/计数器/多 CA 锁 | integration 总召 2 项 + functional CA 索引/锁复用；多 CA 分锁未在真实对端验证 | PASS（部分） | NOT_RUN（部分） |
| ST-47 | 召唤部分完成/超时/夹杂自发 | 无 | — | **NOT_RUN（缺失）** |
| ST-48 | 直控/选择执行/ACT_CON/TERM | integration 直控、选择执行、五命令类型 ACT_TERM 3 项（真实栈） | PASS | NOT_RUN（等价覆盖） |
| ST-49 | IEC104 批处理方法明确拒绝 | `test_iec104_batch_methods_reject_without_network` | PASS（mock 级） | NOT_RUN（部分） |
| ST-50 | 全操作能力/重入/状态一致 | registry 9 + 三协议 contract 套件 + integration 生命周期 | PASS | NOT_RUN（部分） |
| ST-51 | 新配置键生效/非法参数拒绝 | `protocol/test_config_contract.py` 16（ADS net_id/route、IEC104 窗口、Modbus slave_id） | PASS | NOT_RUN（等价覆盖） |
| ST-52 | 点表更新与 close 并发 | 三协议 concurrency 6（modbus/iec104 真实栈，ads mock） | PASS（部分） | NOT_RUN（部分） |
| ST-53 | 更新结果合同全字段/全状态 | 仅 SUCCESS ×2、VALIDATION_FAILED ×1；8 态中 6 态零测试 | — | NOT_RUN（缺失为主） |
| ST-54 | 提交前失败/超时/取消/清理故障 | 无 | — | **NOT_RUN（缺失）** |
| ST-55 | 提交后注销失败/DEGRADED/残留资源 | 无（`residual_resources` 字段无任何测试引用） | — | **NOT_RUN（缺失）** |
| ST-56 | ADS 通知与更新并发、代次门阻断 | 无 | — | **NOT_RUN（缺失）** |
| ST-57 | IEC104 旧代次迟到帧隔离 | fault `test_close_idempotent_and_old_receive_noop` 相邻；代次路由无显式测试 | PASS（部分） | NOT_RUN（部分） |
| ST-58 | active_version 首为 1/递增/重连不增/重建重置 | iec104 functional 断言首提交=1；modbus 断言=2；重连/重建场景未测 | PASS（部分） | NOT_RUN（部分） |
| ST-59 | 模板新键/优先级/版本不可变 | `unit/config/test_versioned_template_security.py` 17 等 | PASS | NOT_RUN（等价覆盖） |
| ST-60 | 观测装配幂等/冲突/gRPC 四流/出口故障 | grpc_correlation 四流形态 4（Request-ID 语义，非 Trace）；**装配冲突、OTLP、出口故障未测** | PASS（部分） | NOT_RUN（部分；OTLP 环境 NOT_RUN） |

**统计**：形式裁决 NOT_RUN 60/60。其中低层级等价/部分覆盖 48 项，**完全缺失 12 项**（ST-04、05、20、26、30、40、45、47、53、54、55、56；ST-53 以缺失为主）。

---

## 6. Marker 完整性与筛选结果

### 6.1 注册情况（pyproject.toml）

设计表 14-3/14-4 所需 Marker 已全部注册：层级 5（unit/component/contract/integration/system）、专题（reliability/safety/soak/performance）、场景（normal/invalid_input/fault_injection/real_service/hardware/timeout/network_failure/concurrency/overload/reload/recovery/restart/deadline/no_targets/conflict/network/docker/root 等）。`--strict-markers` 收集零违例。

### 6.2 实际施加情况（收集期审计插件，证据 `/tmp/s3-acceptance/marker-audit.log`）

| 审计项 | 结果 |
|---|---|
| 总节点 | 509 |
| 层级 Marker | unit 423、contract 38、integration 22、component 0、system 0 |
| 无层级 Marker | **26**（protocol fault_injection/concurrency/stability 仅挂专题 reliability） |
| 多层级 Marker 冲突 | 0 |
| 专题命中 | reliability 26、soak 7；**safety 0、performance 0** |
| 场景命中 | real_service 35、mock_service 11；**timeout/deadline/concurrency/reload/recovery/restart/fault_injection/invalid_input/normal/overload/no_targets/conflict/hardware 全部 0** |
| `st_XX` 命名 | 0（`test_64bit...` 为假阳性已剔除） |

根因：场景 Marker 只在 `tests/core/protocol/conftest.py` 按文件名后缀做粗粒度映射（functional→unit/fast、integration→integration/real_service 等），设计 14.3 要求的函数级场景标记未实施；`tests/core/conftest.py` 对未标记节点兜底打 unit，掩盖了层级缺失。设计 §14.6.1 要求的“缺失/重复层级、目录一致性、ST 可定位性”收集期校验不存在。

### 6.3 §14.6.3 筛选命令实测（tests/core，collect-only）

| 表达式 | 选中/总数 |
|---|---|
| `-m reliability` | 26/509 |
| `-m safety` | **0/509** |
| `-m performance` | **0/509** |
| `-m 'timeout or deadline'` | **0/509** |
| `-m concurrency` | **0/509** |
| `-m reload` | **0/509** |
| `-m 'recovery or restart'` | **0/509** |
| `-m hardware` | 0/509（环境无 PLC，预期） |
| `-m 'real_service and not hardware'` | 35/509 |
| 离线子集 `not hardware and not real_service and not network and not docker and not root` | 474/509 |

---

## 7. 真实服务、故障注入与环境证据

### 7.1 对端实现与版本（真实协议栈、非硬件）

| 协议 | 测试对端 | 实现位置 | 性质 |
|---|---|---|---|
| Modbus TCP | pymodbus 3.15.0 `ModbusTcpServer`（真实 TCP/PDU，带 trace_pdu 服务端计数与断链钩子） | `tests/fixtures/servers/modbus_server.py` | real_service，本机回环 |
| IEC104 | hat-drivers 0.10.10 独立栈 `IEC104ControlServer`/`IEC104MockServer` | `tests/fixtures/servers/iec104_control_server.py`、`iec104_server.py` | real_service，本机回环 |
| ADS | pyads 3.6.0 `AdsTestServer`（AMS/TCP 回环） | `scripts/protocol_servers.py::_ADSVirtualServer` | real_service 但**非 TwinCAT PLC**；PLC 硬件验收 NOT_RUN |

真实性由 conftest 按测试名前缀区分 `real_service` 与 `mock_service`，`real_service` 从不自动赋值（根 conftest 注释与逻辑已核实）；fault_injection/concurrency 中 ADS 全部为 mock_service。

### 7.2 实测结果

- Modbus real_service 14 passed、IEC104 real_service 18 passed、ADS real_service 3 passed（含 soak 1），合计 35 passed、0 skipped；
- 故障注入：modbus 6（服务端断链、显式重连、写 ACK 丢失→UNKNOWN 不重放、畸形响应失效会话、本地编码失败零外发、排队未提交确认失败）；iec104 3（断链恢复、关闭幂等/旧 receive 空操作、错误 COT/CA 无法完成在途读）；ads 4（mock）。
- 跳过统计：tests/core 0 skip；全量基线 33 skip 主要为 Redis/NATS 等外部服务不可用的显式 skip（conftest 中确认绝不退化为 mock）。

### 7.3 环境阻断项（NOT_RUN，不计 PASS）

1. **TwinCAT/真实 PLC**：WSL2 无 ADS 路由与 PLC，所有 `hardware` 场景 NOT_RUN；
2. **OpenTelemetry/OTLP**：包未安装，`poetry.lock` 无 opentelemetry 条目，tracing extra 无法从锁文件安装 → ST-60 Trace/OTLP 与 tracing.py 互操作 NOT_RUN；
3. **prompt_toolkit 缺失**：`tests/cli/test_command_tree.py`、`test_local_tools.py` 收集失败（D-09）；
4. **root/network/netem、Docker、NATS、systemd/dbus**：性能/soak/systemd 类测试在当前环境不具备前提，基线中相关失败/skip 如实保留；
5. **进程二进制未构建**：`wind-hub-commander` 等命令不在 PATH，system/rpc 进程级 e2e（6 节点）NOT_RUN（D-13）；
6. 全仓 `pytest.mark.xfail`/`pytest.xfail` 使用数为 0；36 处 skip 均为显式环境守卫（containerd/NATS/root/TwinCAT/现场窗口/soak profile），不存在以跳过冒充通过。

---

## 8. 缺陷清单（生产缺陷不在 S3 修复范围，保留证据）

| ID | 优先级 | 位置/调用路径 | 现象与复现 | 预期 vs 实际 | 归因 |
|---|---|---|---|---|---|
| D-01 | **P0** | `YamlConfigAdapter` 已从 `core.infrastructure.config` 删除（现为 generic_yaml 原语集），残留调用方：`src/sink_service/assembly.py:12,49`、`src/sink_service/infrastructure/diagnostic.py:12,24`、`src/web_server/application/config/service.py:22,122,126`、`scripts/generate_virtual_site.py:16,202`、`scripts/protocol_servers.py:550`（无导入，F821） | `python -I -c "import sink_service.assembly"` / `import web_server.application.config.service` 抛 ImportError；全量 24 个测试模块因此收集失败（web_server 16、system 5、sink_service 1、tooling 1 等） | 调用方应迁移到 `configuration` 包 + 各进程自有 ConfigPort 实现；实际仍引用已删类 | 分支引入（main 存在 `core/infrastructure/config/adapter.py`，已核实） |
| D-02 | P1 | `tests/web_server/test_config.py:23`、`test_server.py:15` 引用 `core.application.config_diff`（模块已删，符号迁至 `configuration.diff`） | 收集失败 2 模块；两文件同时依赖 D-01 的 service 模块，单改测试无法解锁 | 配置差异经 `configuration` 包导出 | 分支引入 |
| D-03 | P1 | 点表更新合同测试严重不足：8 态 `PointTableUpdateStatus` 仅测 SUCCESS/VALIDATION_FAILED；NOT_READY/TIMEOUT/DEGRADED/FAULTED、提交前/后故障、`residual_resources`、ADS 热更新零覆盖 | 见第 5 节 ST-20/26/53-56 | 设计表 7-6/7-7 全状态可观察 | 分支测试缺口 |
| D-04 | P1 | 静态门禁四条失败：ruff 88 errors、format 20 文件、mypy 27 errors、lint-imports 配置错误 | 第 3 节各命令可复现 | 门禁全绿方可合并 | 分支为主（含 D-07 环境成分） |
| D-05 | P1 | 10 个真实 mypy 类型错误（清单见 3.3 第 3 类），含 `resource_state` Literal 不兼容、tuple/list 赋值、Optional 未判空、拦截器 arg-type | `mypy src/core` EXIT=1 | 类型合同成立 | 分支新增代码 |
| D-06 | P2 | import-linter 配置缺 `include_external_packages=True`，合约无法执行 | `lint-imports` EXIT=1 | 契约正常校验 | 分支配置 |
| D-07 | P1 | `pyproject.toml` 声明 tracing extra（3 个 opentelemetry 依赖）但 `poetry.lock` 零条目，extra 不可安装；致 11 个 mypy import-not-found、OTLP 测试无法运行 | `grep opentelemetry poetry.lock` = 0；`import opentelemetry` ModuleNotFoundError | 锁文件与声明一致、extra 可装 | 分支引入 |
| D-08 | P2 | `test_rpc_boundary.py::test_pb2_only_in_allowed_zones` 失败：7 处 `_pb2` 越界（collector、industrial_gateway） | tests/core 唯一失败项 | 边界守护通过 | **origin/main 既存（已逐文件 git show 证实，UNRELATED）** |
| D-09 | P2 | prompt_toolkit 未安装，2 个 CLI 测试模块收集失败 | `ModuleNotFoundError: prompt_toolkit` | 环境依赖完整 | 环境（memory 已记录的既知缺口） |
| D-10 | P2 | 17 个测试模块收集失败于 `contracts.collector_v2_pb2` 不存在（tests/support/control.py 等） | 全量收集错误 | 生成/提交 v2 proto 产物或回退引用 | **origin/main 既存（已证实，UNRELATED）** |
| D-11 | P2 | 观测性测试缺口：`JsonFormatter`（JSON Lines）全仓零直接测试；观测装配幂等/冲突、后端出口故障、异步背压/关闭清理无测试 | grep `jsonl/JsonFormatter` tests = 0 | UR-03 证据链完整 | 分支测试缺口 |
| D-12a | **P1** | `tests/commander/system/test_lifecycle.py::test_start_invalid_config`：删除被引用的版本化设备模型后 `service.start()` 未拒绝（DID NOT RAISE），服务可带断裂引用闭包启动 | 单测可稳定复现；测试文件分支未改，而 `commander/application/config_manager.py` 本分支改动、`configuration/versioned_templates.py` 为分支新增（113 行） | 启动期校验必须失败且 health=FAILED；实际异常未抛出 | 分支相关（RELATED，配置重构迁移期校验时机/调用丢失，建议 ChatGPT 沿 config_manager 启动链核查） |
| D-12b | P2 | `industrial_gateway ...test_concurrent_same_point_writes_serialize_at_commander`（4 连接 FIFO 顺序）、`data_generator ...test_p1_09_gateway_restart_invalidates_all_tokens`（重启租约失效） | 真实多进程时序敏感用例；分支对两包仅改配置导入（5 文件 16 行 import 迁移），未碰并发/租约运行时 | 时序合同成立 | UNKNOWN（无相关生产增量，疑环境抖动/main 既存 flake；未扩大范围取证） |
| D-12c | P2 | 环境类失败 9 项：systemd/dbus 2（缺 dbus-next）、commander 性能阈值 2（WSL2 实测 622/660ms vs 阈值 200ms）、soak 4（NATS 不可达/监听端口不可用）、r6 文档路径 1（`docs/wind-hub_sink_architecture.md` 在 origin/main 同样不存在） | 全量最终跑次 16 failed 中其余 9 项 | 环境就绪后通过 | UNRELATED（环境/main 既存） |
| D-13 | P2 | `tests/system/rpc/test_server_commander_rpc.py` 6 节点收集已由 S3 解锁，但执行报 `FileNotFoundError: wind-hub-commander`，依赖本机已构建并入 PATH 的进程二进制 | 6 errors（setup 阶段） | 进程 e2e 环境就绪后执行 | 环境 NOT_RUN（测试侧缺陷已修，运行前提缺失） |

---

## 9. S3 测试与 Fixture 修改清单（全部限定 tests/，生产代码零改动）

### 9.1 删除（2）

- `tests/core/diagnostic/test_cli_diag.py`：目标 `core.infrastructure.diagnostic.cli/firewall/network` 已删，CLI 诊断迁至 `src/cli/diagnose_commands.py`；替代覆盖 `tests/cli/test_network_e2e.py`（5 项）；
- `tests/core/diagnostic/test_config_diag.py`：目标 `core.infrastructure.diagnostic.config.check_config/compare_config` 已删、`config check` 命令下线；替代覆盖 `tests/cli/test_diff_e2e.py`（5 项）。
- 依据：测试治理规则禁止复活已废弃架构；删除而非跳过，避免 NOT_RUN 混入统计。

### 9.2 测试代码迁移与合同对齐（22 文件修改）

1. **配置模块路径迁移（11 文件）**：`core.infrastructure.config.{business_points,units,template_graph,document_version,effective,shared_documents,shared_snapshot,versioned_templates}` → `configuration.*`；`test_task.py` 改从 `configuration` 导入 Task/校验函数；`read_yaml_mapping` 旧入口改 `read_yaml` 别名；
2. **跨模块测试解锁（3 文件，本轮新增）**：`tests/collector/component/test_acquisition_runtime_v1.py`、`tests/commander/unit/test_create_session_effective.py` 的旧路径导入迁至 `configuration`（18 节点恢复收集并全部通过）；`tests/system/rpc/test_server_commander_rpc.py` 同样迁移后 6 节点恢复收集，执行因缺 `wind-hub-commander` 二进制 NOT_RUN（D-13）；均 ruff 干净；
3. **点表更新新合同对齐**：modbus/iec104 functional 补 keyword-only `timeout`、改 async；modbus 读计划缓存测试以 fake `ReadSession` 重写为差异化淘汰断言；iec104 两个更新测试以 `_FakeConnectedSession` 重写，断言 SUCCESS/VALIDATION_FAILED、锁复用、代次首为 1；
4. **ADS 订阅新构造合同**：`test_fault_injection.py` 工厂补 `queue_capacity` 与 `MappingBarrier`；
5. **诊断套件重写**：`test_protocol_diag.py` 移除已删 `_driver` 工厂的 7 个测试，14 项对齐新合同（能力缺失 SKIPPED、benchmark 不支持返 FAIL、关闭失败降级 UNKNOWN+CLOSE_FAILED）；
6. **架构守护断言更新**：`test_core_layers.py` 对齐 v1_20 布局（generic_yaml 存在、domain/config 与 application/port/config.py 不存在）；
7. **事实性期望修正**：`test_device.py` 移除已删字段 `subscribe_enabled`；fixture 版本期望 1.0.0→1.1.0；错误码文本 `not found`→`NOT_FOUND`；
8. **Lint 收尾**：对 `tests/` 执行 ruff import 排序与单文件 format（含非 S3 语义修改的 2 个历史文件 test_command.py、test_device_control.py，仅排序），tests/ 静态检查全部清零。

所有修改可经 `git diff` 审查；无断言放松、无 skip/xfail 新增、无 Marker 放宽。

---

## 10. 最终裁决

### 裁决：**FAIL**

| 裁决条件（设计 §14.7） | 实测 | 满足 |
|---|---|---|
| 四层测试组织与目录一致 | 三层目录缺失，26 节点无层级 Marker，system/component 为 0 | 否 |
| ST-01～60 可定位测试与逐项结果 | st_XX 测试 = 0；12 项完全无覆盖，其余仅低层级等价证据 | 否 |
| 场景/专题 Marker 可筛选 | safety/concurrency/reload 等 8 个设计表达式选空 | 否 |
| 静态门禁 | ruff/format/mypy/lint-imports 4/5 失败 | 否 |
| 跨模块调用方回归 | P0 `YamlConfigAdapter` 致 24 模块收集失败，web_server/sink_service 装配不可导入 | 否 |
| Core 库自身质量 | tests/core 508 passed，唯一失败为 main 既存；真实协议栈 35 项 real_service 全过 | 是（但不构成 PASS 充分条件） |
| 失败诚实性 | 无 skip/xfail 冒充 PASS；硬件/OTLP 如实 NOT_RUN；main 既存问题标 UNRELATED | 是 |

### 移交 ChatGPT 的修复优先级建议

1. **P0 D-01**：迁移 5 个生产文件的 `YamlConfigAdapter` 调用至 `configuration` 包/进程 ConfigPort（S3 已验证 `configuration` 导出面与正确调用范式，见 collector/commander/gateway 的已迁移代码）；
2. **P1 D-07/D-04**：补锁 opentelemetry 或移除 tracing extra 声明；清零 ruff/format/mypy（D-05 清单可直接定位）；修 lint-imports 配置；
3. **P1 D-03/D-11**：补齐点表更新全状态/三阶段故障、ADS 热更新、JSON Lines 与观测装配测试；
4. **P2**：按设计 §14 重建四层目录与函数级 Marker、ST 命名追溯（测试体系整改，可与 S3 后续轮次配合）；
5. D-08/D-10 为 main 既存、D-09 为环境缺口，不阻断本分支裁决但应另行立项。

### 验收材料索引

- 本报告：`docs/core-v1-s3-acceptance.md`
- 证据目录：`/tmp/s3-acceptance/`（static-checks 日志、marker-audit.log、各 junit XML、分层/分协议运行日志、mypy 完整输出）
