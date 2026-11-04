# Data Generator v1_10 整改独立复测报告（第二轮）

- 复测日期：2026-11-03
- 复测人：Claude Code（独立验收，未参与 ChatGPT 整改实现）
- 基线提交（整改 HEAD）：`b540b7a119bbee96a4482f0e3f628c17c1e55566`
- 分支：`feat/data-generator-v1`（未 rebase、未 force push、未创建 PR、未合并 main）
- 设计权威：`docs/design/Data_Generator_软件设计_v1_10.md`
- 首轮报告：`docs/data-generator-v1-independent-acceptance.md`
- 复测原则：不预设整改成功；所有结论以真实执行证据为准；未执行项不计 PASS。

## 1. 起止 HEAD 与工作区状态

- 起点：`b540b7a1`（任务书指定整改提交）。
- 终点：本轮分类提交（见第 11 节；报告先于提交撰写，SHA 在提交后于第 11 节落定）。
- 工作区改动分四类：
  1. Proto 正式脚本重生成产物（4 个 `src/contracts/industrial_gateway_pb2*`）；
  2. 一处明确的小范围生产修复（`runtime.py` 错误分类）+ 四个生产文件 ruff format 规范化；
  3. 测试夹具迁移、P1/P2/OBS 新增专项、共用调用方契约同步；
  4. 本报告。
- 性能测量临时产物（`tmp-acceptance-*`）已移出仓库，未提交。

## 2. 环境与执行命令

```bash
python3 scripts/dev.py env            # conda env wind-hub 全部门禁 PASS
```

- Python 3.14.7 / grpcio 1.84.0 / **grpcio-tools 1.81.1**（正式重生成所用版本）/
  mypy 1.20.2 / ruff 0.8.6 / pytest-asyncio（auto 模式）。
- WSL2，Linux 6.18.33.2-microsoft-standard-WSL2，x86_64，16 核，MemTotal 7.4 GiB。
- 测试统一 `PYTHONPATH=src`；系统测试均驱动真实独立 `data_generator.main` 子进程。

## 3. Proto 核验（正式脚本重新生成，未直接信任提交物）

执行：`python scripts/generate_rpc.py`。

- 重生成产物与 b540b7a 提交版**存在差异**，经逐项比对确认全部来自 grpcio-tools
  版本差异（空行/docstring/参数换行/序列化描述符字节），契约实质一致。按任务书
  "以 Proto 为权威"保留重生成产物，**未修改 Proto 迁就生成物**。
- Descriptor 反射核验 7 项全部通过（`tests/data_generator/integration/test_proto_descriptor.py`）：
  1. IG 服务 **8 个 Unary RPC**（无 streaming），新增 `ClaimTargets`；
  2. `UpdateVariablesRequest.lease_token` = 字段 **3 / TYPE_STRING**；
  3. `ClaimTargetsRequest`：`gateway_id=1, owner_id=2, variable_ids=3(repeated string),
     action=4, lease_token=5, lease_ms=6(uint32)`；
  4. `ClaimTargetsResponse`：`accepted=1(bool), lease_token=2, error_code=3, message=4`；
  5. DG 6 + Common 9 = 15 RPC 不变；
  6. Python Stub/pyi 与 Proto 一致（契约矩阵 tests/contracts 92 项通过）；
  7. **无旧无令牌写入兼容路径**：正式服务端无令牌/过期/他主令牌一律
     `FAILED_PRECONDITION/LEASE_LOST`（见 P1-08 与集成测试实测）。
- `tests/contracts` 同步更新到新契约：RPC 总数 42→**43**，新增消息字段冻结断言。

## 4. 静态门禁

| 门禁 | 结果 | 证据 |
|---|---|---|
| `ruff check src tests scripts` | **PASS** | All checks passed |
| `ruff format --check src tests scripts` | 1 个既有失败 | `tests/tooling/test_merge_skill.py` 在 b540b7a 基线即未格式化（基线 worktree 复证），本轮未触碰，标记 **UNRELATED** |
| `mypy src/data_generator src/industrial_gateway` | 4 错误 **UNRELATED** | 全部位于 `commander.py`（WritePointRequest/WritePoint/COMMAND_OUTCOME_CONFIRMED_*），主线 cb380ddb 引入；b540b7a 基线 worktree 同样 4 错误 |
| `MYPYPATH=src mypy tests/data_generator` | **PASS** | Success: no issues in 30 files |
| `lint-imports` | **PASS** | Contracts: 14 kept, 0 broken |

## 5. P1 跨实例租约所有权——真实双进程/真实 Gateway 证据

测试文件：`tests/data_generator/system/test_lease_system.py`（11 个测试，全部 PASS，
总计 ~40s）；P1-02/06/07 由改造后的既有系统测试承载。竞争类场景全部使用**两个独立
DG 子进程**（`subprocess.Popen` 拉起 `data_generator.main`，非同进程双 Runtime）。

| 场景 | 证据测试 | 实测结果 |
|---|---|---|
| P1-01 同时申请恰一胜 | `test_p1_01_simultaneous_claim_exactly_one_wins` | `asyncio.gather` 并发 Start，恰 1 accepted，负方精确 `RESOURCE_BUSY` |
| P1-02 已持有拒绝 | `test_simulation_system::test_conflicting_target_ownership` | B 拒绝 `RESOURCE_BUSY`（严格 PASS，xfail 已移除） |
| P1-03 不同变量共存 | `test_p1_03_disjoint_variables_coexist` | 两进程分别持有 pitch_angle/pitch_rate，均持续真实写入且互不覆盖 |
| P1-04 多变量整组拒绝 | `test_p1_04_group_rejected_without_partial_occupancy` | B 争 [A,B] 被拒；第三方探针立即可取 VAR_B（无部分占用），VAR_A 仍 TARGET_BUSY |
| P1-05 运行中真实续租 | `test_p1_05_renewal_happens_during_generation` | 9s 观察窗：1 ACQUIRE + ≥2 RENEW（lease=12s，周期 4s），token 全程不变，owner=generation_id，lease_ms∈[10000,60000] |
| P1-06 停止即释放 | 同 P1-02 | A Stop→STOPPED 后 B **立即** Start 成功（显式 RELEASE，非等 TTL）并读到新值 |
| P1-07 SIGKILL 到期回收 | `test_lifecycle_system::test_crash_unknown_delivery` | SIGKILL 后 B 有界轮询，期间必为 RESOURCE_BUSY，实测 **lease_recovery≈10s（≤12s）** 后取得并写入 |
| P1-08 旧 token fencing | `test_p1_08_expired_token_write_fenced_by_gateway` | A TTL=1s 到期、B 取得后，A 旧 token 写入 `FAILED_PRECONDITION`；B 写入读回 7.5 |
| P1-09 Gateway 重启失效 | `test_p1_09_gateway_restart_invalidates_all_tokens` | 重启后旁路 token 写入 `FAILED_PRECONDITION`；DG 在 10s 内 `Generation FAILED` 且 last_error 含 lease |
| P1-10 双 Gateway 回滚 | `test_p1_10_second_group_failure_rolls_back_first` | gw-b ACQUIRE UNAVAILABLE → Start `GATEWAY_UNAVAILABLE`；gw-a claim_calls == ["ACQUIRE","RELEASE"]；第三方立即可取 |
| P1-11 申请超时未知 | `test_p1_11_14_claim_timeout_and_cancel_leaves_no_residue` | claim 挂起 0.5s 超时 → `GATEWAY_UNAVAILABLE`；DG 不伪造 RELEASE（仅 1 次 ACQUIRE 记录） |
| P1-12 转移无交错 | `test_p1_12_transfer_has_no_interleaved_stale_write` | 5 轮×（8 旧 token + 8 新 token 并发）：8/8 旧写入被围栏拒绝，存储值恒为新方值；服务端授权+整批写入同一把锁 |
| P1-13 抢占/释放无死锁 | `test_p1_13_concurrent_claim_storm_single_owner` | 4 轮×25 并发 ACQUIRE 恰 1 胜；占用期 TARGET_BUSY；空 token RENEW 拒绝；RELEASE 后立即再得 |
| P1-14 取消无遗留 | 同 P1-11/14 | 客户端取消后放行挂起处理，0.3s 后第三方 ACQUIRE 立即成功（无残留占用） |
| P1-15 伪造 owner 续租 | `test_p1_15_forged_owner_renewal_rejected` | 持真 token + 伪造 owner_id RENEW → `LEASE_LOST`；真 owner RENEW 仍成功且可写 |

**P1 裁定：关闭。** 真实跨进程竞争、旧 token fencing、显式释放、SIGKILL TTL 回收全部通过。

## 6. P2 定向复测

- **P2-1 六类错误码**（`test_error_classification_system.py`，4 个真实子进程测试 PASS；
  其余两类由 P1 测试承载）：变量不存在/类型不匹配/不可写 → `TARGET_VALIDATION_FAILED`
  且零写入；连接失败 → `GATEWAY_UNAVAILABLE`；租约占用 → `RESOURCE_BUSY`（P1-01/02）；
  租约丢失 → Generation FAILED/LEASE_LOST（P1-08/09）。
- **P2-2 diagnose 许可**（`test_diagnose_system.py`，3 新测试 + 1 重写，PASS）：
  默认 `permit_wait_seconds=0.5`，挂起窗口实测排队 **0.4≤waited≤1.2s** 后
  `RESOURCE_EXHAUSTED`；配置 2.0 时窗口内释放许可，排队诊断成功（不误杀）；
  排队等待方客户端取消后许可不泄漏（后续诊断立即成功）；越界值 0/0.005/10.5
  使 `--check` 非零退出且诊断含 `permit_wait_seconds`；诊断全程零写入、write 恒 SKIPPED。
- **P2-3 指标序列**（component + system 双层 PASS）：max_series 有界截断以
  `data_generator.metrics_truncated=1` 显式标记且替换最后一条；无截断不误报；
  空窗口无 P 系列（不伪造 P99）；scope 过滤只返回 `gateway:<id>` 序列。
- **P2-4 restart-required Apply**（`test_config_system::test_restart_required` PASS）：
  `file_commit=COMMITTED`、`restart_required=True`、`effective_revision` 仍为旧 runtime
  修订、DISK 修订已变更；`ApplicationResult.message` 含
  `disk_revision=<new>; effective_revision=<old>`；重启后进程按磁盘修订运行，事务目录无残留。

## 7. OBS 观测性实测（`test_observability_system.py`，2 测试 PASS）

- **OBS-1**：逻辑超时仅 1 个真实原生调用、UNKNOWN 计数、隔离期零盲重发；
  gate 恢复后受控 RENEW 探测（1/3 租期周期）解除隔离；恢复后 UNKNOWN 计数冻结
  （旧批次不重放），新批次 confirmed≥2，全部调用严格限定本目标（无越权/无重复）。
  过期 token 不获写权见 P1-08/09；跨网关在途峰值有界见既有扇出背压测试（≤max_inflight）。
- **OBS-2**：挂起窗口 `rpc_native_started_total == 服务端实受 UpdateVariables 数 == 1`，
  而 `batches_total > 1`（派发尝试口径，含跳过/隔离拒绝），两口径真实分叉且各自可对账；
  `global_max_active ≤ max_inflight=2`；恢复后 native_started 与服务端调用数始终相等，
  `gateway.success_total == confirmed_total`。

## 8. 四层回归

| 层 | 命令 | 结果 |
|---|---|---|
| Unit/Component/Integration | `pytest tests/data_generator -q`（整目录含 system） | **169 passed, 0 failed**（235s） |
| Contracts | `pytest tests/contracts` | **92 passed** |
| Core Architecture | `pytest tests/core/architecture` | **18 passed** |
| Industrial Gateway | `pytest tests/industrial_gateway` | **62 passed, 17 failed（全部 UNRELATED）** |

17 个失败全部位于 r2 command/modbus 套件（business 4、control_echo 5、fc15 1、
pipeline 3、startup 2、modbus_real 2），根因是 pymodbus 协议栈异常响应与
`configs/industrial_gateway.yaml` 路径重构（FileNotFoundError），**均不调用
UpdateVariables/ClaimTargets，与租约无关**；在 b540b7a 基线 worktree 同名同因复现，
main（origin/main）更是收集期 ImportError（gateway_pb2 退役重构中）。标记 **UNRELATED**，
未为全绿扩大范围。

## 9. Gateway / Contracts / Core 影响面与共用调用方同步

- 全仓 UpdateVariables 调用点核查：生产仅 `gateway.py:383`（强制携带 claim token）；
  测试调用方已全部迁移——DG 集成/系统测试、`tests/industrial_gateway/test_data_grpc.py`
  （旧 2 个失败已修复，7 测试全过）、性能脚本 `measure_acceptance.py`（先 ACQUIRE 60s 租约）。
- 契约测试同步：RPC 矩阵 43、字段冻结含 lease_token/ClaimTargets 六字段。
- IP 白名单同时守护 ClaimTargets 与 UpdateVariables（PERMISSION_DENIED 双断言）。
- token 保密审计：fencing token 不出现在 logger/record/事件/message/快照中
  （生产代码 grep 核查；UNKNOWN_OUTPUT 记录仅含 gateway:variable 与 error_code）。
- Core architecture 18 项通过，分层边界未被破坏。

## 10. 性能复测（真实执行，机器/负载/样本量齐全）

命令：`PYTHONPATH=src python tests/data_generator/performance/measure_acceptance.py`
（WSL2 16 核 / grpcio 1.84.0；测量代码已迁移为带租约令牌路径）。

| 指标 | 样本量 | P50 | P95 | P99 | MAX |
|---|---|---|---|---|---|
| 直连 UpdateVariables RTT（含围栏授权） | 2000（+200 预热） | 1.080ms | 1.487ms | 1.695ms | 2.101ms |
| DG 端到端生成→写入链路 | 599 批次（12s） | 2.894ms | 3.576ms | 3.810ms | — |
| replay 启动到首批 | rows=1,000 / 200,000 | 1.120s / 2.344s（CSV 12.5MB，SQLite 30.2MB） | | | |
| 30s 长稳 | 1500 confirmed | RSS 68.8→68.9MB（Δ+0.0），FD 15→15，无泄漏 | | | |

## 11. 残余缺陷与复现

1. **[UNRELATED] mypy src 4 错误**：`industrial_gateway/infrastructure/grpc/commander.py`
   引用主线新 Proto 尚不存在的 WritePoint* 符号（主线 cb380ddb 引入，b540b7a 同样存在）。
2. **[UNRELATED] ruff format**：`tests/tooling/test_merge_skill.py` 基线即不达标。
3. **[UNRELATED] industrial_gateway 17 失败**：r2/modbus 协议栈与配置路径重构，
   基线/main 同因；复现：`pytest tests/industrial_gateway/test_gateway_r2_business.py`。
4. 本轮唯一生产修改（`runtime.py`）：TARGET_QUARANTINED 是"结果未知"本地安全态，
   不应在受控 RENEW 探测前误杀 Generation；仅 LEASE_LOST/LEASE_NOT_HELD 致命。
   该修复属任务书允许的"明确小范围生产缺陷修复"，未触及租约协议/安全边界/状态机设计。

无 BLOCKED / NOT_RUN 项；任务书要求的验证全部真实执行。

## 12. 裁定

# **PASS**

- P1（15 场景）、P2（4 项）、OBS（2 项）全部以真实执行证据关闭；
- 四层回归 DG 自身 169 全绿，Contracts/Architecture 全绿；
- 跨模块失败均经基线 worktree 实证为 UNRELATED，未通过放宽断言/skip/扩兼容层处理；
- Proto 以权威重生成，无旧无令牌写入路径残留；
- 唯一生产改动为任务书授权范围内的小修复，重大设计问题零发现、零擅自重设计。

## 提交清单与推送状态

本轮 4 个分类提交（基于 b540b7a1，测试代码与生产修复分开提交）：

| SHA | 内容 |
|---|---|
| `8ff12bc8` | chore(contracts)：正式脚本重生成 Proto 产物 |
| `7f1f62b9` | fix(data-generator)：QUARANTINED 分类小修 + 3 文件纯格式化 |
| `364cb73c` | test：P1/P2/OBS 专项、夹具迁移、共用调用方契约同步 |
| `bda77440` | docs：本报告 |

**推送阻断（本地提交已保留）**：`git push origin feat/data-generator-v1` 失败——
HTTPS 远端无可用凭据（`fatal: could not read Username for 'https://github.com'`），
SSH 通道无公钥（`Permission denied (publickey)`，`~/.ssh` 无 .pub）。
未 rebase、未 force push、未创建 PR；需在有凭据环境执行推送。
