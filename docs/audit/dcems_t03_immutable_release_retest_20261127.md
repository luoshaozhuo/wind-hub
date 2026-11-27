# DCEMS T03 原始发布版回归验收报告（2026-11-27）

## 0. 范围与方法

- 本轮验证：删除全部 DCEMS Java 补丁后，使用**原始、不可修改的 DCEMS 发布包**完成设备级控制闭环（T03）。
- 仓库未发现《报告模板.md》，本报告沿用既有 DCEMS T02 报告结构。
- 未修改 `third_party/dc-ems/`、`src/`、生产配置、镜像、JAR 或授权工具；不重编译、不 `docker build`、不伪造授权。
- 测试侧修复仅限 `tests/third_party/dc_ems/` 测试工具与格式化。

## 1. Git 基线

| 项目 | 值 |
|---|---|
| 分支 | `feat/dcems-gateway-smoke-20261126`（唯一工作分支） |
| 起始 HEAD | `5aa8412bc1cec44ab97f78b5d563a029fc585ee8` |
| 起始远端 HEAD | `5aa8412bc1cec44ab97f78b5d563a029fc585ee8`（与本地一致，无后续提交） |
| 本轮测试修复与证据 | 见 §8 提交表 |
| 报告提交 | 见 §8 提交表 |

未创建新分支、PR，未合并 main，未 rebase / force push。`git fetch` 后远端无新增提交，按起始 HEAD 执行。

## 2. T03-A：第三方发布包不可变性 — PASS

| 检查 | 结果 |
|---|---|
| `sha256sum -c MANIFEST.sha256` | **133/133 OK**，exit 0 |
| `dc-ems.jar` SHA256 | `1df1c053265c7d1127755b4792646a2e5f3503fd5ba6f23fae43c6cbcf9ae7d2`，与 MANIFEST、`VERSION` 的 `jar_sha256` 一致 |
| `images/dc-ems-server.tar` | `5694498dfbfd6e6fd5ad6b9759674cd217f3084e9d34c8f49ccc527713427ac7`，与 MANIFEST 一致 |
| `images/timescaledb.tar` | `8a501f98cd6cad490fe99786991ad8dba88235b0834a7b1faa69e36fe68944b5`，与 MANIFEST 一致 |
| `deploy.sh`、`runtime/containerized/compose.yaml` 等发布脚本/配置 | 与 MANIFEST 固定 SHA 一致（契约测试断言） |
| Java 补丁 | `tests/third_party/dc_ems/patches/` 不存在；历史删除提交 `6f43167c`/`c62b101c`/`f7cdc8d9` |
| 派生 JAR Compose | `tests/third_party/dc_ems/compose.wtg_patched_jar.yaml` 不存在；契约测试显式断言 |
| 测试脚本禁止派生 JAR | `run_wtg_dcems_server.sh` 对 `DCEMS_WTG_PATCHED_JAR` 直接 `fail` |
| 残留补丁调用/环境变量/断言 | 测试目录无补丁加载入口；契约测试仅保留"禁止"类断言与历史文字证据 |
| Docker 镜像未重建/替换 | `dc-ems-server:1.1.4-583d5da` → `sha256:30779caf…`、`dc-ems-timescaledb:2.7.0-pg14` → `sha256:0f5553d3…`；`Created` 分别为 `2026-09-12T13:32:51Z`、`2025-04-30T18:33:32Z`，与发布 tar 内 config 完全一致（层数 13/18 一致），非本轮重建 |
| `git status third_party/dc-ems/` | 干净 |

`dc-ems-web` 镜像不在发布包内（发布包只导出 Server + TimescaleDB），测试 Overlay 使用 Server 同端口 `/dc-ems` 静态页与 `/api`，与发布契约一致。

## 3. T03-B：静态与配置验收 — PASS

| 门禁 | 结果 |
|---|---|
| `bash -n third_party/dc-ems/deploy.sh` | PASS |
| `bash -n tests/third_party/dc_ems/run_wtg_dcems_server.sh` | PASS |
| `ruff check tests/third_party/dc_ems` | All checks passed（40 文件） |
| `ruff format --check tests/third_party/dc_ems` | 40 files already formatted |
| 四项专项 pytest（contract / preflight / prepare_runtime / seed） | **17 passed** |
| `(cd third_party/dc-ems && sha256sum -c MANIFEST.sha256)` | 133 OK |

Compose 检查：

- 原始基础 `third_party/dc-ems/runtime/containerized/compose.yaml` 未修改（MANIFEST SHA 固定）。
- wind-hub Overlay `tests/third_party/dc_ems/compose.wtg_control.yaml` 隔离：DB `127.0.0.1:15433`、Server `127.0.0.1:18453`、独立种子只读挂载、`machine-id` 只在 Overlay 挂载（发布 Compose 无该挂载，契约测试断言）。
- 数据库名强校验 `dcems_smoke`、JDBC 路径 `/dcems_smoke?`，不复用生产/dcems-smoke 库。
- 无测试专用派生 JAR 加载路径。
- `deploy.sh` 自带限制（机器码 SYSCODE 等）按第三方已知问题记录，未修改第三方脚本；隔离 JKS/`.env` 由 wind-hub 在 `run/` 隔离根内用镜像内 keytool 生成（发布包 `config/ssl/README.md` 允许的站点工件），`third_party/` 保持零改动。

## 4. T03-C：真实 DCEMS Server 启动 — PASS

### 4.1 准备

| 步骤 | 结果 |
|---|---|
| 隔离 JetStream NATS `nats:2.10-alpine`（nats-server 2.10.29）`127.0.0.1:14223` | 启动；`COMMAND_AUDIT` 流按 Commander 配置补建（见 §7-R6） |
| 9 个独立 Bearer 令牌（≥32 字符、互异，0600，仅 Shell 注入，未提交） | `run_wtg_control_chain.sh --prepare/--check/--up` 通过 |
| WTG 模式初始化 | `WTG_SIM01.control_mode=1` 经正式 CLI SUCCESS |
| 隔离 runtime | `prepare_runtime` 生成 `dcems.env`/`db.property`/JKS/`log4j`/随机 `machine-id`（0600） |
| 合法授权 | 发布 JAR 内官方 `LiscenceUtil machine-code DC-EMS`（挂载同一 machine-id）→ 官方 `GenLicense` 生成 **172 字符**授权码；写入隔离 `data.json`、`dcems.env`、DB `ems_config`（length=172）。未伪造/破解；采用 Server 实际使用的 SYSCODE `DC-EMS`（deploy.sh 打印 `DCEMS` 的已知 P1 不一致，按历史报告处理） |
| 种子 | `prepare_wtg_dcems_seed`：WTG01 单机、`host.docker.internal:15532→15531`（探针接入，见 §5）、仅 ctrlActPow/ctrlReActPow 两点、自动控制旗标禁用 |
| 预检 | `run_wtg_dcems_server.sh --check`：images/seed/runtime/jks/license 全 READY；schema/FC16 UNVERIFIED 待实机 |

### 4.2 启动结果

- `run_wtg_dcems_server.sh --up`：`dcems-wtg-control` 项目（原始镜像 `--pull never`）DB healthy → Server 启动。
- Server 进程运行；`https listened on 443`；`init datagetter ok: host.docker.internal,15531`。
- PostgreSQL/TimescaleDB 可连接：26 张 public 业务表 + `data` schema（`d_wtg01`/`d_fp`/`d_fre`/`d_inertia`）建表成功。
- Web/API：`https://127.0.0.1:18453/dc-ems/` 与 `/api/` 均 200；真实登录 `code=200` 返回会话 token。
- 授权有效：`ems_config.license` 长度 172；观察期 0 次授权异常。
- 设备配置加载：`device=WTG01`、`edge_server=host.docker.internal:15531 MODBUSTCP`、`edge_addr_set` 两个控制点 `is_ctrl=t`。
- Modbus FC03 轮询：`data.d_wtg01` 每秒写入，`ems_com_state=t`；探针统计 FC03(41555, qty=4) 持续 ~2 次/秒（一次读覆盖 41555–41558 两控制点）。
- Gateway1 收到控制请求：见 §5 全链证据。
- 启动期已知第三方缺陷（详见 §6）：`dataMain`、`reactPow` 线程 FactoryPoints NPE 死亡；6 个视图创建竞态失败。

### 4.3 记录的 SHA256

见 `evidence/environment/baseline-and-digests.txt` 与 `evidence/environment/runtime-config-digests.txt`（镜像 tar、JAR、镜像 ID、`dcems.env`/`db.property`/JKS/log4j/machine-id/种子；不输出任何值）。

## 5. T03-D：设备级控制闭环 — PASS_WITH_DCEMS_RISKS

### 5.1 测试执行

| 轮次 | 配置 | 结果 |
|---|---|---|
| 1 | 直连 15531 | 6 passed（35.08s，未落盘） |
| 2 | 直连 15531 | 6 passed（23.06s，`pytest-t03-d1-d6.txt`） |
| 3 | 透明探针（首版，语义缺陷） | 5 passed / 1 failed（D6 超时）——测试工具缺陷，非产品缺陷，见 §8 |
| 4 | 门控探针（最终） | **6 passed（22.97s，`pytest-t03-d1-d6-final.txt`）** |

最终裁决以第 4 轮为准；第 3 轮失败完整保留为测试工具整改证据，未删除用例、未放宽断言。

### 5.2 六项用例与证据链

| 编号 | 用例 | 结果 | 关键证据 |
|---|---|---|---|
| D1 | 有功 450 kW | PASS | HTTP `setParam WTG01_ctrlActPow=450` → FC16 原始帧 `…0110a255000204000043e1`（41557，F32-CDAB=450.0）→ Gateway1 protocol write event → Commander `request_id=a13ca5c1…` / `command_id=7aeb269f…` `CONFIRMED_SUCCESS` → DG `APPLIED` → Gateway2 回读 `450000 W` → **风机物理功率 450005.4 W** |
| D2 | 无功 125 kvar | PASS | HTTP `ctrlReActPow=125` → FC16 `…0110a253000204000042fa`（41555，F32-CDAB=125.0）→ `command_id=22ef14f9…` `CONFIRMED_SUCCESS` → DG `APPLIED` → Gateway2 回读 `125000 var` |
| D3 | 错误密码 | PASS | `GET /api/login` → `{"code":"400","msg":"用户密码错误"}`（多轮一致） |
| D4 | 无 token | PASS | `setParam` 无 token → HTTP 200 + `code=401`（pytest 断言）；`_assert_no_new_protocol_write` 确认无新 Gateway1 写事件 |
| D5 | 越界 3000 kW | PASS | `{"code":"400","msg":"有功设定值超出范围: 0-2000.0"}`；探针确认**无 FC16 帧**、无协议写事件 |
| D6 | Gateway1 断连及恢复 | PASS | `StopGateway` 后端口关闭，setParam 仍回 200（源码限制：Web 忽略 control() 返回值）但**无 FC16、无协议写事件**；`StartGateway` 恢复后再次控制成功（FC16 `…a255…43e1`）→ `CONFIRMED_SUCCESS` → `APPLIED` |

全链证据文件：

- `evidence/live/pytest-t03-d1-d6-final.txt`（6 passed）
- `evidence/live/dcems-server-control-evidence.json` / `dcems-server-reactive-evidence.json`（event_id→command_id→APPLIED→回读→物理功率）
- `evidence/server/http-control-requests.txt`（DCEMS 实际 HTTP 控制请求与响应）
- `evidence/server/modbus-frames-15532.jsonl`（**FC03/FC16 原始帧 hex**，1694 帧：FC03×1689、FC16×5）
- `evidence/server/modbus-frames-final-summary.json`（按功能码/地址汇总）
- `evidence/live/rejection-evidence.txt`（D3/D5 拒绝）
- `evidence/live/gateway.log`、`commander.log`、`data-generator.log`

说明：

- HTTP 200 / Web"控制成功" / Modbus ACK 均未单独作为执行证明；断言语义与 T02 一致。
- 测试真实经过 DCEMS Server（HTTP→其内部 control()→Modbus FC16），未由测试客户端代替控制出口。
- FC16 原始帧经 wind-hub 测试侧透明探针（`modbus_request_probe`，仅记录不改报文）捕获；D6 断连语义通过"上游可达才监听"门控与直连完全一致。

## 6. T03-E：DCEMS 可靠性风险观察 — RISKS_RECORDED

观察窗口：`2026-11-27T15:04:03Z → 15:09:06Z`（5 分 03 秒），连续 10 次 30s 采样。

| 观察项 | 结果 |
|---|---|
| FactoryPoints NPE | **2 次线程死亡**（启动期，`dataMain`、`reactPow` 各 1；两次 Server 启动各复现，共 `Exception in thread` 4 条）；堆栈 `FactoryPoints.checkIsLimit/optimizePower/calTheoryPower` 与 `calComOkValues`；影响线程：`dataMain`（主数据/理论功率刷新）、`reactPow`（无功刷新）——两线程启动即死，观察窗口内**无新增**（delta=0） |
| WindTurbo NPE | 0 次（日志无 WindTurbo 相关异常） |
| `dataMain` | **死亡**（启动期 NPE，未复活） |
| `actPow` | **存活**（全程无 `Exception in thread "actPow"`） |
| `reactPow` | **死亡**（启动期 NPE，未复活） |
| Web/API | 10/10 采样 `200`（API + 静态页），观察期无 5xx |
| Modbus | FC03 持续轮询（探针 +60 帧/30s），`ems_com_state` 10/10 采样 `t`，`data.d_wtg01` 52–54 行/分钟；控制写入（FC16）观察期无失败 |
| 控制闭环 | 观察期无实际控制失败；D1–D6 全部成功 |
| 授权 | 0 异常（license 长度 172 保持） |
| 数据库 | 0 新异常；启动期 6 个视图创建竞态 `SQLException`（`data.d_wtg01`/`d_fp` 尚未建表时先建视图），视图未重建，属第三方启动竞态 |

补充风险（非本轮新增，沿用历史报告）：

- Web `setParam` 忽略 `getter.control()` 返回值：断连时仍回"控制成功"（本轮 D6 实测），必须以下游事件为证。
- 启动期视图缺失影响历史/曲线类页面（`wtData/wfData` 无行，历史报告已记录）。
- 十机 Web 表格 P0 渲染缺陷（`dcems_gateway_smoke_independent_20261126`）为第三方生产缺陷，本轮未复测、不纳入 wind-hub 整改。
- `deploy.sh` 机器码 SYSCODE `DCEMS` vs Server `DC-EMS` 不一致（历史 P1）；`deploy.sh` 授权申请流程不可用，需 ChatGPT 侧修复。
- 功率因数为 0 的 Infinity/NaN 风险（历史记录，本轮未触发）。

**裁决规则应用**：DCEMS 内部异常日志不阻断 wind-hub 验收；设备级控制仍全部正确执行，故 `DCEMS_CONTROL_E2E = PASS_WITH_DCEMS_RISKS`；风险全部记录 → `DCEMS_RELIABILITY = RISKS_RECORDED`。未忽略失败断言、未过滤日志、未缩短测试时间。

## 7. 测试侧修复（允许范围内）

| # | 文件 | 修复 |
|---|---|---|
| 1 | `tests/third_party/dc_ems/test_dcems_release_deployment_contract.py` | `ruff format`（+2/−6，纯格式） |
| 2 | `tests/third_party/dc_ems/modbus_request_probe.py` | ① 记录 `raw_hex` 原始帧；② FC15/FC16 地址/数量解析；③ 上游不可达时本地拒绝连接的**门控监听**（保持 D6 refused 语义）；④ 上游断开/连接失败时向 DCEMS 侧发 RST（`transport.abort()`），与直连失败语义一致 |

修复 2 的起因：第 3 轮 D6 失败为探针半开连接导致 setParam 阻塞 16s 超时（证据 `pytest-t03-d1-d6-with-probe.txt` + `probe-stderr.txt`），属测试工具缺陷；修复后第 4 轮 6/6 通过。

### 7.1 环境准备事项（非产品缺陷）

- `COMMAND_AUDIT` JetStream 流需按 README 由线下测试基础设施预先创建；本轮初次遗漏导致 Commander 审计发布失败 88 次（`audit publication failed`），补建流后发布恢复（流消息 38+，手动发布验证 OK）。控制命令执行本身不受影响（Commander SQLite 状态机正常，pytest 全绿）。建议后续把建流并入 `--prepare`（属测试基础设施改进，本轮未改生产代码）。

## 8. 提交与 Git 状态

| 内容 | 说明 |
|---|---|
| 测试修复 | `modbus_request_probe.py`、`test_dcems_release_deployment_contract.py` |
| 报告与证据 | `docs/audit/dcems_t03_immutable_release_retest_20261127.md` + `…_evidence/` |
| `src/`、`third_party/`、生产配置 | **零改动** |
| push | 本机无 GitHub 凭据（按既有约定），提交后由用户手动 push；本地/远端 SHA 差异在交付消息中记录 |

## 9. 资源清理 — PASS

| 项 | 结果 |
|---|---|
| DCEMS Compose 项目 | 容器与网络已删除（0 残留），**卷保留**（database/files/logs/cache/models 5 个） |
| wind-hub 控制链服务 | gateway / commander / gateway-target / data-generator 全部 STOP |
| 探针进程 | 已停止 |
| NATS `dcems-t03-nats` | 已删除，`14223` 释放 |
| 端口 | `14223/15433/18453/15531/15532/15431/15421-15425/52152/52161/52173/52174` 全部释放 |
| 运行根与机密 | `run/dcems-control-wtg`、`run/dcems-t03`（令牌/授权/机器码/JKS/.env）已删除；证据目录无令牌、无 LICENSE 值、无机器码 |
| 其他项目 | `run/dcems-measurement`、`run/dcems-smoke`、`run/dcems-modbus-cli*`、`run/gap8` 与其他 NATS/卷未触碰 |
| `docker compose down -v` | 未执行；未删除任何卷 |

## 10. T04 准入检查 — T04_ENTRY = READY

### 10.1 逐项核查

| # | 检查项 | 状态 | 依据 |
|---|---|---|---|
| 1 | DG 风速/转速/有功/无功/状态测量点 | **具备** | WTG 设备模型 32+ 测量点（wind_speed、sim_rotor_speed、active_power、reactive_power、running_status、fault_status…），DG ClaimTargets 实测在册 |
| 2 | Gateway2 → Collector → Sink → Gateway1 数据路径 | **具备** | `dcems_measurement_full_chain_retest_20261126` 三轮 PASS（COLLECTOR_ACQUISITION / NATS_PUBLICATION / SINK_GATEWAY_DELIVERY / 十机对账全绿）；`read_merge_max_gap=0` 已在测量根 device 配置强制 |
| 3 | Gateway1 对 DCEMS 的工业协议测量点 | **具备** | `point_table_dcems_wtg_random_v1.0.0`（input/holding/ discrete）；`dcems_gateway_smoke_independent_20261126` 真实 Server FC04 十机风速/有功/无功数值对账 PASS |
| 4 | DCEMS 原始点表 ↔ Gateway1 映射 | **具备（9 点子集）** | `tests/third_party/dc-ems/config/mapping_random_smoke.csv`（9 业务点 × 10 台），`validate_gateway_mapping` + `test_config_smoke` 联合契约通过；本轮 T03 控制两点映射同样经契约验证 |
| 5 | 数据类型/寄存器地址/字节序/单位/缩放 | **具备** | F32 CDAB/ABCD、`highBit`、`coe=0.001`、单位断言在 `validate()` 与 smoke 测试中强制；真实 Server 数值对账一致 |
| 6 | 测量周期、时间戳、容差 | **部分具备** | 组件级：DCEMS `interval=500`、DG 批次、45 轮源/目标对账；**端到端联合容差契约未定义**（见缺口 G1） |
| 7 | 数据质量和异常值处理 | **部分具备** | wind-hub 侧故障注入/恢复三轮 PASS；DCEMS 侧落库质量表现未联合验证（见缺口 G2）；已知风险 §6 |
| 8 | 设备身份映射 | **具备** | 十机 `WTG01..WTG10 ↔ WTG_SIM01..WTG_SIM10`（测量场景）；单机 `WTG01 ↔ WTG_SIM01`（本轮控制场景），关联键为 Gateway1 Modbus 端口 |

### 10.2 缺口清单（T04 执行前置/工作项，不阻断准入）

| 编号 | 缺口 | 类型 |
|---|---|---|
| G1 | 联合根与端到端测量周期/时间戳/容差契约：T03 控制根与 `dcems-measurement` 测量根分离，尚无"同一隔离根同时运行 DCEMS Server + 控制链 + 测量全链"的 prepare/测试入口；DCEMS `interval=500ms`、DG 批次、Collector/Sink、DCEMS DB `tm` 的对齐与容差断言待 T04 建立 | 测试入口/契约 |
| G2 | 数据质量与异常值端到端（quality 字段、stale/NaN、DCEMS 落库表现）尚无联合断言 | 测试 |
| G3 | Collector 生产模板默认 `read_merge_max_gap=8`（P-1）；T04 全部配置必须固定 override=0（测量根已做，控制根/新联合根需沿用） | 配置约束 |
| G4 | 映射为 9 点子集（原始种子 49 点）；T04 需确认测量接入范围（其余点是否纳入） | 范围确认 |
| G5 | DCEMS 十机 Web 渲染 P0（第三方生产缺陷）影响 Web 类断言；T04 数据对账建议走 DB/API，Web 缺陷单列风险 | 第三方风险 |
| G6 | 控制（单机 WTG01）与测量（十机）场景的同根合并、以及站点级 P/Q 边界，需在 T04 配置中显式声明 | 配置 |

厂站级 AGC/AVC/PFR 配置重构不在本轮范围（按任务要求未开展）。

## 11. 最终裁决

| 验收项目 | 裁决 |
|---|---|
| `VENDOR_IMMUTABILITY` | **PASS** |
| `STATIC_GATES` | **PASS** |
| `SERVER_STARTUP` | **PASS** |
| `DCEMS_CONTROL_E2E` | **PASS_WITH_DCEMS_RISKS** |
| `DCEMS_RELIABILITY` | **RISKS_RECORDED** |
| `RESOURCE_CLEANUP` | **PASS** |
| `T04_ENTRY` | **READY**（附 §10.2 缺口清单为 T04 执行工作项） |

DCEMS 第三方可靠性风险汇总见 §6；全部有日志/统计证据，均不归 wind-hub 生产代码整改。

## 12. 证据路径

`docs/audit/dcems_t03_immutable_release_retest_20261127_evidence/`

```text
environment/baseline-and-digests.txt        Git/镜像/JAR/tar SHA256 与 VERSION
environment/runtime-config-digests.txt      隔离运行配置文件摘要（无值）
environment/docker-to-host-probe.txt        容器→宿主 Gateway 连通性
environment/nats-stream-check.txt           COMMAND_AUDIT 流与发布验证
environment/cleanup-actions.txt             资源清理清单
static/bash-n.txt ruff-check.txt ruff-format.txt pytest-static.txt manifest-check.txt
live/pytest-t03-d1-d6-final.txt             最终 6 passed
live/pytest-t03-d1-d6.txt                    直连 6 passed
live/pytest-t03-d1-d6-with-probe.txt         探针缺陷轮（1 failed，已整改）
live/dcems-server-control-evidence.json      D1 全链证据
live/dcems-server-reactive-evidence.json     D2 全链证据
live/rejection-evidence.txt                  D3/D5 拒绝
live/gateway.log commander.log data-generator.log（DG 日志为尾部 3000 行截断，APPLIED 全链证据以 JSON/pytest 为准）
server/http-control-requests.txt             DCEMS 实际 HTTP 控制请求
server/modbus-frames-15532.jsonl             FC03/FC16 原始帧（hex）
server/modbus-frames-final-summary.json      帧统计
server/server-full.log                       Server 完整日志
server/probe-stderr.txt                      探针 stderr（含 D6 缺陷轮证据）
reliability/observation-window.txt           5 分钟窗口
reliability/api-probes.txt                   10×30s 采样
reliability/log-stats-comparison.json        线程/NPE/SQL 前后对比
reliability/db-final-state.txt               终态 comOk/license/行数
t04/mapping-and-pointtable.txt               映射 CSV 与点表
t04/path-and-test-entries.txt                数据路径与测试入口清单
t04/point_table_modbus_wtg_dcems_control_v1.2.0.yaml
```

（运行令牌、授权码、机器码、JKS、`.env` 均不在证据目录；`run/` 运行根已清理。）
