# DCEMS 基础数据接入与部署独立验收复测报告（2026-11-26）

## 1. 裁决与范围

**最终裁决：BLOCKED。**

本轮启动时的本机没有 DCEMS 发布镜像，也没有上一轮运行产物。按任务强制约束第 4 条
（禁止重新构建镜像、必须复用本机已有镜像），不能自行构建或拉取
`dc-ems-server`、`dc-ems-web`、`dc-ems-timescaledb`，也不能从零复现上一轮的
隔离运行目录与授权。因此：

- wind-hub 侧 Data Generator → Industrial Gateway → 真实 Modbus TCP 十机链路
  **已真实执行并通过三轮复测**；
- DCEMS Server、Web、TimescaleDB、WebSocket、浏览器、历史数据库、
  Server 异常统计**均未运行**，状态为 `BLOCKED` / `NOT_RUN`；
- 未修改 wind-hub `src/`、生产配置或 `third_party/dc-ems/` 任何文件；
- 未发送任何 AGC/AVC/PFR 控制命令，未启动 Commander，PFR 外部输入全程 false；
- 未写入任何数据库、未伪造 WebSocket 数据。

本报告不是生产 READY 或 READY_TO_MERGE 结论。

## 2. Git 基线与边界

| 项 | 值 |
|---|---|
| Repository | `luoshaozhuo/wind-hub` |
| 开发分支 | `feat/dcems-gateway-smoke-20261126` |
| 指定 / 实测起始 HEAD | `24c2ee3ed0fc161e32563bfb65eff79b01a3fe10` |
| fetch 后 `origin/feat/...` | 与起始 HEAD 一致 |
| 起始工作树 | 干净 |
| 上一轮整改起点 | `3c86251b209870f3273ad35b49226da7d18770a3` |
| 本轮两提交 | `37acd797`、`24c2ee3e`（均已 fetch，逐文件复核） |
| 本轮整改提交 | `0dfb77aa`（测试与审计整改）；本报告提交为最终 HEAD，SHA 由交付消息给出 |
| PR / main 合并 / rebase / force push | 均未执行 |

修改范围仅限：

- `tests/third_party/dc-ems/config/`（测试专用契约 YAML）；
- `tests/third_party/dc_ems/`（测试、审计、操作说明）；
- `docs/audit/`（本报告）。

运行证据在 `run/dcems-smoke/`（`.gitignore` 已覆盖），不含凭据。

## 3. 运行环境与镜像实际状态（本轮硬阻断）

| 组件 | 实测 |
|---|---|
| OS | WSL2，`6.18.33.2-microsoft-standard-WSL2` |
| Python | 3.14.7，`~/miniconda3/envs/wind-hub`；`scripts/dev.py env` = PASS |
| pytest / ruff / mypy | 8.4.2 / 0.8.6 / 1.20.2 |
| pymodbus / websockets / PyYAML | 3.15.0 / 17.1 / 6.0.3 |
| Playwright | 本轮重新安装 `1.63.0` 包；浏览器二进制未安装（堆栈不可运行，未做浏览器验收） |
| Docker / Compose | 29.8.0 / v5.5.1（Docker Desktop 引擎，默认 socket） |
| DCEMS release | `1.1.4-583d5da`（`third_party/dc-ems/VERSION`） |

必需镜像逐一检查（证据 `evidence/docker-state.txt`）：

| 镜像 | 结果 |
|---|---|
| `dc-ems-server:1.1.4-583d5da` | **MISSING** |
| `dc-ems-web:1.1.4-583d5da` | **MISSING** |
| `dc-ems-timescaledb:2.7.0-pg14` | **MISSING** |
| `dc-ems-java:8` / `dc-ems-nginx:1.26-alpine` 及其基础镜像 | **MISSING** |

本机仅有 8 个与本任务无关的镜像（redis/nats/tdengine/kafka 等）。
Compose 项目 `dcems-smoke` 的容器 0 个、卷 0 个；启动时不存在
`run/dcems-smoke/`，即上一轮的隔离配置、种子、JKS、授权与证据均已不存在。

同时确认：

- `third_party/dc-ems/MANIFEST.sha256` 列出的 `images/dc-ems-server.tar`、
  `images/timescaledb.tar` 不在工作树中，磁盘上也没有对应离线包；
- 本机无 Java 运行时、无 PostgreSQL/TimescaleDB，host-based 模式同样不可执行；
- `.env.local` 不含 `DCEMS_LICENSE_CODE`，本机 DCEMS 授权不可恢复。

因此第 6 节要求的“使用已有镜像启动数据库、Server、Web”无法执行，
`run_local.sh --check` 在缺少派生种子处即拒绝（`evidence/run-local-check.log`）。
按约束未尝试 `docker build`、`docker pull` 或改写发布包。

## 4. 本轮两个提交的实现复核

### 4.1 `37acd797` 十机逐台动态刷新与外部接口审查

复核结果：

- 浏览器用例由“只检查 WTG01 刷新”改为逐台采样；
- 设备行定位使用精确文本匹配，不依赖首个下划线解析；
- 冻结判据为单台样本集合基数 `< 2`，缺失单台即失败；
- 外部接口审查由“任一设备绑定”改为“同类型设备全部绑定”
  （`audit_external_signals.py` 的 `eligible and all(...)`）；
- 新增 `avc_grid_target`（`emsGridDispatchReact`）与 `avc_enabled`
  （`emsReactPowerEnable`）；
- 新增“部分设备未绑定即不完整”的负向用例。

本轮补强（见 4.3）：

- 设备行匹配数量 `> 1` 时显式判歧义，而不是静默跳过；
- 增加同一轮扫描中各行数值不能全部相同，覆盖历史“所有行解析成首台设备”缺陷；
- 新增逐台全测点实时验证（功能码/地址/类型/字序/常量/区间/动态）；
- 新增 AVC 页面基础导航测试；
- 审计新增 `pfr_factory_mode`（`emsIsFreMod`），明确其不是独立协议入口。

### 4.2 `24c2ee3e` 控制使能唯一性与安全复测边界

复核结果正确：

- `prepare_seed.build_seed` 在派生完成后要求
  `emsActPowerEnable`、`emsReactPowerEnable`、`bFreCtrlEnableFlag`
  各自恰好一条且全部为 false，否则抛 `ValueError`；
- 三个“缺少一个”、一个“重复一条”的负向用例均存在并通过；
- `stage_config` 删除了重复的同一条件；
- README 明确禁止 false→true→false 提前激励，要求保存异常计数与历史证据。

该守卫有实际意义：发布源码 `FactoryConfig.java:398/403` 中
`emsReactPowerEnable = new Value<>(false, true)`、`emsActPowerEnable = new Value<>(false, true)`
的第二参数是“当前值”，即未显式配置时默认 **true**。测试种子必须覆盖为 false。

### 4.3 本轮测试实现缺陷与修复

| 问题 | 处理 |
|---|---|
| 两个新提交引入 4 个文件格式不合规（ruff format 未过） | `ruff format` 修复，不改变断言语义 |
| 浏览器行匹配 `count != 1` 时静默跳过，失败信息只显示 missing | 增加 `ambiguous` 列表并单独断言 |
| 无“多行同值”检测，历史串台缺陷可能被逐台刷新掩盖 | 每轮扫描记录各行数值，要求单轮至少 6 个不同值 |
| 仅实时验证风速，未覆盖十机有功/无功/限值/转速/桨距/故障 | 新增 `test_gateway_ten_turbine_all_points_match_scenario_contract` |
| 无 AVC 页面导航验收 | 新增 `test_browser_avc_page_is_navigable_without_control_action` |
| 审计契约未显式包含 `emsIsFreMod` | 新增 `pfr_factory_mode` + `DERIVED_INTERNAL` 状态与用例 |

以上均未降低既有断言或容差。

## 5. 静态门禁

全部在仓库根目录、wind-hub Conda 环境执行，设置 `PYTHONPATH="$PWD/src:$PWD"`。

| 检查 | 命令摘要 | 退出码 | 结果 |
|---|---|---:|---|
| 环境 | `python3 scripts/dev.py env` | 0 | PASS |
| Ruff（变更范围） | `ruff check tests/third_party/dc_ems` | 0 | All checks passed |
| Ruff format（变更范围） | `ruff format --check tests/third_party/dc_ems` | 0 | 19 files already formatted |
| mypy（变更范围） | `mypy tests/third_party/dc_ems` | 0 | 19 files，0 error |
| pytest 收集 | `pytest --collect-only -q tests/third_party/dc_ems` | 0 | 60 tests |
| 非实时测试 | `pytest -q tests/third_party/dc_ems --ignore=.../test_live_smoke.py` | 0 | 29 passed |
| Bash 语法 | `bash -n run_local.sh run_windhub.sh` | 0 | 通过 |
| Compose 配置 | `docker compose ... config --quiet` | 0 | 通过 |
| Git whitespace | `git diff --check` | 0 | 无错误 |
| 官方 Loader | `pytest test_official_loader.py`（含于 29 passed） | 0 | 真实 SharedDocument/Gateway/DG Loader |
| 仓库门禁 `ruff check src tests scripts` | `scripts/ci_gate.py:116` 同款 | 1 | **5 个既有错误，3 个既有文件，UNRELATED** |
| 仓库门禁 `ruff format --check src tests scripts` | 同上 | 1 | **7 个既有文件，UNRELATED** |
| 仓库门禁 `mypy src` | 同上 | 0 | 483 files，0 error |

初始状态与修复：

- 变更范围初始 format 失败：4 个文件（`prepare_seed.py`、
  `test_external_signals.py`、`test_live_smoke.py`、`test_prepare_seed.py`），已修复；
- 变更范围初始 mypy 失败 1 项：重建后的环境未安装 `playwright`
  （`import-not-found`），安装 1.63.0 后通过；属环境缺失，不是代码缺陷；
- 仓库级 Ruff 失败文件均为本轮未触碰的既有文件：
  `src/storage_service/.../kafka_consumer.py`、`.../tdengine_backend.py`、
  `tests/storage_service/integration/test_tdengine_wide_probe.py`、
  `scripts/audit_business_points.py`、`src/configuration_service/.../schema_catalog.py`、
  `src/sink_service/infrastructure/config_io.py`、`.../tdengine_schema.py` 与
  `docs/audit/*_20261123_evidence/*.py`。判 `UNRELATED`，不扩大任务范围。

证据：`evidence/static-dcems-tests.log`、`evidence/static-repo-gate.log`、
`evidence/mypy-src.log`、`evidence/nonlive-tests.log`、
`evidence/compose-and-bash-check.log`。

## 6. 隔离部署执行情况

| 步骤（任务第 6 节） | 状态 | 说明 |
|---|---|---|
| 检查镜像与实际 ID | PASS | 三镜像全部缺失，已记录 |
| 停止上一轮残留服务 | PASS | 无容器、无进程、无监听 |
| 归档旧运行配置与证据 | PASS | 启动时 `run/` 不存在，无可归档 |
| 重新生成 wind-hub 隔离配置根 | PASS | `run_windhub.sh --prepare` |
| 通过官方 Loader/CLI 校验配置 | PASS | Gateway/DG `--check`，非实时 Loader 测试 |
| 生成 DCEMS 数据库种子 | BLOCKED | 需 `release-config`、授权、JKS，均不存在 |
| 生成 DCEMS 运行配置 | BLOCKED | `prepare_runtime` 依赖 `release-config` |
| 全新隔离数据库初始化 | BLOCKED | 无 TimescaleDB 镜像 |
| 启动 Gateway / DG | PASS | 正式 CLI，三轮各自启动 |
| 启动 DCEMS 数据库/Server/Web | BLOCKED | 必需镜像缺失 |
| 设备/通道/使能核对 | 部分 PASS | 派生种子内存构建：12 设备、10 通道、12 点、四个使能 false；运行库 NOT_RUN |

派生种子内存校验（`evidence/external-signals-audit-derived-seed.json`）：

```text
devices  = PFR, SUBSTATION, WTG01..WTG10   (12)
channels = 10
points   = 12
control  = emsActPowerEnable=false, emsReactPowerEnable=false,
           bFreCtrlEnableFlag=false, kk_flag=false
```

旧库复用、`down -v`、范围外清理均未发生。

## 7. Gateway 工业协议十机逐台结果（PASS，三轮）

每轮执行：

```bash
DCEMS_LIVE=1 pytest -q tests/third_party/dc_ems/test_live_smoke.py \
  -k "gateway_modbus_random_input_changes or \
      station_power_and_primary_frequency_mode_over_gateway or \
      all_points_match_scenario_contract"
```

| 轮次 | 启动 UTC | 测试窗口 UTC | 结果 | DG 批次 | failed / unknown | 停止 |
|---|---|---|---|---|---|---:|---|
| R1 | 03:51:20 | 03:58:24–03:59:24 | **21 passed** (59.35s) | 972 | 0 / 0 | CPU 进程停止，端口清零 |
| R2 | 03:59:49 | 03:59:59–04:01:00 | **21 passed** (59.37s) | 141 | 0 / 0 | 同上 |
| R3 | 04:01:17 | 04:01:27–04:02:28 | **21 passed** (59.38s) | 141 | 0 / 0 | 同上 |

启动后约 6 秒内 10 个 listener（15221–15230）全部就绪；DG 每批
`confirmed=93 failed=0 unknown=0`（10 台 × 9 点 + 首台 3 个站控/调频点）。

逐台按点表功能码/地址/类型/字序读取的实测（R3 首末样本）：

| 设备 | 风速 m/s | 有功 W | 无功 var | 限值 kW | 转速 rpm | 桨距/最小桨距 ° | 模式 | 故障 |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| WTG01 | 10.50 → 5.84 | 905542 → 951393 | -55133 → 63834 | 2000 | 1500 | 5 / 0 | 32 | false |
| WTG02 | 10.62 → 12.72 | 852722 → 986731 | -76899 → -14815 | 2000 | 1500 | 5 / 0 | 32 | false |
| WTG03 | 17.37 → 14.90 | 357816 → 986100 | 42406 → 34130 | 2000 | 1500 | 5 / 0 | 32 | false |
| WTG04 | 7.24 → 5.06 | 597868 → 646607 | -30235 → 24914 | 2000 | 1500 | 5 / 0 | 32 | false |
| WTG05 | 14.51 → 12.44 | 304544 → 895675 | 39218 → 26522 | 2000 | 1500 | 5 / 0 | 32 | false |
| WTG06 | 10.29 → 7.21 | 475785 → 922389 | -29601 → 73308 | 2000 | 1500 | 5 / 0 | 32 | false |
| WTG07 | 7.29 → 8.49 | 873420 → 640621 | 27747 → 10053 | 2000 | 1500 | 5 / 0 | 32 | false |
| WTG08 | 4.42 → 11.74 | 818023 → 372467 | -99620 → -74506 | 2000 | 1500 | 5 / 0 | 32 | false |
| WTG09 | 5.11 → 8.85 | 596945 → 567153 | 16033 → 54416 | 2000 | 1500 | 5 / 0 | 32 | false |
| WTG10 | 6.51 → 7.19 | 874603 → 225380 | 36905 → 29875 | 2000 | 1500 | 5 / 0 | 32 | false |

站控与调频层（首台 Gateway，同一测试内断言）：

| 项 | 实测 | 结果 |
|---|---|---|
| `sim_plant_active_power` W | 994677 → 1118460 → … → 1099324，6 样本均变化 | PASS |
| `sim_plant_reactive_power` var | 221152 → 116804 → -145812 → … | PASS |
| `primary_frequency_mode` FC02 | 6/6 样本 false | PASS |

说明：站控 P/Q 是独立随机激励，**不是十台机组功率物理求和**，本轮不宣称
电厂物理模型正确。数据类型、字序与功能码由解码方式与场景区间共同验证：
F32 ABCD（FC04）、I32 ABCD（FC04）、bool（FC02 discrete input）。

证据：`evidence/gateway-round{1,2,3}.log`、`evidence/gateway-points-WTG01..10.json`、
`evidence/round{1,2,3}-gateway.log`、`evidence/round{1,2,3}-data-generator.log`。

## 8. DCEMS Server / Web 十机实时显示

状态：**BLOCKED / NOT_RUN**。

未执行项目（依赖运行中的 DCEMS）：

- 十机 DCEMS 接收值与 Gateway 数值对账（含 W/var→kW/kVar）；
- WebSocket 订阅与动态刷新；
- Playwright 十机逐行数值、单位、唯一行、刷新；
- 首页升压站 P/Q 动态显示；
- AGC / AVC / PFR 页面真实导航与显示；
- 登录与基础页面导航。

本轮没有把这些项目标为 SKIPPED 或 PASS。代码侧新增/补强了对应断言，
待镜像与授权恢复后可直接执行。

## 9. PFR 三层状态传播

| 层级 | 测点 | 本轮状态 | 证据 |
|---|---|---|---|
| Gateway | FC02 `primary_frequency_mode` | **PASS = false** | 3 轮逐台测试，6/6 样本 false |
| DCEMS 设备变量 | `PFR_isFreMode` | **NOT_RUN** | 无 Server/WebSocket |
| DCEMS 全场变量 | `fp_emsIsFreMod` | **NOT_RUN** | 同上 |
| Web 调频页面 | “调频信号” | **NOT_RUN** | 无浏览器/Web |

对 `fp_emsIsFreMod` 的源码复核（承接上一轮 FAIL 证据，非本轮实测）：

```text
ConfigLoader.addDataChanged  → 仅注册 Value.dataChanged
Value.update                 → 仅当 lastVal == null 或值不相等才触发 eventsChange
FactoryPoints.emsIsFreMod    = new Value<>(false, false)   // 初始 val=false
YctpDevice.initEvents        → isFreMode.dataUpdate(...) → emsIsFreMod.update(false)
```

初始 false 与当前值相等，`dataChanged` 不触发，`MemorySet.cedis` 不会写入
`fp_emsIsFreMod`，`WsPublisher` 的 `G.getter.refresh(key)` 返回 null，键因此缺席。
这解释了上一轮“设备 false 正确、全场键缺失、页面空白”的现象。
false 与 null/缺失键/空串/未初始化在测试中被严格区分，不视为等价。

## 10. Server 周期性异常

状态：**BLOCKED / NOT_RUN**（无 Server 进程、无日志）。

源码复核（`FactoryPoints.checkIsLimit`）：

```java
return (emsFacDispatchActEnabled.getVal() || emsGridDispatchActEnabled.getVal()
        || emsJkDispatchActEnabled.getVal()) && ...
```

三个使能均以单参数 `new Value<>(false)` 构造，只设置类型、`val` 为 null；
控制关闭时没有事件写入当前值，`Boolean` 拆箱抛 NPE，被 catch 后每周期记录一次。
上一轮记录的 278 次/最终快照属于历史证据，本轮无法复现或更新计数。
该异常被捕获不代表 Server 健康，需在生产侧修复后重新统计。

## 11. 历史数据库

状态：**BLOCKED / NOT_RUN**。无数据库容器、无卷、无历史表/视图可查。

上一轮的 `data_tables=15 / pldata_views=0 / energy_report_view=0` 与
`data.d_fp` 建表失败属于历史事实，本轮不重复、不篡改。
新设备 ID（无下划线）不改变历史表/视图的建表顺序问题；需在恢复运行后重新核查。

## 12. 外部控制接口静态审查

`audit_external_signals.py` 已运行，输出
`evidence/external-signals-audit.json`（发布种子）与
`evidence/external-signals-audit-derived-seed.json`（十机派生种子）。

派生种子结果：设备/站控/调频设备输入全部 `BOUND`；
`emsIsFreMod` 为 `DERIVED_INTERNAL`；三个控制使能为 `INTERNAL_CONFIG`；
以下 5 项仍为 `UNPROVEN_FACTORY_INPUT`。

| 变量 | 含义 / 单位 | 协议入口 | 设备类型 | Getter / 写入 | 消费者 | 配置可实现性 | 阻断原因 |
|---|---|---|---|---|---|---|---|
| `emsGridDispatchAct` | AGC 有功目标 / kW | 无 | FactoryPoints | 无设备 getter | `ActPowerFun:489`、`FreActPowerFun:77`、`EmsDispatcher.emsGridDispatchActChanged` | 否 | 无 FactoryPoints 设备协议适配 |
| `emsGridDispatchReact` | AVC 无功目标 / kVar | 无 | FactoryPoints | 无设备 getter | `ReactPowerFun:64-82`、`EmsDispatcher.emsGridDispatchReactChanged`、`FactoryPoints:887` | 否 | 同上；另有反馈写错，见 P-5 |
| `emsFreDst` | 一次调频目标有功 / kW | 无 | FactoryPoints | 无设备 getter | `EmsDispatcher.actFreFunValueChanged`、`FreActPowerFun` | 否 | 同上 |
| `emsDeltapValue` | 调频增量功率 / kW（哨兵 > -9999999） | 无 | FactoryPoints | 无设备 getter | `FreActPowerFun:85-86` | 否 | 同上 |
| 电网频率 | Hz | 无 | — | 无 | `FreActPowerFun` 不消费频率测量值，仅 `iCtrlFrePoint` 模式与目标功率 | 否 | 需算法输入设计 |
| `emsIsFreMod` | 一次调频模式 / bool | FC02（经 YctpDevice） | YctpDevice（type 2） | `isFreMode` → `emsIsFreMod.update` | `FreActPowerFun:17`、`EmsDispatcher:84/115/130/152` | 是（设备绑定） | 全场缓存传播缺陷（P-1） |
| `emsActPowerEnable` | AGC 总使能 / bool | 内部配置 | FactoryConfig | 种子 `emsConfig` | AGC 算法 | 是 | 发布默认 true，种子里强制 false |
| `emsReactPowerEnable` | AVC 总使能 / bool | 内部配置 | FactoryConfig | 种子 `emsConfig` | AVC 算法 | 是 | 同上 |
| `bFreCtrlEnableFlag` | 一次调频总使能 / bool | 内部配置 | FactoryConfig | 种子 `emsConfig` | PFR 算法 | 是 | 保持 false |

结论：内部变量存在、同名测点存在、算法消费者存在，均**不**证明工业协议入口已闭合。
本轮只审查，未开发接口、未启用控制。

## 13. 三轮稳定性

| 轮次 | 十机动态采集 | 站控 P/Q | PFR 层 1 | Server 异常 | 数据库 | 重启回归 |
|---|---|---|---|---|---|---|
| R1 | PASS | PASS | PASS(false) | NOT_RUN | NOT_RUN | — |
| R2 | PASS | PASS | PASS(false) | NOT_RUN | NOT_RUN | PASS（完整停止→重启） |
| R3 | PASS | PASS | PASS(false) | NOT_RUN | NOT_RUN | PASS（再次停止→重启） |

三轮均为独立进程启动，未用平均值掩盖任何一轮；每轮结束后 10 个端口与
2 个进程均清零。DCEMS 参与的稳定性轮次为 NOT_RUN，不能据此宣称整体稳定。

## 14. 已修复问题与复测证据

| 项 | 修复 | 复测 |
|---|---|---|
| 4 文件格式不合规 | `ruff format` | format/check 0，19 files |
| 浏览器行歧义静默跳过 | 显式 `ambiguous` 断言 | 非实时/静态通过，实时 NOT_RUN |
| 历史串台可能漏检 | 单轮 ≥6 个不同值 | 同上 |
| 十机测点覆盖不足 | 新增逐台全测点用例 | **3 轮 × 10 台 PASS** |
| AVC 页面未验收 | 新增导航用例 | NOT_RUN |
| 审计缺 `emsIsFreMod` | 新增 `pfr_factory_mode` / `DERIVED_INTERNAL` | 非实时用例 PASS |
| 环境缺 Playwright | 安装 1.63.0 | mypy/收集通过 |

## 15. 未关闭问题、严重等级、根因与最小整改建议

| 编号 | 等级 | 问题 | 根因 | 最小整改建议 |
|---|---|---|---|---|
| E-1 | **BLOCKER** | DCEMS 三镜像、离线包、JKS、授权、`release-config` 均不存在 | 本机环境/发布物缺失 | 由维护方恢复 `dc-ems-server/web:1.1.4-583d5da`、`dc-ems-timescaledb:2.7.0-pg14` 与本机合法授权后重跑本报告实时套件；不得以构建镜像绕过 |
| P-1 | P0（生产） | `fp_emsIsFreMod` 不进公共缓存，Web 调频信号空 | `Value` 仅值变化才触发 `dataChanged`，初始 equal 值不写缓存 | 启动装载后把 FP 当前值写入 `MemorySet.cedis`，或让 `Value.update` 产生初始事件，或 `WsPublisher` 缺键时直读 FP 当前值；补 false 初始与 false→true→false 用例 |
| P-2 | P0（生产，已被测试规避） | 发布 Web `fullFilePage.vue:411` 以首个下划线切设备 ID | 解析假定设备 ID 无下划线 | 生产侧改为按最后一个下划线或行 `deviceId` 组装键；本轮以无下划线设备 ID 规避，不改发布包 |
| P-3 | P1（生产） | `FactoryPoints.checkIsLimit` 周期 NPE | 单参数 `Value` 使能 `val=null`，控制关闭无写入 | 使能显式初始化当前值，或 `checkIsLimit` 做 null 安全短路；补控制关闭回归 |
| P-4 | P1（生产） | 历史视图/报表缺失 | 建表/建视图顺序与启动回读缺失 | 启动后校验必需视图并进入明确未就绪状态；恢复后重跑 `d_fp`/`pldata`/`energy_report` 查询 |
| P-5 | P1（生产） | `EmsDispatcher.java:286` 把 `emsGridDispatchAct` 写入 `emsAvcFeedback` | 复制粘贴错误 | 改为 `emsGridDispatchReact`；与 `FactoryPoints:887` 一致；用不同 AGC/AVC 目标值专项验证 |
| P-6 | P1（生产） | AGC/AVC/PFR 目标与电网频率无协议入口 | 无 FactoryPoints 设备适配层 | 新增逻辑设备/协议适配，定义单位、质量位、时间戳、失联策略；本轮仅审查 |
| V-1 | 验证缺口 | 十机 Web 刷新、WebSocket、浏览器、历史查询未执行 | E-1 | 环境恢复后直接运行第 7/8/9/10/13 节同日测试与浏览器证据采集 |

## 16. 对后续完整测量链路开发的影响

- wind-hub 侧十机数据生成、Gateway 协议、站控/PFR 输入已可独立运行并稳定复现；
- 完整测量链路（Gateway → DCEMS Server → Web）目前 **不能** 验收，
  阻塞点是运行环境与发布物（E-1），不是本轮测试代码；
- E-1 解除前，不应把“基础数据接入 PASS”用于后续控制闭环或生产准入；
- P-1/P-3/P-5 是生产源码缺陷，需由维护方修复后才能验收 Server 健康与 PFR 传播；
- P-2 已由无下划线设备 ID 规避，但发布包缺陷仍需生产侧修复以防回归。

## 17. 最终裁决

| 验收项 | 裁决 |
|---|---|
| `BASIC_DATA_ACQUISITION` | **BLOCKED**（Gateway 侧 PASS；DCEMS 接收 NOT_RUN） |
| `TEN_TURBINE_WEB_REFRESH` | **BLOCKED** |
| `PFR_MODE_PROPAGATION` | **BLOCKED**（层 1 PASS=false；层 2/3 NOT_RUN） |
| `SERVER_RUNTIME_HEALTH` | **BLOCKED** |
| `HISTORICAL_DATA` | **BLOCKED** |
| `DEPLOYMENT_REPRODUCIBILITY` | **FAIL**（必需镜像/发布物/授权缺失，按约束不得重建） |
| `EXTERNAL_CONTROL_INTERFACE` | **FAIL**（5 项外部输入未闭合；内部使能正确关闭） |
| `FULL_MEASUREMENT_CHAIN_DEVELOPMENT` | **BLOCKED** |
| 总体开发准入 | **BLOCKED** |

不给出 READY / READY_WITH_LIMITATIONS / READY_TO_MERGE 结论。

## 18. 证据路径

运行证据根：`run/dcems-smoke/evidence/`（Git 忽略，不含凭据）。

```text
docker-state.txt                        必需镜像/容器/卷实际状态
run-local-check.log                     DCEMS 启动预检拒绝点
static-dcems-tests.log                  变更范围 ruff/format/mypy/collect
static-repo-gate.log                    仓库门禁既有失败
mypy-src.log                            mypy src 483 files
nonlive-tests.log                       29 passed
compose-and-bash-check.log              compose config / bash -n
external-signals-audit.json             发布种子审计
external-signals-audit-derived-seed.json 十机派生种子审计
gateway-round{1,2,3}.log                三轮 21 passed
gateway-points-WTG01..10.json           逐台全测点样本
round{1,2,3}-{gateway,data-generator}.log 各轮进程日志
```

本报告提交后，最终 HEAD 由交付消息给出；本轮未创建 PR、未合并 main、
未 push 到远端（本机无 GitHub 凭据，按仓库既有约定由用户推送）。
