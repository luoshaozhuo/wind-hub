# DCEMS Gateway 随机十机独立线下复测报告（2026-11-26）

## 1. 裁决与范围

**最终裁决：BLOCKED。**

真实数据面已经建立并取得数值证据：

```text
Data Generator random（10 台风机）
  → Industrial Gateway（10 个真实 Modbus TCP listener）
  → DCEMS Server（真实 Modbus 采集）
  → DCEMS WebSocket / Web
```

但两项核心 Web 验收未通过：

1. AGC 单机视图只有 `DCEMS_WTG01` 显示数值，`DCEMS_WTG02..10` 为空白；已定位到 Web 生产源码对含下划线的设备 ID 解析错误。
2. FC02 的一次调频设备值和 DCEMS 设备键均为 `false`，但 `fp_emsIsFreMod` 不出现在 WebSocket 推送中，调频页面的“调频信号”为空。

因此不能裁决为 `PASS` 或 `PASS_WITH_LIMITATIONS`。AGC、AVC、一次调频控制闭环均为 **BLOCKED / NOT_RUN**，本任务没有开启自动控制、没有直接写实时数据库、没有伪造控制成功。

## 2. Git 基线与边界

| 项 | 值 |
|---|---|
| Repository | `luoshaozhuo/wind-hub` |
| 开发分支 | `feat/dcems-gateway-smoke-20261126` |
| 指定/初始 HEAD | `feb6fba7969247f1a6dfb2e0cd769d1378521f6d` |
| 基线 main | `9a17dcc253128e0e0685afb7f29899bed214093d` |
| fetch 后远端开发分支 HEAD | 与指定 HEAD 一致 |
| 最终 HEAD | 为本报告及修复所在的最终提交；精确 SHA 随交付消息给出 |
| PR / main 合并 | 未创建 PR，未合并或修改 main |

初始工作树干净。修复只涉及：

- `tests/third_party/dc-ems/` 测试专用 YAML/CSV；
- `tests/third_party/dc_ems/` 测试、启动工具、Compose Overlay 和操作说明；
- 本报告 `docs/audit/`。

**未修改 `src/`、`configs/`、`third_party/dc-ems/`。** 运行时密码、Token、授权、JKS、种子及数据库均只位于被忽略的 `run/dcems-smoke/`，未提交。

运行时证据根目录：`run/dcems-smoke/evidence/`。该目录不是 Git 交付物，不包含应提交的凭据；报告仅引用相对路径。

## 3. 实际环境

| 组件 | 实际版本/状态 |
|---|---|
| OS | WSL2，Linux `6.6.87.2-microsoft-standard-WSL2` |
| Python | 3.11.16，`~/miniconda3/envs/wind-hub` |
| pytest / Ruff / mypy | 8.4.2 / 0.8.6 / 1.20.2 |
| pymodbus / Playwright | 3.15.0 / 1.63.0 |
| PyYAML / websockets | 6.0.3 / 17.1 |
| Docker Client/Server | 29.2.1 / 29.2.1，Docker Desktop |
| Docker Compose | v5.0.2 |
| 浏览器 | Playwright Chrome for Testing 153.0.8010.12，真实 Chromium 渲染 |
| DCEMS release | `1.1.4-583d5da`；发布 `VERSION` 标记 `git_dirty=true` |

本地镜像：

| 镜像 | 内容 ID | 最终状态 |
|---|---|---|
| `dc-ems-server:1.1.4-583d5da` | `sha256:30779cafbfc62090718db55e93c9bb857b2f7e831dad4ccc927cd8a137fe4e4e` | 容器 Up |
| `dc-ems-web:1.1.4-583d5da` | `sha256:d957dc01a7d441346b093ddf4cddede491c2bebcf4834051bf6b321441402057` | 容器 Up |
| `dc-ems-timescaledb:2.7.0-pg14` | `sha256:0f5553d37abbe0f20bfe0a04dab5d1238955b5cda2711aba403090f95b6e51ff` | healthy |

证据：`docker-runtime-status.txt`、`final-windhub-processes.txt`、`final-listeners.txt`。

### 隔离与授权处理

- 数据库固定为 `dcems_smoke`，Compose 项目及卷固定为 `dcems-smoke`。
- 只删除过本任务刚创建且标签为 `com.docker.compose.project=dcems-smoke` 的失败初始化卷；未删除其他业务卷，未使用 `down -v`。
- 最终数据库有 12 条设备记录、10 条 Gateway 通道、12 条协议点记录。
- 上游发布包按设计不含现场 `.env`/JKS。本任务从本机现场资源派生隔离目录，生成全新随机数据库密码。
- 旧授权与当前设备不匹配，Server 明确拒绝启动。随后使用本机 DCEMS 官方管理员 `liscense-1.0.jar`，针对 bridge 模式下当前 Server 输出的机器码生成一年期授权。
- 授权仅在空库首次导入；通过全新隔离数据库完成导入。最终日志授权错误数为 0，HTTPS 成功监听。
- 本机旧 JKS 与 Server 固定口令契约不符；重新生成真实 JKS，并用 `keytool -list` 验证。未修改原始证书或发布包。

证据：`isolated-volume-inspect.txt`、`database-contract.txt`、`database-counts.txt`、`server-final.log`、`server-log-summary.json`。

## 4. 静态检查与实际结果

所有最终命令均在仓库根目录、wind-hub Conda 环境下执行，设置：

```bash
export PYTHONPATH="$PWD/src:$PWD"
```

| 检查 | 实际命令摘要 | 退出码 | 结果 |
|---|---|---:|---|
| Ruff | `ruff check tests/third_party/dc_ems` | 0 | All checks passed |
| Ruff format | `ruff format --check tests/third_party/dc_ems` | 0 | 19 files already formatted |
| mypy strict | `mypy tests/third_party/dc_ems` | 0 | 19 source files，0 error |
| pytest collection | `python -m pytest --collect-only -q tests/third_party/dc_ems` | 0 | 42 collected |
| 非实时测试 | `python -m pytest -q tests/third_party/dc_ems --ignore=.../test_live_smoke.py` | 0 | **23 passed** |
| Docker Compose | `bash tests/third_party/dc_ems/run_local.sh --check` | 0 | preflight OK |
| Git whitespace | `git diff --check` | 0 | 无错误 |

最终汇总日志：`final-static-gates.log`、`final-compose-check.log`。

### 初始失败而非隐去的历史

- 初始 Ruff：16 errors；format：9 files would be reformatted。
- 初始 mypy：17 errors。
- 初始收集：38 tests；非实时：23 passed。
- `test_live_smoke.py` 原风机采样把 `address = 0` 写进注释，导致 F821/name-defined；已修复。
- 生产包未发布 `py.typed`，仅对三个真实 Loader import 添加带原因的 `import-untyped` 抑制，仍调用真实 Loader，没有改成 YAML 假解析。

原始日志：`static/ruff-check-initial.log`、`static/ruff-format-initial.log`、`static/mypy-initial.log`、`static/pytest-collect-initial.log`、`static/pytest-static-initial.log`。

### 配置闭包

生产 `SharedDocument`、Deployment 选择器、Gateway `load_gateway/assemble_gateway`、DG `load_settings` 均实际执行。10 台设备、10 个 Endpoint、Deployment→Service→Instance→Template 引用闭包通过。

最终每台 WTG 的只读输入：

| DCEMS 字段 | BusinessPoint | Modbus | 类型/单位 |
|---|---|---|---|
| `emsWinSpd` | `wind_speed` | `1.3.0` | F32 ABCD，m/s |
| `emsWorkMode` | `sim_running_status` | `1.3.6` | I32 ABCD，固定 32 |
| `emsPowAct` | `sim_active_power` | `1.3.8` | F32 ABCD，W，DCEMS ×0.001 |
| `emsPowReact` | `sim_reactive_power` | `1.3.10` | F32 ABCD，var，DCEMS ×0.001 |
| `emsActPowerMaxSetFeedback` | `active_power_limit` | `1.3.12` | F32 ABCD，kW，固定 2000 |
| `emsGenRotateSpd` | `generator_speed` | `1.3.14` | F32 ABCD，rpm |
| `emsBladeAngle1` | `blade_1_pitch_angle` | `1.3.16` | F32 ABCD，degree |
| `emsMinBladeAngle` | `pitch_angle` | `1.3.18` | F32 ABCD，degree |
| `emsFaultState` | `device_fault_active` | `1.1.1` | FC02 bool，固定 false |

额外逻辑设备共用首台 Gateway：

| DCEMS 设备/字段 | BusinessPoint | Modbus | 换算 |
|---|---|---|---|
| `DCEMS_SUBSTATION.powAct` | `sim_plant_active_power` | `1.3.2`，F32 ABCD | W→kW，×0.001 |
| `DCEMS_SUBSTATION.powReact` | `sim_plant_reactive_power` | `1.3.4`，F32 ABCD | var→kVar，×0.001 |
| `DCEMS_PFR.isFreMode` | `primary_frequency_mode` | `1.1.0`，FC02 | bool，固定 false |

补充静态字段是因为 DCEMS `Value(T typeDef)` 只设置类型、不初始化当前值，精简点表会触发空指针；这些只读量不构成控制输入或控制成功证据。`kk_flag`、`emsActPowerEnable`、`emsReactPowerEnable`、`bFreCtrlEnableFlag` 均显式为 false，其中 `kk_flag=false` 用于避免进入不适用的 KK 厂商分支。

## 5. 真实启动与协议证据

### wind-hub

按 `run_windhub.sh --prepare/--check/--up` 使用正式 CLI 启动 Gateway 和 DG，并启动 `dcems-random-ten`。

- Gateway PID 273535，DG PID 273590，均真实运行。
- `127.0.0.1:15221..15230` 十个 listener 全部存在。
- DG 每批 `confirmed=93 failed=0 unknown=0`（10 台各 9 个量 + 首台 3 个站控/调频量）。
- 首轮独立 Modbus 动态检查：**11 passed**。

### DCEMS

Docker Desktop 的 `network_mode: host` 不向本 WSL 环境提供预期入口，因此 Overlay 改为 bridge：

- Web `18080:18080`；Server `18443:443`；
- Server→DB 使用 `dc-ems-db:5432`；
- Server→Gateway 使用 `host.docker.internal:15221..15230`；
- 从真实 Docker 容器到 Gateway 15221 的 TCP 连接实测成功；
- Web HTTP 200，Server `/api/` HTTPS 200；
- Server 日志含 10 条 `init datagetter ok` 和 `https listened on 443`。

证据：`dcems-workmode-startup.log`、`dcems-licensed-fresh-startup.log`、`final-listeners.txt`、`server-final.log`。

## 6. 十机真实采样与数值对账

测试不是比较寄存器 ACK，而是并发执行：

1. 从 10 个 Gateway 端口读取 FC04 F32 ABCD 风速；
2. 从首台读取站控 P/Q 和 FC02 模式；
3. 同时通过真实 `/api/ws` 发送订阅，覆盖完整 12 秒窗口；
4. 将 Gateway 样本按 DCEMS 的两位小数显示精度舍入，与同设备 DCEMS 键对账；
5. 同时核对 10 台 `emsWorkMode=32`。

实际匹配样例（每个值均来自同一次真实采样窗口，不是预置常量）：

| 设备 | Gateway 风速（m/s，舍入） | DCEMS 风速（m/s） | 结果 |
|---|---:|---:|---|
| DCEMS_WTG01 | 17.39 | 17.39 | PASS |
| DCEMS_WTG02 | 15.31 | 15.31 | PASS |
| DCEMS_WTG03 | 12.46 | 12.46 | PASS |
| DCEMS_WTG04 | 12.93 | 12.93 | PASS |
| DCEMS_WTG05 | 4.44 | 4.44 | PASS |
| DCEMS_WTG06 | 4.25 | 4.25 | PASS |
| DCEMS_WTG07 | 8.74 | 8.74 | PASS |
| DCEMS_WTG08 | 8.96 | 8.96 | PASS |
| DCEMS_WTG09 | 15.21 | 15.21 | PASS |
| DCEMS_WTG10 | 13.02 | 13.02 | PASS |

每台有 18 个协议样本，DCEMS 每台至少有两个动态值；逐设备匹配排除了串台。

| 站控量 | Gateway 原值 | ×0.001 后 | DCEMS | 结果 |
|---|---:|---:|---:|---|
| `powAct` | 955125.3125 W | 955.13 kW | 955.13 kW | PASS |
| `powReact` | 159758.21875 var | 159.76 kVar | 159.76 kVar | PASS |
| FC02 调频模式 | false | 不适用 | `DCEMS_PFR_isFreMode=false` | PASS（设备键） |

完整序列：`numeric-reconciliation.json`。测试日志：`numeric-reconciliation-fixed-window.log`。

## 7. Web 与 WebSocket 真实浏览器结果

### 已通过

- Chromium 真实打开登录页，使用运行时种子的 admin 账户登录成功，进入 `#/dashboard`。
- 首页风场平均风速、有功总和、理论功率、可用功率动态更新。
- 测试专用首页模板增加“升压站有功/无功”；浏览器实测：
  - 有功 `429.52 kW → 707.15 kW`；
  - 无功 `-241.43 kVar → -141.15 kVar`。
- 页面订阅 60 个单机键，实际收到 WTG01..10 各自不同的风速。
- WTG01 页面风速实测 `14.98 → 7.15 m/s`，证明至少该行持续刷新。
- AGC/AVC/PFR 控制页面可打开；只审查控件，没有点击控制确认或发送命令。

证据：`browser/browser-station-power.png`、`browser/browser-station-power.json`、`browser/single-view-subscriptions.json`、`browser/single-view-latest.json`。

### P0：十机 Web 表格渲染失败

AGC 单机视图列出全部 10 个设备 ID，但只有 WTG01 有数值，WTG02..10 只有单位，数值为空。真实浏览器断言失败：

```text
browser has no numeric wind speed for DCEMS_WTG02
```

抓帧排除了采集和订阅缺失：

- 页面最后一条订阅含 60 个键，包括 10 台 `emsWinSpd`；
- Server 实际返回 10 台不同的 `DCEMS_WTGxx_emsWinSpd`；
- 数值对账同时证明 10 台均与 Gateway 匹配。

生产源码根因（本机受维护源码 `/home/luosh/dc-ems/`）：

```text
web/src/views/components/fullFilePage.vue:410-417
```

当前逻辑：

```javascript
let deviceId = item.split("_")[0];
```

对 `DCEMS_WTG02_emsWinSpd` 得到 `DCEMS`，所有设备都映射到 `tableData[0]`。这正好解释仅首行刷新。

证据：`browser/browser-ten-turbines.png`、`browser/browser-ten-turbines.json`、`browser-ten-turbines-test.log`。

### P0：调频 false 没有进入页面显示

独立订阅结果：

- `DCEMS_PFR_isFreMode`：持续为字符串 `false`；
- `fp_emsSyzAct` / `fp_emsSyzReact`：持续动态变化；
- `fp_emsIsFreMod`：帧中键不存在，不是 false；
- 浏览器调频页“调频信号”实时值为空，断言失败。

相关生产链路：

- `emslib/.../YctpDevice.java:27-31`：`isFreMode.dataUpdate` 应更新 `FactoryPoint.emsIsFreMod`；
- `dc-ems/.../ConfigLoader.java:452-460`：FactoryPoint 通过 `dataChanged` 写入公共缓存；
- `dc-ems/.../core/web/websocket/WsPublisher.java:97-110`：只推送 `G.getter.refresh(key)` 非 null 值。

当前证据可把故障界定在 YctpDevice→FactoryPoint→公共缓存链，但不在没有更多证据时指定某一行为唯一根因。需在生产源码中加入 false 初始值和 false→true→false 的专项实证。

证据：`factory-values.json`、`browser/browser-pfr-mode.png`、`browser/browser-pfr-mode.txt`、`browser-station-pfr-test.log`。

## 8. 控制能力与 BLOCKED 边界

### 已有生产原语

- DCEMS `DataGetterModbusImpl.control` 存在按设备/变量查 locator 并调用 `ModbusMaster.setValue` 的能力。
- Gateway 支持 FC05/06/15/16 和可写地址校验。
- Gateway→Commander `SubmitCommand`、Commander FIFO/持久化、Gateway→DG 仿真 RPC、DG `APPLIED` 均有生产实现。

### 当前配置/生产链缺口

1. smoke 种子只含只读采集点，未保留 DCEMS `ctrlActPow/ctrlReActPow/ctrlStart/ctrlStop/ctrlReset` 等控制地址。
2. 10 个 Gateway listener 均为 `control.enabled=false`、`mappings=[]`。
3. Deployment 只有 Gateway 和 DG，没有 Commander。
4. DG 使用 `kind: random`，没有可写模型输入；不能将随机值变化当成命令执行。
5. `emsGridDispatchAct`、`emsGridDispatchReact`、`emsFreDst`、`emsDeltapValue` 没有已证实的设备协议输入生产者：
   - `emsGridDispatchAct` 是 AGC 有功目标；
   - `emsFreDst` 是一次调频目标有功功率（kW），不是 Hz；
   - `emsDeltapValue` 是一次调频增量功率（kW）。
6. AGC/AVC/PFR 总使能是内部安全配置，不与外部 `isFreMode` 混用；本任务保持全部关闭。
7. Gateway 的 Modbus 写 ACK 不代表 Commander/DG 已执行，未来闭环必须另查 Commander 终态及 DG `GetInputExecution=APPLIED`。

外部信号审查仍报告：`agc_grid_target`、`pfr_target_power`、`pfr_delta_power`、`grid_frequency` 未闭包。证据：`external-signals-audit.json`。

**没有发送 AGC/AVC/PFR 控制指令，没有启动 Commander，没有直接写数据库制造控制结果。**

## 9. 其他生产遗留问题

### P1：控制关闭时周期算法空指针

最终 Server 日志仍有 `FactoryPoints.checkIsLimit` 周期 NPE；一次最终快照记录 278 次。相关源码：

```text
emslib/.../FactoryPoints.java:1101-1109
```

`emsFacDispatchActEnabled`、`emsGridDispatchActEnabled`、`emsJkDispatchActEnabled` 等使用只设类型、不设当前值的 `Value` 构造，控制关闭时没有生产者为其赋值。异常被捕获后返回 false，主数据线程未退出，但不能把持续异常称为正常。

另外，静态审查发现 `EmsDispatcher.lambda$emsGridDispatchReactChanged$11` 将 `emsGridDispatchAct` 写入 `emsAvcFeedback`；该 AVC 反馈语义风险未做动态专项，不计为已关闭缺陷。

### P1：历史视图初始化失败

Server 启动日志存在 `data.d_fp`、`data.d_dcems_wtg10` 不存在的 SQL 错误；最终数据库中：

```text
data_tables=15
pldata_views=0
energy_report_view=0
```

`wtData/wfData` 历史查询页面没有数据行。实时采集与实时 WebSocket 不受此结论替代，历史报表/曲线不准出。

证据：`server-final.log`、`database-views.txt`。

### 已在测试配置内闭环的问题

- Docker Desktop host 网络入口不可达 → 改 bridge + 显式端口映射。
- 非隔离数据库风险 → 固定 `dcems_smoke`，拒绝其他 DB 和未核实卷复用。
- 旧设备授权不匹配 → 使用官方管理员工具对当前机器码授权并全新导入。
- JKS 口令不匹配 → 生成并校验符合发布契约的独立 JKS。
- 精简 DCEMS 点集触发 WTG 空指针 → 补齐真实只读量、通用 `kk_flag=false`。
- 数值对账偶发窗口不重叠 → WebSocket 观测器完整覆盖采样窗口；未降低匹配断言。

## 10. 修复文件与原因

| 文件组 | 修复内容 |
|---|---|
| `tests/third_party/dc_ems/test_live_smoke.py` | 修 F821；增加逐机数值/单位对账、真实浏览器十机/站控/PFR 断言 |
| `websocket_observer.py` | 支持指定键、增加工作模式、覆盖完整采样窗口、严格帧处理 |
| `prepare_seed.py` / `validate_gateway_mapping.py` | 支持并校验 `coe`；固定安全控制标志和通用 `kk_flag` |
| `auxiliary_seed.py` | 派生站控/调频逻辑设备及首页 P/Q 显示模板 |
| `tests/third_party/dc-ems/config/` | 补齐 BusinessPoint、地址、字序、单位、随机/常量规则及设备映射 |
| `compose.web.yaml` / `nginx.smoke.conf` | Docker Desktop bridge、端口映射、服务名反向代理、宿主机 Gateway 地址 |
| `prepare_runtime.py` | JDBC 指向独立 `dc-ems-db:5432/dcems_smoke` |
| 其他测试 Python | Ruff 导入/格式、安全 SIM300 改写、mypy strict 类型修复 |
| `README.txt` | 更新最终点表、隔离资源、bridge、数值与浏览器执行边界 |

修复提交记录以当前开发分支 Git 历史为准；最终交付消息给出精确 HEAD。运行证据和敏感运行配置均未提交。

## 11. 最终测试统计

最终完整实时命令：

```bash
DCEMS_LIVE=1 python -m pytest -q tests/third_party/dc_ems/test_live_smoke.py
```

实际还注入了 `PYTHONPATH`、证据目录、浏览器共享库路径及当前 Shell 的登录口令。最终结果：

```text
19 tests = 17 passed + 2 failed
退出码 1
耗时 80.77s
```

失败项仅为：

1. `test_browser_ten_turbines_are_numeric_and_refresh`；
2. `test_browser_primary_frequency_mode_matches_false_input`。

通过项包括：10 台 Modbus 动态测试、站控 P/Q/FC02、Web/API、WebSocket 真实订阅、完整数值对账、真实浏览器站控 P/Q 刷新。

最终日志：`final-live-suite-fixed-window.log`。

测试结束后已通过正式 CLI 停止 DG/Gateway，并通过 Compose `down`（不带 `-v`）删除本任务容器与网络；隔离数据库卷 `dcems-smoke_database` 保留用于复核。Playwright 浏览器均关闭，本任务专用 Windows Chrome 实例也已关闭。证据：`cleanup.log`。

| 层级 | 结果 |
|---|---|
| 静态门禁 | PASS |
| 配置加载闭包 | PASS |
| 10 台随机数据采集与数值对账 | PASS |
| 站控 P/Q 换算与 Web 刷新 | PASS |
| WebSocket 实际动态订阅 | PASS |
| 十机 Web 全量显示 | **FAIL / P0** |
| 调频 false 的 FactoryPoint 与 Web 显示 | **FAIL / P0** |
| AGC/AVC/PFR 控制闭环 | **BLOCKED；未执行控制写入** |
| 历史视图/报表 | **FAIL / P1** |
| 总体 | **BLOCKED** |

## 12. 交给生产代码维护方的最小修复建议

1. **Web 十机渲染**：在 `fullFilePage.vue` 不用首个下划线拆设备 ID；使用最后一个下划线分隔 `varPath`，或直接用当前行 `deviceId` 建立键。新增含多下划线设备 ID 的 10 行真实 Web 测试，逐行断言并验证刷新。
2. **调频 false 推送**：检查 YctpDevice 初始化事件是否实际注册，以及 `FactoryPoint.emsIsFreMod` 到 `MemorySet.cedis` 的 false 初始更新；不要用真值判断过滤 false。覆盖初始 false、false→true、true→false 和失联质量位。
3. **算法空值**：`Value` 的单参数构造与当前值初始化语义需统一，或在 `checkIsLimit` 等路径显式定义安全默认值；控制关闭也必须是可运行状态。补零输入和控制关闭回归测试。
4. **历史视图**：修正建表/建视图顺序，启动后回读必需视图；失败应进入明确就绪状态，而不是只记录 ERROR 后继续假就绪。
5. **外部控制目标**：在受维护 Server 源码增加明确逻辑设备/协议适配，将真实 Modbus 输入分别写入 `emsGridDispatchAct`、`emsGridDispatchReact`、`emsFreDst`、按需 `emsDeltapValue`，同时定义单位、质量位、时间戳、失联和失效策略。
6. **控制闭环**：另建可写 smoke 版本，配置 holding/coil、Gateway control mappings、Commander 与具备输入语义的 DG 物理模型；先覆盖 DG 已支持的有功/无功/限值/模式。WTG start/stop/reset 当前 DG 输入白名单不支持，须另行实现，不能凭字段名直接接线。
7. **AVC 反馈复核**：用不同 AGC/AVC 目标值专项验证 `emsAvcFeedback` 是否误读有功目标；当前只作为静态风险移交。

在上述 P0 项由生产维护方修复并重跑本报告实时套件前，最终结论保持 **BLOCKED**。
