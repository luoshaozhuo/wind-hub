# DCEMS T04 十机测量接入与统一环境联测报告（2026-11-28）

## 0. 范围与方法

- 在 T03 基础上完成**十机测量 + 单机控制**统一跨系统联测：真实测量数据从 Data
  Generator 经 Gateway2 → Collector → Sink → Gateway1 进入原始 DCEMS Server，
  并在同一根内完成设备级控制回归与测量反馈闭环。
- 目标裁决：`T04_MEASUREMENT_E2E`、`T04_CONTROL_REGRESSION`、`T05_ENTRY`。
- DCEMS 为不可变第三方：未修改 `third_party/dc-ems/`、Java 源码/JAR/镜像；
  内部缺陷只记录风险。wind-hub 生产代码零改动；测试侧修复仅限
  `tests/third_party/dc_ems/`。
- 未创建分支/PR、未合并 main、未 rebase/force push。

## 1. Git 基线

| 项目 | 值 |
|---|---|
| 分支 | `feat/dcems-gateway-smoke-20261126`（唯一工作分支） |
| 起始 HEAD | `41e540205f2b12d4fde6e616a10d856c43f99fb6` |
| 起始远端 HEAD | `41e540205f2b12d4fde6e616a10d856c43f99fb6`（fetch 后一致，无新增） |
| 本轮提交 | 见 §11 |

## 2. T04-A：统一测试根 — PASS

统一根 `run/dcems-t04`（Git 忽略），由 `run_t04_unified.sh --prepare/--check/--up/--down`
一键编排；全部 wind-hub 服务经正式 CLI 启停（gateway / gateway-target / commander /
data-generator / collector / sink 六服务）。

### 2.1 架构落地

```text
DG(t04-unified-ten: WTG01 物理 + WTG02-10 恒值随机)
  → Gateway2 dcems-source（15221-15230，执行侧含 WTG01 控制白名单）
  → Collector(interval 500ms, read_merge_max_gap 生效值=0)
  → NATS 14222（JetStream，COMMAND_AUDIT 同流）
  → Sink(10 deliveries) → Gateway1 dcems-target（15121-15130，WTG01 控制入口）
  → 探针(15321-15330 透明转发+抓帧) → DCEMS 原始 Server(https://127.0.0.1:18453)
DCEMS FC16 → Gateway1 → Commander(52152) → Gateway2 → DG 物理风机
```

- 同一测试根、同一批设备身份（WTG01..WTG10）、同一组 Gateway 实例；
  未拼接两套独立环境。
- DCEMS 使用原始镜像 `dc-ems-server:1.1.4-583d5da` + TimescaleDB，独立 Compose
  项目 `dcems-t04`（新数据卷），隔离 NATS/端口/日志；未复用生产库与 T03 卷。
- `read_merge_max_gap=0` 在**共享快照合成后的生效协议参数**中断言
  （设备覆盖 > 型号 > 协议模板默认8），并经 `parse_modbus_config` 复核最终
  driver 值=0（`test_t04_unified_config.py`，源/目标各10台全通过）。
- 服务 `--check`（gateway×2/DG/collector Loader）+ 离线契约7项全绿。

### 2.2 G6 联合拓扑映射表

完整表：`evidence/config/topology-mapping-table.json`。摘要：

| DCEMS ID | wind-hub ID | Gateway1(target) | Gateway2(source) | 测量点表 | 控制点表 | 方向 | 采样周期 |
|---|---|---|---|---|---|---|---|
| WTG01 | WTG01 | 127.0.0.1:15121 uid1 | 127.0.0.1:15221 uid1 | unified(target)+modbus_wtg_sim(source) | unified holding41557/41555 CDAB | 测量+控制 | DG0.05s / Collector500ms / DCEMS500ms |
| WTG02..10 | WTG02..10 | 127.0.0.1:15122-15130 uid1 | 127.0.0.1:15222-15230 uid1 | unified(target)+dcems_wtg_random(source) | — | 测量 | 同上 |

设备身份显式配置于 instance/scenario/delivery 文件，无隐含命名推断；
DCEMS 派生种子将10台 edgeServer 指向探针端口15321-15330（转发至15121-15130）。

## 3. T04-B：九点映射与49点覆盖 — PASS

- 映射基线：T03 已验证的 `mapping_random_smoke.csv`（9点，地址/字节序未改）。
- 统一点表 `point_table_dcems_wtg_unified_v1.0.0` = 9测量点（输入/离散，big_endian）
  + 2控制点（holding 41557/41555，little_endian/CDAB，来自 T03 控制契约）；
  与映射 CSV 的联合契约测试通过（地址、字节序、类型、单位逐点断言）。
- 每点验收字段（DCEMS点名/BusinessPoint/设备/功能码与地址/类型字序/单位缩放/
  周期/范围/质量/误差）固化于
  `evidence/measurement/t04-nine-point-reconciliation.json` 与契约测试。
- **49点覆盖矩阵**（`evidence/measurement/49point-coverage-matrix.json`）：

| 分类 | 数量 |
|---|---|
| 已映射、已验证（九点测量 E2E） | 9 |
| 已映射、已验证（控制 E2E / 控制输出契约） | 2 |
| 本轮不适用（控制契约 NOT_MAPPED 未接入） | 7 |
| 本轮不适用（未纳入九点映射，T05 前按需扩展） | 31 |
| **合计** | **49** |

**本报告不将9/49表述为49/49。** 无废弃兼容点表引入。

## 4. T04-C：真实测量端到端 — PASS

数据真实经过 DG → Gateway2 → Collector → Sink → Gateway1 → DCEMS；
未向 DCEMS 数据库写入任何模拟测量值。

| 检查 | 结果 |
|---|---|
| 十机身份 | PASS：10台 wind_speed 互不相同（WTG01 物理连续值 + WTG02-10 恒值常量6..16 m/s），逐台对账无串号 |
| 9点逐点对账 | PASS：10×9 全部通过（随机机与期望恒值逐点相等；物理机与 Gateway1 同期包络/夹逼一致，DCEMS kW = Gateway W×0.001 coe 换算正确） |
| 类型/字节序 | PASS：F32 big_endian（测量）/CDAB（控制）；FC16 帧解码=450.0/125.0 |
| FC03/FC04 真实轮询 | PASS：探针233,511帧（FC04 addr0 qty20 宽读、FC03 41555 qty4、FC02 addr1 qty1，十机 ~2次/秒/口） |
| 采样周期 | PASS：FC04 帧间隔中位数≈0.5s（DCEMS edge interval=500ms），证据 `t04-probe-summary.json` |
| DCEMS 实际收到并使用 | PASS：`data.d_wtg01..d_wtg10` 落库值与源一致；`ems_com_state` 全 t |
| 连续性/无串号 | PASS：30分钟观察中 WTG02-10 全程连续（见 §7）；恒值点杜绝串号掩盖 |
| 过期值/跳变 | 见 §5（Sink 事故窗口内 WTG01 曾以冻结值持续落库，已记录为发现） |
| Gateway1↔DCEMS 一致 | PASS：`test_gateway1_and_dcems_read_consistency`（包络对账，10台） |

### 时间戳与容差

时钟基准：主机 UTC（探针/DG/DCEMS `tm` 在 T03 已实测同基准）。证据
`evidence/measurement/t04-timestamp-pipeline.json`：

| 阶段 | 实测 | 容差（依据） |
|---|---|---|
| DG 源时间 → DCEMS 行（旅行时延） | **0.09s**（断言通过） | ≤5s（DG0.1s + Collector max_staleness3000ms + DCEMS1s 行周期推导） |
| Gateway1 样本 → 采样时刻 | ≤5s 断言通过 | 同上 |
| DCEMS 行写入器新鲜度（行 tm vs 墙钟） | 实测停顿 0~30s 后恢复 | 单设45s界（第三方写入器停顿，非 wind-hub 链路） |
| 同一样本关联 | DCEMS 行值 = 该行 tm 时刻 Gateway 值（实测逐点一致） | 湍流包络 ±1.5m/s（TI=0.12→σ≈1.1） |

DCEMS 不暴露内部读取时间戳：以协议抓帧行时间 + DB `tm` + DG `source_time`
建立可复核上下界（间接证据已在证据文件标注）。

## 5. T04-D：数据质量与异常输入 — PARTIAL

### 5.1 质量状态落点

| 状态 | wind-hub 内部真实位置 | Gateway1 对外 | DCEMS 观察 |
|---|---|---|---|
| GOOD | WTG02-10 全点 + WTG01 除故障点外：ReadVariables `quality=GOOD, freshness=CURRENT` | Modbus 正常返回工程值 | 正确值落库 |
| UNKNOWN | WTG01 `device_fault_active`：目标变量 `quality=UNKNOWN, freshness=UNINITIALIZED` | **寄存器默认 False**（`stored is None → False`） | 恒为 false（可能掩盖真实故障） |
| NOT_COLLECTED | 同上单元格：从未被注入 | 默认值供给，无“未采集”信号 | 无法区分“真无故障”与“未采集” |
| BAD / UNCERTAIN | Collector 侧具备枚举；本轮故障注入场景由断连覆盖 | Modbus 无质量字段 | **协议不可表达** |
| 过期值 | Sink 事故窗口（见5.3）：目标值冻结，Gateway `freshness` 仍标 CURRENT | 继续返回冻结值 | `comOk=t` 且行继续落库（旧值） |

**关键发现（wind-hub 生产缺口，交 ChatGPT）**：物理 WTG01 的
`fault_status(int32)` 无法注入 `device_fault_active(bool)`——
`VariableStore.validate_value` 拒绝 int→bool（最小复现
`evidence/quality/int-to-bool-repro.txt`：`TypeError: bool requires a boolean value`）；
编码/解码/校验链无任何强制转换。实时证据：源 `fault_status GOOD/CURRENT`，
目标 `device_fault_active UNINITIALIZED`，DCEMS 恒读 false。
WTG02-10（随机 bool 源）不受影响，全链 GOOD。

### 5.2 异常输入与断连

- 非法输入入口拒绝：`control_mode=99` 物理机拒绝；随机机写入拒绝；
  DCEMS 越界3000kW → `code=400` 且**零协议写事件**（`test_illegal_values_are_rejected_at_entry`）。
- 断连隔离与恢复（`test_disconnect_isolation_and_recovery`，D6 同型）：
  停 `dcems-target-01` 工业监听 → 端口拒绝、DCEMS `ems_com_state→f`（不再获得新值）、
  **其余九机不受影响持续更新**；恢复监听后 comOk→t、新样本恢复、freshness=CURRENT。
  实测记录：Sink 注入走 gRPC，与工业监听独立（断连期间内部存储可继续更新）——已记录。
- 重连后恢复新数据：PASS。

### 5.3 本轮真实事故（记录，非掩盖）

运行中（01:37:25Z）10条 Sink Delivery 全部自发进入
`FAILED: previous Delivery stop has not safely settled`（`stop()` 等待 settle
未完成且无超时 → 永久卡死）。目标侧冻结约4分钟；期间 DCEMS 以 `comOk=t`
持续落库**冻结值**（Modbus 无质量字段，属5.1“过期值”行）。重启 sink 服务后
全部恢复 RUNNING，目标新鲜度回到0.14s。证据：
`evidence/quality/sink-deliveries-failed.txt`。
定级：wind-hub 生产健壮性 P2（stop() 无超时界 + 失败需人工重启），已附状态
机行号复现路径，交 ChatGPT。初始触发原因未定位（观察期间无外部 stop 调用），
标记 UNKNOWN 触发源、已做最小诊断。

## 6. T04-E：同根设备级控制回归与反馈 — PASS

13项 live 测试最终轮 **13 passed（9m07s）**：
`evidence/live/pytest-t04-full.txt`。

| 编号 | 用例 | 结果 | 关键证据 |
|---|---|---|---|
| D1 | 有功450kW+测量反馈 | PASS | HTTP setParam → FC16 `…0110a255…000043e1`(41557,CDAB=450.0) → Gateway1 event → Commander `CONFIRMED_SUCCESS` → DG `APPLIED`(step) → Gateway2 源寄存器回读450000W → **物理响应** → 测量链回 DCEMS `ems_pow_act=446.73kW`（450±25，响应62.96s，`t04-d1-evidence.json`） |
| D2 | 无功125kvar+物理反馈 | PASS | FC16 `…a253…000042fa`(41555,=125.0) → CONFIRMED_SUCCESS → APPLIED → 回读125000var → **DCEMS `ems_pow_react=124.99988 kvar`**（8.28s；寄存器回读与物理响应分别判定） |
| D3 | 错误密码 | PASS | `{"code":"400","msg":"用户密码错误"}` |
| D4 | 无 token | PASS | `code=401` 且零协议写事件 |
| D5 | 越界3000kW | PASS | `code="400" 有功设定值超出范围: 0-2000.0` 且零 FC16 帧 |
| D6 | Gateway1 断连恢复 | PASS | 断连期 Web 回200（源码限制）但**无协议写事件**；恢复监听+重连等待后再次控制全链成功 |

- 反馈链完整：`DCEMS→FC16→GW1→Commander→GW2→DG 物理→测量链→DCEMS`
  全部真实经过；HTTP 200/Modbus ACK 未作为执行证据。
- 有功记录：设定450kW、DCEMS观察446.73kW、响应62.96s、误差≈0.7%；
  无功：设定125kvar、观察124.99988kvar、响应8.28s。
- FC16 帧共12条（多轮 D1/D2/D6），全部41557/41555、CDAB 编码正确
  （`evidence/server/modbus-frames-summary.json`）。

## 7. T04-F：30分钟稳定性 — PASS（附1次已恢复事故）

窗口 `01:16:44Z → 01:48:24Z`（31m40s，61样本×30s）：
`evidence/stability/samples.jsonl`。

| 观察项 | 结果 |
|---|---|
| Collector/Sink/GW1/GW2 进程 | wind-hub 进程数恒为7，无退出 |
| DCEMS 容器 | server+db 全程 Up |
| DCEMS Web/API | 61/61 = 200 |
| 十机测量连续性 | WTG02-10 全程 `com=t` 且60s行数52-58（无中断）；WTG01 在01:46-01:48出现 `com=f` 4个样本=**本方断连测试窗口**（预期） |
| 消息积压/丢失 | DG 批次持续 `confirmed=113 failed=0 unknown=0`；探针帧单调增长（全量233,511总帧，无回退） |
| Modbus 超时/重试 | 探针帧间隔稳定；断连窗口外无异常 |
| Gateway1 新鲜度 | 事故窗口外 ≤1s（恢复后实测0.14s） |
| DCEMS 线程 | **0 次线程死亡**（dataMain/reactPow 持续捕获型 NPE，见 §8） |
| 授权/数据库 | 0 异常（license172保持） |
| Sink Delivery 事故 | 01:37 自发 FAILED → 01:41 重启恢复（§5.3，P2 已记录） |

“关键风机数据持续中断且无法恢复”未发生（事故4分钟内恢复；其余九机全程无中断）。

## 8. DCEMS 第三方可靠性风险（RISKS_RECORDED）

完整统计：`evidence/server/dcems-log-stats.json`（Server 全日志）。

| 风险 | 观测 |
|---|---|
| FactoryPoints NPE | **4,554 行**（`checkIsLimit`/`calComOkValues` 等），dataMain 5,809 / reactPow 830 次错误行；本轮**线程未死亡**（捕获型） |
| WindTurbo NPE | **已实际观测**：`WindTurbo.checkEmsLimitActPowerMonitor:1535` NPE ×404（`Caused by: NullPointerException`），影响功率反馈监控计算 |
| dataMain/actPow/reactPow | 0 次线程死亡（与 T03 启动即死不同：本轮为逐周期捕获 NPE） |
| 视图创建竞态 | 6 个 `create view` SQLException（启动期 data 表未就绪），视图未重建 |
| 行写入器停顿 | 周期性 0~30s 停顿后恢复（第三方调度），已单列容差界 |
| Web 忽略 control() 返回值 | D6 实测断连期仍回“控制成功”（既有已知限制） |
| 授权 | 0 异常 |
| 数据库 | 除启动期视图竞态外 0 异常 |

全部为 DCEMS 内部缺陷，不阻断 wind-hub 裁决；实际测量/控制断言未因此失败。

## 9. wind-hub 生产发现（交 ChatGPT 整改）

| # | 级别 | 问题 | 复现/证据 | 建议 |
|---|---|---|---|---|
| F1 | P1 | Sink→Gateway 注入链无 int→bool 强制转换：物理设备 `fault_status(int32)` 无法进入 `device_fault_active(bool)`，DCEMS 恒读默认 False（真实故障被掩盖） | `evidence/quality/int-to-bool-repro.txt` + `quality-states.json`（源 GOOD/目标 UNINITIALIZED） | 在 Sink 目标映射或 Gateway UpdateVariables 处做显式窄化转换（0/1→bool），或提供 bool 质量源；保持类型契约测试 |
| F2 | P2 | Sink `RealtimeDelivery.stop()` 等待 settle 无超时：一旦挂起，`_stopping` 永真，所有 Delivery `FAILED: previous Delivery stop has not safely settled`，需重启服务 | `evidence/quality/sink-deliveries-failed.txt`；`src/sink_service/application/realtime_delivery.py:89,141-163` | stop() 各步加超时与失败回滚；FAILED 后允许自动重试 start |
| F3 | P2（观察） | 目标变量样本冻结时 `freshness` 仍标 `CURRENT`（事故窗口实测30s+旧样本标 CURRENT） | §5.3 窗口内 ReadVariables 输出 | freshness 以 sample_timestamp 距今计算并对接 max_staleness |
| F4 | 记录 | DG 物理步长过小需求（默认满功率需求+大步长）会触发超速粘性跳闸（Generation 生命周期内不恢复） | 本轮 dt=0.1+默认 setpoint=rated 触发一次，重启 generation 恢复；证据 `measurement/dg-log-before-trip-fix.log` | T04 已改 dt=0.05+安全 setpoint 初始化；建议物理场景启动时强制安全初值 |

均为**已解释**发现（附复现与行号）；无未解释 P0/P1。

## 10. 静态与配置门禁 — PASS

| 门禁 | 结果 |
|---|---|
| `bash -n` deploy.sh / run_wtg_dcems_server.sh / run_t04_unified.sh | PASS |
| `ruff check tests/third_party/dc_ems` | All checks passed（44文件） |
| `ruff format --check` | 44 files formatted |
| T02 基线四专项 pytest | **17 passed**（T02/T03 基线未受本轮新配置影响） |
| T04 配置契约 pytest | **7 passed**（含生效 gap=0、混合场景、交付映射、Commander 目标） |
| `(cd third_party/dc-ems && sha256sum -c MANIFEST.sha256)` | 133 OK |
| `git status third_party/` | 零改动（无 Java 补丁/派生 JAR/发布文件变更） |
| 新配置不影响 T02 基线 | 四专项17 passed；`run_wtg_dcems_server.sh` 仅新增可选 `DCEMS_COMPOSE_PROJECT`（默认值不变，向后兼容） |

## 11. 失败与重试记录（如实）

1. 配置构建期：点表协议头缺失、业务目录1.5.0/1.6.0混钉、物理场景 points 声明
   违规、场景 gateway_id 与 server_id 不一致（DG 预检 NOT_FOUND）——均在
   `--check` 阶段暴露并修复后重建根。
2. 物理超速跳闸（dt=0.1+默认满功率需求）：重启 generation，改 dt=0.05+安全
   setpoint 初始化（F4）。
3. 测试方法论三轮修正：变化点瞬时值对比 → 双端夹逼 → 行 tm 中心包络
   （适配 DCEMS 写入器停顿与湍流，未放宽随机机恒值逐点相等断言）。
4. D6 首轮失败：恢复后 DCEMS 重连重试 ~16s 超过事件等待——增加恢复等待
   （监听+重连周期）与40s事件等待，断言语义不变。
5. Sink 事故（§5.3）：重启 sink 恢复；事故窗口内一致性/对账断言失败为真实
   现象暴露，未绕过。
6. 证据文件一度误存令牌值：已清除并修正测试代码（证据复检无令牌）。
7. 最终轮：**13/13 passed**（`pytest-t04-full.txt`）。

## 12. 资源清理 — PASS（进行于交付前）

| 项 | 结果 |
|---|---|
| wind-hub 六服务 + 探针 | `run_t04_unified.sh --down` 停止 |
| DCEMS Compose 项目 `dcems-t04` | 容器/网络删除 |
| NATS `dcems-t04-nats`（14222） | 删除，端口释放 |
| 端口 | 15121-15130/15221-15230/15321-15330/14222/18453/15433/52051/52053/52061/52073/52074/52152 全部释放 |
| 保留数据卷（按约定列出，供复核） | `dcems-t04_database`、`dcems-t04_files`、`dcems-t04_logs`、`dcems-t04_cache`、`dcems-t04_models`（T03 的 `dcems-wtg-control_*` 卷未触碰） |
| 敏感文件 | `run/dcems-t04`（令牌/授权/机器码/JKS/.env/种子）整体删除；证据目录无令牌/授权值 |
| 其他项目 | `run/dcems-measurement`、`run/dcems-smoke`、`run/dcems-modbus-cli*`、`run/gap8` 及其容器/卷未触碰；未执行 `down -v` |

## 13. 最终裁决

| 验收项 | 裁决 |
|---|---|
| `VENDOR_IMMUTABILITY` | **PASS** |
| `UNIFIED_TEST_ROOT` | **PASS** |
| `TEN_WTG_MEASUREMENT` | **PASS** |
| `NINE_POINT_MAPPING` | **PASS** |
| `MEASUREMENT_E2E` | **PASS** |
| `TIMESTAMP_TOLERANCE` | **PASS**（链路旅行≤5s；DCEMS 写入器停顿单列45s界并记录） |
| `QUALITY_E2E` | **PARTIAL**（GOOD/UNKNOWN/NOT_COLLECTED 落点与断连恢复已验证；WTG01 故障点受 F1 生产缺口影响未送达 DCEMS；Modbus 质量字段协议不可表达） |
| `CONTROL_REGRESSION` | **PASS** |
| `FEEDBACK_LOOP` | **PASS** |
| `STABILITY_30MIN` | **PASS**（31m40s；1次 Sink 事故4分钟内恢复，F2/F3 已记录） |
| `STATIC_GATES` | **PASS** |
| `DCEMS_RELIABILITY` | **RISKS_RECORDED** |
| `T05_ENTRY` | **READY** |

`PARTIAL` 不表述为 PASS。`QUALITY_E2E` 的 PARTIAL 仅因 F1（已解释、附复现）
与协议固有不可表达项，不构成未解释 P0/P1。

## 14. T05 准入判断 — READY

- 统一测试根建立完成 ✓
- 十机测量真实进入 DCEMS ✓（233,511 帧、10×9 对账、31分钟连续性）
- 九点映射逐点对账通过 ✓
- 同根设备级控制及反馈通过 ✓（D1/D2 物理响应回读 DCEMS）
- 不存在**未解释**的 P0/P1 wind-hub 链路缺陷 ✓（F1-F4 全部附复现与行号，
  交 ChatGPT 整改；F1 建议在 T05 前修复）
- 所有 DCEMS 第三方风险已记录 ✓（§8）

T05（厂站级 AGC/AVC/PFR）启动前建议：修复 F1；确认 F2 不在控制关键路径；
厂站级点表/AGC 映射另行立项（本轮未开展，符合任务范围）。

## 15. 证据路径

`docs/audit/dcems_t04_measurement_control_e2e_20261128_evidence/`

```text
environment/baseline-and-digests.txt      Git/镜像/JAR/tar SHA256
environment/runtime-config-digests.txt    隔离运行配置摘要（无值）
environment/docker-to-host-probe.txt      容器→宿主连通性
static/bash-n.txt ruff-*.txt pytest-*.txt manifest-check.txt
config/topology-mapping-table.json        G6 十机拓扑映射表
measurement/t04-nine-point-reconciliation.json   10×9 逐点对账
measurement/49point-coverage-matrix.json         49点覆盖矩阵
measurement/t04-probe-summary.json               FC03/04/02 采样统计
measurement/t04-timestamp-pipeline.json          全链时间戳与时延
measurement/dg-log-before-trip-fix.log           超速跳闸窗口（F4）
live/pytest-t04-full.txt                 最终 13 passed
control/t04-d1-evidence.json             D1 全链+反馈（446.73kW/62.96s）
control/t04-d2-evidence.json             D2 全链+物理无功（124.99988kvar）
control/t04-d6-evidence.json             D6 断连恢复
control/rejection-evidence.txt           D3/D4/D5 拒绝
quality/quality-states.json              四态落点与 F1 缺口
quality/int-to-bool-repro.txt            F1 最小复现
quality/disconnect-recovery.json         断连隔离与恢复
quality/sink-deliveries-failed.txt       F2 事故现场
server/modbus-frames.jsonl               抓帧分层采样（每20帧取1+全部FC16；全量计数在 summary）
server/modbus-frames-sampling.json      采样说明（分层：每20取1+全FC16）
server/modbus-frames-summary.json        FC16×12 明细
server/http-control-requests.txt         DCEMS 实际 HTTP 控制请求
server/server-full.log                   Server 完整日志
server/dcems-log-stats.json              NPE/线程/视图统计
stability/samples.jsonl window.txt       30分钟×61样本
```

（运行令牌、授权码、机器码、JKS、`.env`、种子均不在证据目录；`run/` 已清理。）
