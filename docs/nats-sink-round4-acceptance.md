# NATS / Sink Service — Round 4 独立验收报告（测试债务收尾）

- 分支：`feat/nats-sink-service`
- 日期：2026-10-29
- 验收人：Claude Code（独立验收；不修改生产业务逻辑）
- 结论：**PASS WITH LIMITATIONS**
  - Round 3 遗留 31 项 system/soak 迁移失败**全部关闭**（RPC 10/10、
    recovery 17/17、soak 目标负载 4/4 + 长 soak 60s 冒烟 1/1），且数据链路
    实证为 **Modbus/IEC104 → Collector 子进程 → NATS → sink_service 子进程
    → Redis**，不是"配置能加载即通过"。
  - Docker Fixture（R4-3）三种行为实证：外部服务零容器生命周期、自动 compose
    健康/清理、端口冲突明确报错且不动用户容器。
  - Web Admin（R4-4）废弃 write-test 引用清零，现行 verify 链路完整；前端
    typecheck/lint/prettier/build/185 单测全绿。
  - Python 四门全绿；全量 `pytest -q`：**1510 passed, 1 failed**（FC15，
    main 基线复现，UNRELATED）。
  - 限制：netem 7 项行为用例在本机 unshare netns 内不可达 NATS/Redis
    **NOT_RUN**（perf-netem root runner 为既定执行环境，skip 信息明确）；
    mock Playwright 1 项 `file_archive` 失败为 main 基线（PR #92 移除
    FileSink 后 E2E 未同步），UNRELATED；TDengine/gRPC/Kafka/下行链路维持
    Round 3 的 NOT_RUN。

---

## 1. Git 基线与工作区

| 项 | 值 |
|---|---|
| 验收起点（Round 3 报告 HEAD） | `0c6308b2` |
| ChatGPT Round 4 HEAD（验收开始） | `88d9a6f72634fe360bb522a7b73adc6970fa511c`（= `origin/feat/nats-sink-service`） |
| `origin/main`（fetch 后） | `cb380ddb3bee249defb75e5922e2b70987a18211`（PR #105） |
| merge-base | `cb380ddb`（main 为分支祖先） |
| 本轮新增提交 | `e9b7e069`（system 拓扑迁移）、`a018e1ae`（web mock 用例迁移）、本报告提交 |
| 禁止动作遵守 | 未 rebase、未 force push、未合 main、未建 PR；ADS/IEC104/Modbus adapter 生产代码零改动 |

`0c6308b2..88d9a6f7` 共 13 个提交，累积 diff 仅 12 文件（+21/−92），
全部位于 `tests/` 与 `src/web_admin/src/mocks/`：

```text
src/web_admin/src/mocks/handlers.ts            |  9 ----
src/web_admin/src/mocks/service.ts             | 45 ----------------
tests/fixtures/services/docker-compose.yml     |  1 -
tests/support/recovery_helpers.py              | 28 +++---------
tests/support/soak/runner.py                   |  4 +-
tests/system/network/test_modbus_network_degradation.py | 3 +-
tests/system/recovery/*.py（6 文件）           | 小改
tests/system/rpc/test_server_collector_rpc.py  |  2 +-
```

**审查发现：ChatGPT 只完成了配置格式机械迁移，未闭合数据链路**——全部
recovery/network 测试仍只启动 Collector 单进程，而新架构 Collector 不写
Redis（`src/collector` 全树无 redis 直写，装配根只有 NatsPublisher），
且 7 个 `write_config_tree` 直写的配置缺 `system.messaging`，Collector
启动即 `ConfigError: system.messaging is required`。这是本轮验收方修复的
主体（§4），属测试夹具债务，非生产缺陷。

---

## 2. ChatGPT 代码独立审查（13 提交 / 12 文件）

| 审查点 | 结论 | 证据 |
|---|---|---|
| Task 定义不再含旧 `outputs` | **PASS** | 全树无 `"outputs": [{...sink_id}]` 残留（`web_server/test_config.py:337` 故意构造非法 tasks 测拒绝，语义仍成立） |
| Sink 映射落在 sinks.yaml | **PASS** | `SinkOutputSchema`（`sink_schema.py`）以 task_id 为键；跨校验 `Sink 'x' references unknown Task` 在 `parse_domain.py:649` |
| key_prefix 语义保留 | **PASS** | recovery/netem/watchdog/restart 各自前缀（wind-hub-recovery/-netem/-watchdog/-iec-restart/-shutdown/-resources）迁入 sink outputs，Redis 边界 key 断言未改 |
| 多 Task/Sink 关系 | **PASS** | sink service CLI 支持多 `--realtime-sink`；多设备 soak 每设备独立 subject（farm.collector.device.point） |
| 子进程实读新配置 | **FAIL→已修复** | 16 recovery + 7 netem 用例启动即崩或无消费侧；本轮补 messaging 与 sink_service 进程后实证（§5） |
| reload/restart/shutdown 断言强度 | **部分有缺陷→已修复** | `test_reload_failure.py` 的"ghost sink 注入"落在 Collector 不解析的 sinks.yaml（`load_collector(include_sinks=False)`），prepare 被错误接受（`assert prepared.success is False` 实测 True）。已改为 Task 引用未知设备组（Collector 确定性拒绝），并保留旧任务继续出数、恢复后 interval=0.5 的全部原断言 |
| soak r.0000→r_0000 | **PASS** | `_points_payload` 键与 `_write_loop` 的 `ProtocolWrite(point="r_0000")` 一致；地址 holding 0 存在；点号违反 `validate_subject_token`（`.` 禁止），重命名为必需修正 |
| compose 去 container_name | **PASS** | 实证见 §6 |
| web mock 删除完整性 | **有遗漏→已修复** | 路由/服务实现删净，但 `mockCapabilities.spec.ts` 用例 14 仍 import 已删除的 `writeTestSink`（vitest 185 中 1 红）；已迁移到 verify。另有一处孤儿 `/** Write Test ... */` 注释残留 |
| 生产代码零变更 | **PASS** | `git diff 0c6308b2..HEAD -- src/ ':!src/web_admin'` 为空；本轮修复同样零生产 Python 改动 |

---

## 3. R4-1：System/Recovery 配置迁移验收矩阵

| # | 要求 | 结果 | 证据 |
|---|---|---|---|
| 1 | Task 无旧 outputs 字段 | PASS | grep + parser `_keys(... 仅四字段)` 拒绝额外键 |
| 2 | outputs 映射在 sinks.yaml | PASS | 全部 config builder 改为 sink 侧 outputs |
| 3 | key_prefix 保语义 | PASS | 6 类前缀原样迁移；Redis key 断言未弱化 |
| 4 | 多 Task/Sink 正确 | PASS | soak 多设备任务、sink service 多 sink CLI |
| 5 | 配置可被生产解析器加载 | PASS | Collector snapshot 与全量 domain parse 双路实测（含 netem 配置） |
| 6 | **子进程实读新配置** | PASS（本轮修复后） | 17/17 recovery 真实双进程；配置 nats_url 全线程入 builder |
| 7 | reload/restart/shutdown 断言不降级 | PASS（本轮修复后） | reload-failure 改注入点；restart/kill 用例 sink_service 跨 Collector 重启独立存活，after-baseline 时间戳断言保留 |
| — | **数据链路实证** | PASS | 所有 Redis 值来自 sink_service：消费侧缺失时测试确定性超时（修复前 16 红即反证）；双进程拓扑下 rotor_speed 等值 good、时间戳推进、停任务/断连恢复语义全部成立 |

IEC104 点 ID `meas.active_power` 含点号，违反 NATS subject token 契约
（Collector 装配即 ValueError）。本轮测试夹具改名 `meas_active_power`
等（CA=1、IOA 100/200/300 与 type_id 不变）；**提请注意：生产 IEC104
点表若使用点号 ID，在 NATS 架构下无法启动 Collector——属配置约束，非
本轮缺陷，交 ChatGPT 在文档/迁移指南中确认。**

---

## 4. 本轮新增修复（验收方，全部限测试/夹具/前端 mock）

### `e9b7e069` test(system)：双进程拓扑 + skip 语义

1. `tests/system/conftest.py` 新增共享 `sink_service_factory`：先于
   Collector 启动真实 `wind-hub-sink-service`（`--realtime-sink redis_sink`、
   farm=test、唯一 deployment/state_dir），TCP 就绪探针，结束优雅回收；
   acquisition E2E 的本地同名 fixture 收敛为共享实现。
2. 16 recovery + 7 netem 用例：fixture 签名加 `sink_service_factory`/
   `nats_service`，每个用例显式起消费侧；`write_config_tree` 直写者补
   `system.messaging`（新助手 `realtime_messaging()`）；
   `write_modbus_redis_config(..., nats_url=)` 贯通。
3. reload-failure 故障注入点修正（§2）。
4. `tests/conftest.py` `nats_service`：不可达时的 `pytest.skip` 此前是
   死代码（`wait_until` 超时直接抛 WaitTimeoutError）→ 捕获超时转明确
   skip。netns 子进程内 7 用例由 ERROR 转为诚实 skip。

### `a018e1ae` test(web-admin)：mock 能力矩阵对齐 verify

用例 14 从已删除的 write-test 迁移到 `POST /sinks/:name/verify`：固定
modbus 故障 → message 含 `SINK_CONNECTION_REFUSED`、`verification failed`
日志存在、sink healthy=false。删除 service.ts 孤儿注释；prettier 收敛
两个被分支触达且 main 即脏的文件（sinks.ts/config.ts，净改动 1 空行 +
import 折行）。

**未使用任何 skip/xfail/ignore/降断言掩盖问题。**

---

## 5. 测试执行记录（真实服务，无 Mock 冒充）

环境：本机 Docker 既有 `redis:7-alpine`（6379）、`nats:2-alpine -js`
（4222，JetStream 开），以 `WIND_HUB_TEST_REDIS`/`WIND_HUB_TEST_NATS`
外部服务模式注入——测试框架不管理其生命周期。

| 套件 | 命令（env 略） | 结果 | 耗时 |
|---|---|---|---|
| RPC system（Round 3：11 ERROR） | `pytest tests/system/rpc/` | **10 passed** | 14.6s |
| Recovery（Round 3：16 ERROR） | `pytest tests/system/recovery/` | **17 passed**（修复前复跑 16 failed/1 passed，启动崩溃日志举证） | 112.6s（修复后） |
| Acquisition 双进程 E2E | `pytest tests/system/acquisition/test_modbus_data_flow.py` | **2 passed** | 合计 28.2s |
| netem 行为（Round 3 同目录失败） | `pytest tests/system/network/` | 本机 **7 skipped / 3 selfcheck passed**；skip 原因明确（NATS/Redis 在 unshare netns 不可达） | 13.4s |
| Soak 目标负载（Round 3：4 FAILED） | `pytest tests/system/soak/test_target_load.py` | **4 passed, 2 skipped**（2 项 root/netem 档） | 129.4s |
| 长 soak 路径 | `WIND_HUB_SOAK_PROFILE=smoke_1h WIND_HUB_SOAK_DURATION_S=60 pytest tests/system/soak/test_long_running.py` | **1 passed**（0 dropped、NATS 边界收 ≥99%、0 重复） | 63.6s |
| JetStream reliable（Round 3 P1 回归） | `pytest tests/sink_service/` | **20 passed**（11 reliable 生命周期 + 9 其他） | 8.2s |
| **全量后端** | `pytest -q --ignore=tests/web_admin`（无 marker 裁剪） | **1510 passed, 1 failed, 3 skipped, 10 deselected**；失败仅 FC15 | 474.4s |
| 前端单测 | `vitest run` | **185 passed (28 files)**（修复前 184/1 红） | 8.3s |
| Playwright mock E2E | `npm run test:e2e:mock` | 5 passed, **1 failed**（`file_archive`，main 基线 UNRELATED） | 10.7s |

10 deselected = netem 父进程转 unshare 子进程（7）+ 子进程机制内的
既定项；全量统计因子进程输出并入终端、父汇总行只显示父进程计数，
netem 7 项在子进程为 skipped（NOT_RUN，非通过）。

### 唯一失败定性

- `tests/industrial_gateway/test_gateway_r2_fc15.py::test_fc01_readback_matches_point_store`
  —**UNRELATED（main 基线）**：在 merge-base `cb380ddb` 独立 worktree
  复跑同型断言失败（device_id=9 未映射线圈期望异常读，实测返回正常），
  与本分支零文件交集；任务书亦明示其为 main 既有项。
- Playwright `file_archive` 行——**UNRELATED（main 基线）**：main 上
  E2E 期望 file_archive sink，但 FileSink 与 seed 数据已在 PR #92
  (`deeee58e`) 移除，cb380ddb 上 spec/seed 即不一致。未在本轮扩范围修复。

---

## 6. R4-3：Docker Fixture 实证矩阵

| # | 要求 | 结果 | 证据 |
|---|---|---|---|
| 1 | 外部 Redis 时不管容器 | PASS | 全量测试以 env 外部模式运行后，`docker ps -a --filter label=com.docker.compose.project=wind-hub-test` 为空；用户容器全程未停 |
| 2 | 自动 compose 启动/健康/清理 | PASS | 用**同一份 compose 文件机制**、隔离项目 `wind-hub-r4verify`、宿主端口 6399 实跑：容器自动命名 `wind-hub-r4verify-redis-1`（无固定名）、healthcheck 转 healthy、`down -v --remove-orphans` 后容器清零 |
| 3 | 不误停用户容器 | PASS | 直接调用真实 `compose.compose_up()`（6379 被无 label 的用户容器占用）：抛 `ComposeError`，daemon 原文 `Bind for 0.0.0.0:6379 failed: port is already allocated`；前后 `wind-hub-test-redis/-nats` State.Running 均 true；项目级 down 清理后仍 true |
| 4 | 并行/重复不资源名冲突 | PASS（静态+机制） | 容器名由 project+service 派生（`-1`），移除固定名即消除跨项目冲突；项目隔离由 `-p` 保证 |
| 5 | 失败有明确错误 | PASS | ComposeError 合并 stdout/stderr，端口冲突原文可定位 |

未删除、未停止任何用户 Docker 资源（`dc-ems-*` 业务容器与两个
wind-hub-test-* 手工容器均保持运行）。

---

## 7. R4-4：Web Admin 废弃 Mock 清理矩阵

| 检查 | 结果 |
|---|---|
| `write-test` / `writeTestSink*` 残留（src 与 tests） | **0**（仅余 1 条说明性注释"write-test 已删除"） |
| 现行 `POST /api/v1/sinks/{name}/verify` 链路 | 完整：`api/sinks.ts` → OpenAPI 生成类型（schema 含该路径）→ MSW handler → `verifySinkByName`（分阶段 passed/warning/failed/skipped）→ store `config.verifySink`；后端 `webapi/v1/sinks.py:60` 实路由在 |
| 未重新接回 Collector | 确认；verify 经 SinkService/SinkDirectoryPort 语义，前端无 Collector sink 调用 |
| vue-tsc + vite build | PASS |
| eslint | PASS（0 输出） |
| prettier --check . | PASS（全部文件达标） |
| OpenAPI 契约漂移 `check:api` | PASS（DRIFT CHECK: PASS） |
| vitest | 185/185 PASS |
| Playwright mock | 5 passed / 1 UNRELATED（file_archive，main 基线） |

---

## 8. 静态门禁

| 门禁 | 结果 |
|---|---|
| `ruff check src/ tests/ scripts/` | **PASS**（All checks passed） |
| `ruff format --check src/ tests/ scripts/` | **PASS**（610 files already formatted） |
| `mypy src` | **PASS**（324 files, no issues） |
| `lint-imports` | **PASS**（11 kept, 0 broken） |
| 前端 typecheck/lint/format/build/契约 | 全 PASS（§7） |

---

## 9. NOT_RUN 与遗留项（诚实声明）

1. **netem 7 行为用例（本地）**：非 root 经 `unshare -rmn` 子进程执行，
   新 netns 内宿主 loopback NATS/Redis 不可达、docker socket 被 tmpfs
   遮蔽，fixture 明确 skip（NOT_RUN，非 PASS）。配置迁移已经双路解析与
   同模式 recovery 套件实证；行为矩阵的**正式执行环境为 CI
   `self-hosted perf-netem` root runner**（ci-performance.yml）。
2. **TDengine / gRPC 下行 / Modbus 下行 / Kafka 真实链路**：维持 Round 3
   NOT_RUN（本机无对端/镜像）。
3. **Playwright real-backend E2E**：本轮未执行（需要完整后端栈）。
4. **main 基线项**：FC15（已在 cb380ddb 复现）、mock E2E file_archive
   （PR #92 遗留）；建议另立任务，不在本分支扩范围。
5. 提请 ChatGPT 关注生产约束：NATS subject token 禁点号对存量 IEC104
   点表命名的影响（§3）。

---

## 10. 验收标准对照

| # | Round 4 标准 | 判定 |
|---|---|---|
| 1 | Round 3 的 31 项失败全关闭 | **PASS**（rpc 10 + recovery 16 + soak 4；另 recovery 原本通过的 1 项与双进程拓扑一并实证） |
| 2 | Docker 冲突验证与处理 | **PASS**（§6 五矩阵） |
| 3 | Web Admin 无废弃调用 | **PASS**（补掉 1 处测试残留后 0 引用） |
| 4 | Python 静态门禁 | **PASS**（四门） |
| 5 | 前端 TS/构建门禁 | **PASS**（含 185 单测；1 E2E 为 main 基线） |
| 6 | 全量 pytest 无新增 RELATED 失败 | **PASS**（唯一失败 FC15 已证 main 同态） |
| 7 | Round 3 P1 不回归 | **PASS**（JetStream 20/20，clear 重启重绑在列） |
| 8 | 不引入生产逻辑变更 | **PASS**（零生产 Python 改动；前端仅 mock/测试与 prettier） |

---

## 11. 总结论

**PASS WITH LIMITATIONS。** 任务书范围内 8 项标准全部满足：31 项迁移
债务经独立复现失败、定位（messaging 缺失 / 无消费侧进程 / sink 注入点
错位 / skip 死代码 / 前端残留用例）、在测试夹具边界内修复并以真实双
进程链路实证；四门与全量回归干净；唯一红灯与 main 基线同态。限制项
（netem 行为矩阵、非 Redis 真实链路、real-backend E2E）均为环境/CI
门禁，已明确 NOT_RUN，不以 Mock 顶替。

**满足最终 Squash Merge 条件**（合并前建议：① 在 perf-netem runner
跑一次 `tests/system/network/` 取得 7 项行为绿灯；② 将 file_archive
E2E 与 FC15 两个 main 基线项另立任务）。

本轮停止在报告阶段：未合并、未建 PR，等待 ChatGPT 根据报告决定后续。
