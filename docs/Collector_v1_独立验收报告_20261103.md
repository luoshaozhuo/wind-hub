# Collector v1 四阶段开发 — 独立验收报告（第二轮，终审）

- 验收日期：2026-11-03
- 验收分支：`refactor/collector-v1-stage1-restart-20261102`
- 验收人：Claude Code（独立测试方；生产架构、计数口径与合并决策归 ChatGPT）
- 验收依据（优先级序）：`docs/Collector_软件设计_v1_5.md`（最高依据，正式设计）、`src/contracts/common.proto`、`src/contracts/collector.proto`、生产代码 `src/collector`、正式配置 `configs/`、测试 `tests/collector`
- 前序报告：`docs/Collector_v1_独立验收报告_20261102.md`（PASS WITH LIMITATIONS；本报告追踪其全部遗留项）

## 最终裁定：PASS WITH LIMITATIONS

在经验证范围内（Modbus/IEC104 真实链路、真实 NATS/JetStream、真实独立 gRPC 进程、配置事务崩溃恢复、任务并发、生命周期、20 RPC 真实调用、Diagnose 五阶段、Metrics），设计 v1_5 的 UR-01..UR-08、UC-01..UC-08 的 S01/S02/S03 与定量约束均有代码与真实测试证据；静态检查全绿；本轮发现的确定性局部缺陷已修复并复测。限制项：ADS 真实硬件 2 项 NOT_RUN、git push 无凭据 NOT_RUN、性能/长稳未执行、`tests/collector/integration/` 结构性空目录、1 个 P2 计数口径议题（D-15）与若干 P3 待 ChatGPT 裁决。详见第 9、10、11 节。

---

## 1. Git 基准

| 项 | 值 |
|---|---|
| 任务书指定交付 HEAD | `f372fb2fd2697c72cd379748109d63d07d8917d0` |
| 验收开始时 HEAD | `f372fb2f`（与任务书一致，未盲目覆盖远程） |
| merge-base（origin/main） | `6d6776d33b3398d42654468301bdd4d8542f9203` |
| 本轮修复 commit | `fc33e06bfd9f4a6ef8113347a3ac71f4a34d160c`（含本报告；紧随的 docs 提交仅补记本哈希） |
| 终审 HEAD | 见第 12 节 |
| **push** | **NOT_RUN**：`fatal: could not read Username for 'https://github.com'`（本机无凭据/无 gh，连续两日相同）；commit 仅存本地分支 |
| 分支保护动作 | 未创建 PR、未合并 main、未 rebase、未 force push、未切换分支、未在 main 开发 |

工作区改动均为本轮授权范围内的小修复、测试与本报告；`src/contracts/*_pb2*.py`（6 个服务共 24 个生成文件）为 protobuf 工具生成物（pyproject.toml「generated protobuf 边界」明确非手写源），**未纳入提交**，由工具链按 .proto 再生成。`docs/Collector_软件设计_v1_5.md` 为 ChatGPT 设计交付物（未跟踪状态），不由验收方代为提交。

## 2. 环境与命令

```bash
python3 scripts/dev.py env            # 必须为 ~/miniconda3 下 wind-hub，否则中止
source ~/miniconda3/etc/profile.d/conda.sh && conda activate wind-hub

# 静态
ruff check src/collector tests/collector
ruff format --check src/collector tests/collector
MYPYPATH=src mypy src/collector tests/collector
PYTHONPATH=src lint-imports

# 四层测试
python -m pytest tests/collector/unit      tests/collector/unit -q
python -m pytest tests/collector/component -q
python -m pytest tests/collector/integration -q
python -m pytest tests/collector/system -q        # 真实进程 + docker nats:2.10-alpine -js
python -m pytest tests/contracts -q

# 正式配置校验（本轮修复后）
python -m collector.main --check \
  --config configs/services/collector.yaml \
  --instances configs/instances --templates configs/templates \
  --state-dir /tmp/collector-check
# → INFO: Collector configuration validated；EXIT=0
```

环境事实：Python 3.14（wind-hub conda 环境）；docker 29.8.0 可用，系统测试每会话拉起独立命名 `nats:2.10-alpine -js` 容器并以独立 `nats_verify` 观察客户端验证；无 ADS 路由器（2 skip）；无 `gh` CLI、无 HTTPS Git 凭据。

## 3. 静态检查结果

| 检查 | 结果（本轮修复后终审） |
|---|---|
| ruff lint（src/collector + tests/collector） | **All checks passed** |
| ruff format（line=100，54 文件） | **54 files already formatted** |
| mypy strict（生产 22 + 测试，合计 54 源文件） | **Success: no issues found in 54 source files** |
| import-linter（Core 依赖方向 11 契约） | **Contracts: 11 kept, 0 broken** |
| contracts 描述符/线契约 | **87 passed** |

本轮静态修复（均不改语义）：`snapshot_repository.py` SIM102（嵌套 if 合并为 and 链）；`collector_service.py` I001 导入排序；`test_snapshot_repository.py` I001+format；`configuration.py` format。无新增 noqa/type:ignore/屏蔽检查。`tests/tooling/test_merge_skill.py` 的 format 偏差经 merge-base 取证为本分支未触碰的既有问题，标记 **UNRELATED** 不修。

收集错误普查（`pytest --collect-only` 全仓 60 个错误：25 YamlConfigAdapter / 17 collector_v2_pb2 / 10 gateway_pb2 / 8 adapter）全部位于 collector 目录之外，且在 merge-base `6d6776d3` 即存在（`git show 6d6776d3:scripts/protocol_servers.py` 取证导入不存在的符号），collector 自身收集错误为 **0** → 全部 **UNRELATED**。

## 4. 四层测试结果

| 层级 | 收集数 | 结果 | 说明 |
|---|---|---|---|
| unit | 26 | **26 passed** | 纯函数/领域/计数口径/窗口秩算法 |
| component | 47 | **47 passed** | 快照事务 14 项（含本轮新增 stage 触发孤儿回收）、仓储 fsync/慢盘/路径逃逸、configuration manager、task_control |
| integration | 0 | **NOT_RUN（结构缺口）** | 仅 `__init__.py`；设计 §14.1 的进程外协议适配集成层无在用例。所声明的集成意图（真实驱动↔仓储↔发布协作）由 system 层真实链路等价覆盖，但分层结构本身不合设计，需 ChatGPT 补例或书面调整 §14.1 |
| system | 38 | **36 passed, 2 skipped**，118.57s，连续三轮稳定（119.97s/118.57s） | 真实 `python -m collector.main` 独立进程、真实 admin token、真实 docker NATS、pymodbus/c104 真实 slave；每测试仅一层级 marker，real_service 从不自动赋值 |
| tests/collector 合计 | 111 | **109 passed, 2 skipped, 0 failed, 0 xfail** | |
| tests/contracts | 87 | **87 passed** | Proto/服务矩阵/共享语义 |

测试迁移零丢失核验：旧树 89 个测试函数全部在新四层树中保留，另新增 3 个（本轮 1 个 + 前序 2 个）；迁移中误留的空包 `tests/collector/rpc/__init__.py`、`tests/collector/iec104_slave/__init__.py` 已 `git rm`。marker 完整性由 conftest 的 `pytest_collection_modifyitems` 按参数 id 前缀补 modbus/iec104/ads 标记，杜绝漏标。

## 5. 三协议真实链路（禁 Mock 冒充）

| 协议 | 真实设施 | 故障注入（真实） | 结果 |
|---|---|---|---|
| Modbus TCP | pymodbus TCP slave，loopback 真实寄存器（1500 / 12.5 / 42） | `DelayedTcpProxy` 响应方向真实延迟 0.5s 制造超期；`TcpBlackhole` accept 不回包制造建链/读超时；停本地 listener 制造 ECONNREFUSED | 轮询完整信封、总召缺测点 PARTIAL、Watchdog 重连、逐点 TIMEOUT_UNCONFIRMED 全部有真实断言 |
| IEC 104 | c104/lib60870 真实 slave | 真实自发上送、真实总召；blackhole 下 STARTDT 永不完成（握手有界，不楔死设备锁） | listen 自发上送、InterrogateDevice 真实 SUCCESS、建链有界→FAULTED/ISOLATED + 事件证据 |
| ADS | — | 需 TwinCAT/路由器硬件 | `test_uc01_s01_ads_real_device`、`test_uc03_s01_subscribe_requires_ads` **NOT_RUN（2 skip）**，门控变量 `WIND_HUB_TEST_ADS_ROUTER`，无伪造 PASS |

I/O 所有权十项特别核查结论：① 锁层次全链单方向，无 AB-BA；② 异常路径无锁泄漏；③ 超时不释放会话（`_bounded` = shield + wait_for，`_inflight` 单飞准入门）；④ close 失败保留旧 worker（reconfigure 显式重试失败的 stop，不替换会话）；⑤ STOP 以 desired_state 总闸 + monitor 退出，Watchdog/Reload/旧候选均不能复活；⑥ subscribe/listen 握手经 `_bounded` 有界（D-06 已修）；⑦ create_task 10 处全部有属主与取消等待；⑧ submit 层取消后 I/O 仍被 worker 拥有并在 stop 中 `_settle()`；⑨ SUBSCRIBE/LISTEN 状态门闩（STARTING 前置，回调可推进 RUNNING）；⑩ 残留 P3：submit 层缺 STOPPING 准入门（A-1，gather 快照后注入任务的异常可能 never retrieved）。

## 6. NATS / JetStream 真实链路

- realtime：独立观察客户端 Core NATS 订阅，验证样本真实可达；realtime 计数只增 `client_submitted`（本地客户端接受），**绝不计入 PubAck 确认口径**（publisher.py:157 注释与计数分离，D-02 修复持续有效）。
- reliable：JetStream 临时消费者在真实 PubAck 后可消费，`pubacked` 为唯一确认计数（publisher.py:167）。
- 故障：真实 `docker stop/start` 断连-恢复用例；发布异常/取消只计 `unconfirmed`，不判确定丢失、不盲目重发（取消守恒式 `attempted > published + unconfirmed`，publisher.py:183）。
- 关停：`queue.join()` 2s 有界，超时后逐条 drain 计 SHUTDOWN_DROPPED，连接 drain 2s 有界后 close。
- 背压：样本队列容量 2048 独立于 `metrics.pending_capacity=120`（D-03 修复持续有效）；QueueFull 计 backpressure 事件。
- 遗留：D-15（连接失败路径 failed+dropped+not_attempted 一次三计，§13.5 互斥性）待 ChatGPT 裁决，见第 9 节。

## 7. UR → UC → S01/S02/S03 → 实现/测试/结果/证据 追溯矩阵

| UR | UC | S01 主流程（证据） | S02 边界（证据） | S03 异常恢复（证据） | 裁定 |
|---|---|---|---|---|---|
| UR-01 连接与健康 | UC-01 | test_uc01_s01：MODBUS/IEC104 真实连接健康；ads NOT_RUN | 重复 Start 幂等；错误端点逐设备上报；disabled 拒绝 | 断链 Watchdog 真实重连 | PASS（ADS 限） |
| UR-02 任务管理 | UC-02 | Start→采样→Stop 静默 | 断链下发、链路恢复后收敛；单 poll task + 锁互斥 | 手动 Stop 不被 Watchdog/Reload 复活（真实断言） | PASS |
| UR-03/04 采样发布 | UC-03 | poll 完整信封；IEC104 listen 自发；reliable JetStream PubAck；ads subscribe NOT_RUN | 总召缺测点 PARTIAL；超期 skipped_slots；blackhole 握手有界 | NATS 断连恢复；发布未确认在 uc07_s03 真实覆盖 | PASS（ADS 限） |
| UR-06 配置快照 | UC-04 | Push 惰性、Apply 原子切换（14 组件测试 + 真实重启） | 陈旧 revision ABORTED；已发布模板不可变/消失；service 变更 RESTART_REQUIRED | 候选持久化跨进程恢复、崩溃窗口孤儿回收（本轮 D-12）、部分设备故障保留失败对象 | PASS |
| UR-07 诊断 | UC-05 | 选阶段执行带证据 | 无目标 UNKNOWN + Sink 边界 limitation + 非法阶段 NOT_RUN | 拒连 PORT/路由 NETWORK/超时 TIMEOUT/点表 POINT/归一化 NORMALIZATION/NATS_CONNECTION 九类错误码（本轮 D-10）；预算截断后续阶段 NOT_RUN | PASS |
| UR-08 指标 | UC-06 | 空窗不造零（d165d32e 生效中） | 真实流量后窗口计数与 P95/P99、device scope | window 必须整秒，否则 INVALID_ARGUMENT | PASS |
| UR-05/06/07/08 gRPC | UC-07 | 20 RPC 全真实调用（见第 8 节） | 鉴权/分页/非法参数/事件页容量淘汰 | 截止期未确认；关停期 UNAVAILABLE | PASS |
| UR-01..04 生命周期 | UC-08 | CLI --check 完整树（本轮正式配置 EXIT=0） | SIGTERM 优雅关停并释放文件锁 | SIGKILL 重启恢复持久化 START 意图；部分故障健康设备继续；UNSETTLED I/O 非零退出 | PASS |

设计 §14.5.2 的 24 个 UC×S 组合全部有对应用例；2 个 ADS 主流程项 NOT_RUN。

## 8. 20 个 RPC 真实调用矩阵

`test_uc07_s01_all_20_rpcs_real_invocation` 在真实独立进程上逐个调用，invoked 集合与 20 名集合严格相等。本轮该测试因 ScalarValue 裁决更新（见下），复测通过。

**CommonService（9）**：GetServiceInfo、GetServiceStatus、GetCapabilities、GetConfigSnapshot（DISK/RUNTIME/CANDIDATE 三 scope）、PushConfigSnapshot、ApplyConfigSnapshot、GetMetrics（全量/窗口/device scope）、GetEvents（分页/淘汰）、Diagnose（五阶段）。
**CollectorService（11）**：ListDevices、GetDevice、ListPoints、GetPoint、ReadPoints（真实 Modbus 值）、ListTasks、GetTask、StartTask、StopTask、GetPublishingStatus（真实连接态与计数）、InterrogateDevice（真实 IEC104 总召）。

错误码真实断言：PERMISSION_DENIED（缺/错 admin token）、NOT_FOUND（未知诊断目标/设备）、INVALID_ARGUMENT（窗口/scope）、FAILED_PRECONDITION（空快照/事件页过期）、ABORTED（陈旧 revision，cf0a84c1）、响应体 TIMEOUT_UNCONFIRMED。

**ScalarValue 线契约（昨日 A-2 关闭）**：ChatGPT 已裁决「对齐 Core」，交付 commit `cf0a84c1` 复用 `core.infrastructure.rpc.scalar_codec.encode_scalar`（范围内 int 统一 sint64 臂，common.proto six-way oneof 为唯一线契约，§9.6 D04）。本轮发现系统测试仍钉死旧分叉 uint64 臂（3 处），取证后判定测试为旧残留，已改为断言 `sint64_value` 并复测通过；生产代码无需再动。

## 9. 缺陷台账

### 9.1 本轮独立核验后已修复（小修复权限内，全部复测）

| ID | 级别 | 位置 | 问题（设计条款） | 修复 | 复测证据 |
|---|---|---|---|---|---|
| D-09 | P1 | configs/services/collector.yaml | 正式配置缺 `file_version`，真实 `--check` exit=1 `invalid file_version None`（昨日遗留 P1；同树其余文件均 1.0.0） | 补 `file_version: 1.0.0`（数据修复，非契约变更） | `--check` EXIT=0；系统全量 |
| D-10 | P2 | grpc/config_service.py Diagnose | ① 错误码全坍缩为 FAILED/阶段名，违反 §13.5 九类区分（NETWORK/PORT/PROTOCOL/POINT/NORMALIZATION/NATS_CONNECTION/NATS_PUBLISH/AUTHORIZATION/TIMEOUT）；② 证据年龄取 `max(timeout, watchdog)`，违反「阶段 timeout 与运行时新鲜度较严格者」；③ NATS 阶段未声明 PubAck/Sink 未验证 | errno 精分 PORT/NETWORK；健康失败 PROTOCOL；good≠len→POINT；编码失败→NORMALIZATION；超时→TIMEOUT；改 `min()`；NATS UNKNOWN→NATS_CONNECTION 并追加 limitation 文案 | diagnose 4 系统测试 PASS（含 PORT 断言更新）；mypy/ruff 绿 |
| D-11 | P3 | publisher.py / assembly.py | §3.10 定量默认值分叉：realtime 新鲜度默认 1000ms（设计 500ms）；事件容量 fallback 1024（设计 1000） | 默认值对齐 500 / 1000 | 系统全量；无阈值放宽 |
| D-12 | P2 | snapshot_repository.py | `collect_orphan_generations()` 已实现且有组件测试但**无任何生产调用方**：「generation 提升后、收据落盘前」崩溃窗口遗留的世代目录永不回收（昨日 A-6 残留半） | 在 `stage()` 的 storage_lock 临界区开头接线（RLock 同线程可入），引用保护仍为 active+全部收据+在途候选，语义不变 | 新增组件测试 `test_stage_reclaims_orphan_generations_from_crash_window`；component 47 passed；system 36 passed |
| D-13 | P2 | application/configuration.py、grpc/config_service.py | Apply 幂等提升路径的收据 fsync（:126）、read_generation 16MiB 读+sha256（:144）、retry_failed 三次 active_paths 全量校验、GetConfigSnapshot DISK/CANDIDATE 全在事件循环线程（昨日 A-3 残留）；仓库自身 commit 早已 to_thread，Apply 路径却漏网 | 全部按仓库既有模式 `asyncio.to_thread` offload；新增 `_read_retry_material`/`_read_candidate` 合并线程跳数；不改任何接口与返回语义 | unit+component 73→同数全绿；system 36 passed |
| D-14 | P3 | 多处 | ruff SIM102/I001/format；测试迁移残留空包 2 个；marker 漏标风险 | 见第 3、4 节 | 静态全绿 |

### 9.2 必须报告 ChatGPT 的未决项（超出局部修复权限）

| ID | 级别 | 位置 | 问题 | 为何不自修 |
|---|---|---|---|---|
| D-15（昨日 A-4） | P2 | publisher.py:138-140（及 :99-100、:118-119、:217-218） | NATS 连接失败一次同时增 `failed`+`dropped`+`not_attempted`；QueueFull/过期/关停路径同样多维并计。§13.5 要求 attempted/submitted/ack/fail/unknown/drop 互斥不双计；但现状把「结局/原因/是否尝试」当并列累计维度，GetPublishingStatus 对外口径与系统测试均依赖现状 | 修改塑造对外 RPC 计数语义与运维看板口径，需 ChatGPT 按 §13.5 给出「单计数器分区」或「书面确认多维累计」裁决并同步改测试 |
| D-16（昨日 A-5） | P2 | configuration.py | 部分收敛时 RUNTIME 快照整代回旧，缺对象级生效视图（§11.5 effective_revision 范围语义） | 语义设计级变更 |
| D-17（昨日 A-8） | P2 | src/core | modbus CPython 3.14 Runner.close 关停竞态；collector 以失败即非零退出加固绕过，根因在 core | 跨模块根因修复 |
| D-18 | P3 | snapshot_repository.py | receipts 与已提交世代**按设计永不回收**（幂等重放依赖），长期运行世代目录无界增长（每代 ≤16MiB）；D-12 只解决崩溃孤儿 | 保留期限是设计决策（§11.7/11.8 需补世代保留策略） |
| D-19 | P3 | snapshot_repository.py | 硬崩溃（SIGKILL）可留 `.candidate-*`/`.generation-*`/`.validate-*` 暂存目录，`_prune` 只按 32hex 正式名扫描，不清理点前缀暂存 | 清理策略需确认不与并发 stage 冲突，建议在下一版仓储统一处理 |
| D-20 | P3 | commit() | 调用方二次取消可使第二次 `await shield` 再次抛 CancelledError，磁盘操作虽仍由 storage_lock 保护到完成，但 asyncio.Lock 的释放在等待者返回之后；无属主窗口极短 | 涉及关停热路径，按 A-1 同批处理 |
| D-21 | P3 | acquisition.py:297-308 | 回调收到 GOOD 样本即置 RUNNING，即使此刻 `_needs_reconnect`/ISOLATED 在途；恢复谎报窗口最长一个 watchdog 周期（5s） | 状态机语义需与 §6 恢复裁决对齐后调整 |
| D-22 | P3 | grpc/config_service.py / task_control.py | disabled 设备 Start：库存无对象走 NOT_FOUND，设计文案曾出现 FAILED_PRECONDITION，错误码不一致 | 需以 proto/设计错误码表为准统一 |
| D-23 | P3 | publisher.stop / metrics_emitter.stop | close 路径无自身截止（drain 有界，close 无界）；nats 客户端异常时可能拖过长关停 | 随关停 30s 预算整体改造 |
| D-24 | P3 | assembly.py | 启动引导期首次 stage/commit/collect_source 仍为同步磁盘 I/O（仅启动一次，不影响运行期 loop）；`metrics.enabled=false` 时 resource sampler 仍启动；requires_restart 与脱敏用 yaml.safe_load 未经 core 重复键拒绝 Loader | 合并为启动路径小改造批次 |
| D-25 | P3 | scripts/ci_scope.py:67,85 | 引用迁移后已不存在的 `tests/collector/rpc`、`tests/collector/sink` 路径，CI 范围选择漂移 | CI 脚本归治理目录，按规则提 issue 修 |
| D-26 | P3 | tests/collector/integration/ | 分层空目录，§14.1 集成层无在用例（功能现由 system 真实链路覆盖） | 需补例或修订设计 §14.1 |

昨日 A-1（关停窗口并发 close 取证不成立，降 P3）并入 D-20/§5 第⑩项；A-2 已由 cf0a84c1 + 本轮测试对齐关闭；A-3 由 D-13 关闭运行期部分，启动残留并入 D-24；A-6 候选持久化部分由交付链（8b13601e 等）+ D-12 关闭；A-7 由 D-10 关闭（NATS_PUBLISH 隔离发布探测设计上声明为不验证并显式 limitation，若 ChatGPT 要求真实隔离发布则属增量需求）。

## 10. skip / xfail / NOT_RUN 台账

- **skipped = 2**（均为真实硬件门控，非伪装）：`test_uc01_s01_ads_real_device`、`test_uc03_s01_subscribe_requires_ads`，门控 `WIND_HUB_TEST_ADS_ROUTER`。
- **xfail = 0**；本轮未新增任何 skip/xfail，无断言降级、无阈值放宽、无检查屏蔽。
- **NOT_RUN**：① ADS 两项；② git push（本机无凭据，见第 1 节）；③ 长稳/soak、性能容量长测、P99 大样本（本轮为功能验收）；④ mTLS 部署形态（代码有三证同具校验与远程强制 mTLS，无证书环境未做真实 TLS 系统测试）；⑤ integration 层 0 用例（D-26）。

## 11. 性能与资源约束（§3.10/§11.8/§13.5）核验

| 约束 | 设计值 | 代码/配置事实 | 验证方式 |
|---|---|---|---|
| 连接/读超时 | 10s / 5s | assembly 解析注入 worker，Interrogate timeout_ms=0 走 worker.timeout 有界（D-01） | 组件+系统 |
| Watchdog / 健康 | 5s / 1s | AcquisitionPlan 注入，supervise 周期 wait_for | 系统断链重连实测 |
| 优雅关停 | 30s | shutdown_timeout 贯穿 reconfigure/stop，UNSETTLED 非零退出 | 系统 SIGTERM/SIGKILL |
| 线程池 | 32 | configure_default_thread_pool(32)，gRPC maximum_concurrent_rpcs=32 | 代码取证 |
| realtime 新鲜度 | 500ms | publisher 默认本轮对齐（D-11） | 系统真实过期样本 |
| 窗口 | 60/300/3600s | telemetry 配置默认一致 | 系统窗口测试 |
| metrics 发布/滞留/pending | 60s / 300s / 120 | metrics_emitter 参数一致，pending 独立于样本队列 2048（D-03） | 代码+系统 |
| 事件容量 | 1000 | assembly fallback 本轮对齐（D-11） | 代码取证 |
| 候选 8 / 16MiB / 256 文件 / TTL 3600s | §11.8 冻结 | 仓储构造默认值一致；容量计数含磁盘 32hex 恢复目录；重启 wall-clock 重算 TTL | 组件测试（容量/过期/损坏） |
| fsync 顺序 | 文件→父目录→收据→CURRENT→根 | _write_generation/_persist_candidate/_write_receipt/_commit_sync 逐处符合；os.replace/os.rename 单指针 | 14 项组件测试含慢盘 |

未在负载下验证：吞吐/CPU/RSS 上限、32 线程池饱和行为、NATS 长断连积压、磁盘满行为——均属第 10 节 NOT_RUN 的性能长测范围，不以静态符合冒充性能 PASS。

## 12. 终审裁定与合并建议

**裁定：PASS WITH LIMITATIONS。**

1. 设计 v1_5 在已验证部署形态（Modbus + IEC104 + realtime/reliable NATS + 无 TLS 本地 gRPC）下，UR/UC、20 RPC、三类配置事务、定量约束均有真实链路证据；四轮验收（静态、分层测试、真实链路故障注入、设计符合度复审）全部执行完毕。
2. 本轮 D-09..D-14 已在授权范围内修复并复测（静态 54 文件全绿；collector 109 passed / 2 skipped / 0 failed；contracts 87 passed；系统三轮稳定）。
3. 昨日 P1/P2 遗留：file_version（D-09）、Diagnose 九类错误码与新鲜度（D-10/A-7）、GC 接线（D-12/A-6）、loop 磁盘 I/O 运行期路径（D-13/A-3）、ScalarValue 对齐（A-2/cf0a84c1）均已关闭。
4. **合并建议：可按仓库流程（//merge-branch Squash Merge）合并至 main**，但须在合并说明中随附限制清单：ADS 两项硬件 NOT_RUN、D-15 发布计数互斥口径需 ChatGPT 裁决（合并不改变其现状，裁决后以独立变更落地）、D-26 integration 层缺口、性能长测未执行；D-18/D-19（世代与暂存目录保留策略）建议列入 v1.1 首批。
5. push 在本机仍为 NOT_RUN；本报告与修复 commit 留在开发分支，由有凭据环境推送后再执行合并。

终审数量：collector 四层 **109 passed / 2 skipped(ADS) / 0 failed / 0 xfail**（unit 26、component 47、system 36+2skip、integration 0），contracts 87 passed；静态 ruff/format/mypy(54 文件)/lint-imports(11 契约) 全绿；正式配置 `--check` EXIT=0。修复 commit `fc33e06bfd9f4a6ef8113347a3ac71f4a34d160c`；push NOT_RUN（无凭据）。
