# DCEMS 真实风机控制链路独立验收报告（2026-11-27）

## 0. 模板与范围声明

- 仓库内未发现《报告模板.md》，本报告未声称符合任何模板。
- 范围：DCEMS Server/Web → Modbus TCP FC16 → Gateway1 → Commander → Gateway2 → Data Generator → WTG 物理响应。
- 本报告只验证风机设备级控制出口；不代表厂站级 AGC/AVC/PFR 自动算法通过。

## 1. Git 基线

| 项目 | 值 |
|---|---|
| 分支 | `feat/dcems-gateway-smoke-20261126` |
| 起始 HEAD | `94cac57d017c3fb668dcba88d417f38bc49a28fb` |
| 远端 HEAD（fetch 后） | `94cac57d017c3fb668dcba88d417f38bc49a28fb`（与本地一致） |
| 距上一验收 HEAD `00fc6a46` | 29 个提交（任务描述为 26；实际分支包含 29 个） |
| 测试修复与证据提交 | `8a50ed4e` |
| 报告提交（最终功能性 HEAD） | 本文件提交 |

未创建分支、未创建 PR、未合并 main、未 force push / rebase / reset。

## 2. 实际环境与版本

| 项目 | 值 |
|---|---|
| 平台 | Linux 6.18.33.2-microsoft-standard-WSL2 |
| Python | 3.14.7（`~/miniconda3/envs/wind-hub`） |
| Docker | 29.8.0 |
| grpcio / protobuf / pymodbus / nats-py | 1.84.0 / 6.33.6 / 3.15.0 / 2.16.0 |
| NATS | nats-server 2.10.29（JetStream，`127.0.0.1:14223`） |
| dc-ems 发布标识 | `release_version=1.1.4-583d5da` |
| 预期 DCEMS 镜像 | `dc-ems-server:1.1.4-583d5da`、`dc-ems-web:1.1.4-583d5da`、`dc-ems-timescaledb:2.7.0-pg14` |
| 实际 DCEMS 镜像 | **本机均缺失**（`docker image inspect` 返回 rc=1） |

隔离根：

- `run/dcems-control-wtg`、`run/dcems-control-wtg2`、`run/dcems-control-wtg3`、`run/dcems-control-wtg4` 均为本专项临时根，验收后已删除。
- 未触碰 `run/dcems-measurement/`、`run/dcems-smoke/`、`run/dcems-modbus-cli*`、`run/gap8/`。
- 九个随机 Bearer Token 仅通过临时 Shell 文件注入；文件已删除，未提交、未打印。
- 未提交任何明文令牌、DB 密码、许可证或真实用户凭据。

## 3. 静态审查与门禁

### 3.1 结果

```text
ruff check src/industrial_gateway tests/third_party/dc_ems   All checks passed
ruff format --check …                                        74 files already formatted
mypy src/industrial_gateway                                  Success: 37 source files
import-linter                                                22 kept, 0 broken
```

| 测试集 | 结果 |
|---|---|
| 契约/种子/隔离配置（3 文件） | 15 passed |
| Gateway 回归 + 上一阶段 PPC 配置 | 59 passed |
| DG 权限与输入执行 | 10 passed |
| T01 真实控制链 | 3 passed |
| T03 物理响应 | **1 failed** |
| T02 真实 DCEMS Server | NOT_RUN |

### 3.2 正式 Loader

- Gateway：`load_gateway` 解析 Gateway1（2 servers）与 Gateway2（5 servers）成功。
- Commander：`load_service_settings` 解析 `PPC_SIM01`、`WTG_SIM01` simulation targets 成功。
- DG：`load_settings` 解析 `e2e-plant-physical`、WTG 新 DeviceModel/PointTable 成功。
- Deployment 版本闭包保持 `dcems-control@1.0.0`；原 `e2e-final` 配置未被修改。

### 3.3 静态发现并修复的测试问题

| 编号 | 文件 | 问题 | 处理 |
|---|---|---|---|
| F-01 | `test_wtg_control_chain_live.py` | `_read_power` 把 Gateway2 的 CDAB 寄存器按 ABCD 解码，得到 1e22～1e36 伪值，导致 T03 假通过 | 改为 CDAB 解码并复测；T03 由假 PASS 变为真实 FAIL |
| F-02 | 8 个专项测试/生成器文件 | ruff SIM/I001 与格式不合规 | 格式化与等价简化，未改变断言 |
| F-03 | `run_wtg_control_chain.sh` | `--down` 只认 `DCEMS_WTG_CONTROL_ROOT`，误用 `DCEMS_CONTROL_ROOT` 时会指向默认根 | 增加 `DCEMS_CONTROL_ROOT` 回退 |
| F-04 | T01/T03 证据 | 未落 `effective_step` 与原始寄存器 | 增加证据字段并重跑 |

未删除断言、未降低 T03 门槛、未绕过权限检查。

## 4. T01 — Gateway 单位转换：PASS

原始协议证据（两寄存器 F32/CDAB、slave=1、FC16）：

| 点 | 寄存器 | 源寄存器（CDAB 顺序） | 源解码 | Gateway2 SI 寄存器 | SI 解码 | DG 状态 / step |
|---|---:|---|---:|---|---:|---|
| `ctrlActPow` | 41557 | `[0, 17377]` | 450.0 kW | `[47616, 18651]` | 450000.0 W | APPLIED / 107 |
| `ctrlReActPow` | 41555 | `[0, 17146]` | 125.0 kvar | `[9216, 18420]` | 125000.0 var | APPLIED / 125 |

链路证据：

- Gateway1 源 Holding 回读与写入寄存器完全一致，原始 wire 值未被倍率改写。
- Commander 实际 `requested_value` 为 `450000.0` / `125000.0`，`SUCCEEDED` / `CONFIRMED_SUCCESS`，`io_settled=true`。
- `request_id → command_id` 一致；DG `GetInputExecution` 返回 `APPLIED`、`effective_step > 0`。
- Gateway1 映射 `value_scale=1000`；Gateway2 执行映射默认 `1.0`，不存在双重转换。
- 旧版 `e2e-final` 未设置 `value_scale` 的映射经正式 Loader 解析为 `1.0`。

结论：kW/kvar → W/var 的转换在 **测试 Modbus 客户端 → Gateway1 → Commander → DG → Gateway2** 路径得到真实执行证据。该结论尚不包含真实 DCEMS Server 发令路径（见 T02）。

## 5. T02 — 真实 DCEMS Server：NOT_RUN（环境阻断）

未执行，原因按顺序：

1. 本机无发布 runtime：`run_wtg_dcems_server.sh --check` 报 `prepare isolated runtime first`；`/home/luo/dc-ems` 无 `release/`、无 `db.property`、无 `dc-ems.jks`。
2. 三个发布镜像全部缺失：`dc-ems-server:1.1.4-583d5da`、`dc-ems-web:1.1.4-583d5da`、`dc-ems-timescaledb:2.7.0-pg14`。
3. 无合法 `DCEMS_LICENSE_CODE`；隔离种子 `license` 为空。
4. 无隔离数据库卷与已确认的业务 schema。

因此 **真实 DCEMS Server 未实际发出 FC16**；不得以 Web 200 或种子配置替代。

已完成的可执行检查：

| 检查 | 结果 |
|---|---|
| 隔离种子设备身份 | `WTG01`，单设备，capacity 2000 kW |
| 隔离通道 | `host.docker.internal:15531`，MODBUSTCP，500 ms |
| 控制点 | `ctrlReActPow 1.4.41555`、`ctrlActPow 1.4.41557`，F、highBit=1、isCtrl=true |
| 自动算法开关 | `emsActPowerEnable=false`、`emsReactPowerEnable=false`、`bFreCtrlEnableFlag=false` |
| Web 路由 | `POST /api/config/setParam`，`{"param":{"WTG01_ctrlActPow":450}}` 与源码一致 |
| JSON/响应 | `Configure.setParam` 返回 `text/json` 与 `ApiResult.ok("控制成功")` |
| 返回值语义 | 源码确认 Web **忽略** `getter.control()` 返回值；测试必须依赖 Gateway1 事件，原测试设计正确 |
| Modbus 写路径 | `DataGetterModbusImpl.control()` 使用已绑定 `isCtrl` 点且要求 `comOk`；`comOk` 由真实读取成功后置位 |
| Docker→宿主网络 | **PASS**：容器内 `host.docker.internal` 访问 `15531/15431` rc=0，未开放端口 `15999` rc=1；Gateway 仍仅绑定 127.0.0.1，未暴露到生产网卡 |

## 6. T03 — 物理动态响应：FAIL（P1）

最终复跑（修正 CDAB 解码后）：

| 指标 | 值 |
|---|---|
| 初始本地设定 | mode=1，150000 W |
| DCEMS 工业协议目标 | 650 kW（归一化 650000 W） |
| 基线尾均值 | **8822.65 W**（峰值 923908.7 W 后坍缩） |
| 响应尾均值 | **0.0027 W**（峰值 56230.4 W） |
| 变化 | 指令后功率**下降**，不是上升 |
| Commander / DG | `SUCCEEDED/CONFIRMED_SUCCESS`，DG `APPLIED`，step=282 |
| 模型终态 | rotor=1.8349 rad/s，λ≈11.01，`aerodynamic_power=0`，`available_power=0`，pitch=11.52° |

干净 Generation 分层探针：

| 工况 | 尾均值/峰值 |
|---|---|
| mode 0（最优） | 稳定 2 000 000 W |
| mode 1，150 kW | 尾均值 0.45 W；峰值 1 523 508 W 后坍缩 |
| mode 1，650 kW | 尾均值 19 335 W；峰值 62 022 W |
| mode 1，1.9 MW | 尾均值 66 632 W；rotor=1.837 |
| mode 2，650 kW 限值 | 尾均值 80.7 W；rotor=1.835 |

根因（生产代码与模型参数，本专项不修改）：

1. `src/data_generator/application/model/turbine.py:113-126`：`_cp()` 在 λ 超出 Cp 曲线区间时直接返回 0。曲线最大 λ=11（`device_model_wtg_sim_physical_v1.2.0.yaml:31-34`）；风速 10 m/s 时 λ>11 仅需 rotor>1.8333 rad/s。
2. `turbine.py:155-172`：mode 1/2 下发电机转矩按需求限功率，需求低于可用功率时气动转矩显著大于电磁转矩，转子持续加速；`turbine.py:188-192` 的 pitch 增益不足以泄放功率。
3. 转子越过 λ=11 后 `cp=0`，`available_power` 与电功率坍缩为 0，形成非物理的硬截止。
4. `turbine.py:161`：mode 0 完全忽略 `active_power_setpoint`；而 DCEMS 隔离种子没有 `control_mode` 控制点，DG local 默认 mode 0。因此即使真实 DCEMS Server 发令，`ctrlActPow` 在默认模式下也不会改变物理功率。

结论：**物理响应失败，不是控制链延迟或测试误差**。写入值已 APPLIED，但模型实际电功率不响应且反向坍缩。按任务要求不得判为 PASS。

## 7. T04 — 安全、幂等与异常

| 检查 | 结果 | 证据 |
|---|---|---|
| 非法倍率（0/负/Inf/NaN） | PASS | `TargetMapping` pydantic 拒绝 |
| 非浮点控制点设倍率 | PASS | 正式 Loader `scaled control mappings require floating-point points` |
| 原始寄存器不被转换 | PASS | 源 [0,17377] 解码 450.0；Commander/DG 450000 |
| 双重转换 | PASS | Gateway2 映射默认 1.0 |
| 转换溢出 | PASS | FC16 最大 F32 ×1000 → `forward REJECTED`，Commander `NOT_FOUND`，无命令、无执行 |
| 错误 Modbus 地址 | PASS | 41556/41558/41559 均返回 exception code 2，无写入事件 |
| 重复提交（同 request_id 同载荷） | PASS | 复用同一 command_id |
| 同 request_id 不同值 | PASS | `ALREADY_EXISTS` |
| Commander 失联 | PASS | Gateway1 `forward REJECTED`；恢复后 `NOT_FOUND`；DG 无 APPLIED |
| Gateway2 失联 | PASS | Commander `UNKNOWN/CONNECT_TIMEOUT`，`io_settled=true`；DG `UNKNOWN/step=0`；恢复后终态不变、命令数仍为 1 |
| DG 失联 | PASS | Commander `UNKNOWN/WRITE_UNKNOWN`；恢复后 DG `UNKNOWN/step=0`，未自动重放 |
| 连接超时 | PASS（Gateway2 失联路径得到 `CONNECT_TIMEOUT`） | 无伪报成功 |
| UNKNOWN 后新写入 | PASS（fail-closed） | Commander 重启 `DEGRADED`，Gateway1 新写入 `forward REJECTED` |
| 重启后状态追溯 | PASS | UNKNOWN 命令与成功命令均保留 |
| DCEMS 控制调用失败 | **NOT_RUN** | 真实 Server 未运行 |
| DCEMS 越界设定值（live） | **NOT_RUN** | 静态源码限 0..capacity(2000)；未在真实 Server 执行 |

## 8. T05 — 回归与环境清理：PASS

| 检查 | 结果 |
|---|---|
| Gateway/Commander/DG 单测与配置回归 | 59 passed |
| 契约/种子/隔离配置 | 15 passed |
| DG 权限与输入执行 | 10 passed |
| 旧配置默认倍率 1.0 | PASS（`PPC_SIM01` 两个映射均为 1.0） |
| CLI 停机 | PASS：服务 `STOPPED`，`15531/15421/52152/52161/52173/52174` 全部释放 |
| 僵尸进程 | 未发现 |
| 重启 | PASS：服务 RUNNING；Commander 因持久 UNKNOWN 为 `DEGRADED`（设计内 fail-closed） |
| 状态保留 | 成功命令 `SUCCEEDED/CONFIRMED_SUCCESS`；UNKNOWN 命令保留 |
| 资源清理 | 所有 `run/dcems-control-wtg*` 根、NATS 容器、临时令牌文件已删除；`14223` 释放 |
| 其他测试链路 | 未触碰十机测量及既有运行目录 |

## 9. 问题清单

### P0

无。

### P1

**P1-1 风机物理模型无法执行限功率需求，且默认模式忽略 DCEMS 有功设定值。**

- 复现：T03 `DCEMS_WTG_PHYSICS_LIVE=1`，mode 1、150 kW 后 650 kW → 功率由 8.8 kW 尾均值降至 0.0027 W。
- 源码：`src/data_generator/application/model/turbine.py:113-126, 155-172, 188-192, 161`；模型参数 `deploy/config_roots/e2e-final/templates/device_models/device_model_wtg_sim_physical_v1.2.0.yaml:18,25,31-34,43-44`。
- 期望：限功率模式应通过 pitch/转矩将电功率稳定在需求附近，并在需求低于可用功率时持续输出该需求。
- 实际：转子越过 λ=11 后 Cp 被硬置 0，available/aerodynamic/active power 全部坍缩；mode 0 又完全忽略 setpoint。
- 影响：DCEMS `ctrlActPow` 无法形成有效的物理功率控制；T03 物理响应门禁失败。
- 建议：修正 Cp 曲线外推/限幅与 pitch 控制能力，或在模型中明确支持限功率稳态；同时为 DCEMS 种子增加受控的 `control_mode`（或让 mode 0 合法响应需求）。需 ChatGPT 修复生产模型，Claude 未修改 `src/`。

### P2

1. `runtime.py` 只检查缩放后的 Python double 是否有限；float32 目标溢出由 Commander/DG 下游拒绝（实测无执行），未在 Gateway 边界提前拒绝。行为安全，但错误定位较晚。
2. `run_wtg_control_chain.sh` 原先 `--down` 仅识别 `DCEMS_WTG_CONTROL_ROOT`；已加 `DCEMS_CONTROL_ROOT` 回退。
3. T03 测试解码字序错误曾造成假 PASS；已修复，属测试代码缺陷。

### 环境阻断

- 真实 DCEMS Server/Web/TimescaleDB 镜像缺失；无发布 runtime、JKS、`db.property`、license 与业务 schema。T02 无法执行。

## 10. 修改文件与提交

| 文件 | 说明 |
|---|---|
| `tests/third_party/dc_ems/test_wtg_control_chain_live.py` | 修正 CDAB 输出解码；记录原始寄存器/effective_step |
| `tests/third_party/dc_ems/test_wtg_physical_response_live.py` | 记录 effective_step |
| `tests/third_party/dc_ems/prepare_wtg_control_chain.py` 等 6 文件 | ruff/format 等价整理 |
| `tests/third_party/dc_ems/run_wtg_control_chain.sh` | `DCEMS_CONTROL_ROOT` 回退 |
| `docs/audit/dcems_wtg_control_independent_retest_20261127_evidence/` | 证据目录（无明文令牌/许可） |

提交：

- 起始：`94cac57d`
- 测试修复与证据：`8a50ed4e`
- 报告：本文件提交

## 11. 未测试范围

1. 真实 DCEMS Server/Web 发令（FC16、comOk、认证、数据库）。
2. T03 物理响应未通过；修复后需重新验收。
3. DCEMS 无功设定值的物理响应（仅验证 Gateway/DG 链路 APPLIED）。
4. DCEMS 越界设定值的 live 行为与 Web 失败文案。
5. 长期稳定性、并发、自动 AGC/AVC/PFR 算法。
6. 真实风机/PLC 硬件。

## 12. 最终裁决

**`BLOCKED`**

依据：

- P1：风机物理模型在限功率模式下功率坍缩，DCEMS 有功设定值无法形成有效物理响应；T03 FAIL。
- T02 真实 DCEMS Server 因镜像、授权、JKS/db.property 与 schema 缺失无法执行；真实 Server 未发出 FC16。
- T01 单位转换链路 PASS（测试 Modbus 客户端路径），但不足以证明 DCEMS Server 物理控制闭环。
- T04 控制安全、幂等与失联 fail-closed 行为 PASS（除需真实 Server 的子项）。
- T05 回归与资源清理 PASS；无 P0。

恢复 READY_FOR_NEXT_STAGE 的前置条件：

1. 提供发布 DCEMS 镜像、合法授权、JKS/db.property 与隔离 DB schema，执行 T02。
2. 修复 WTG 限功率物理模型（并明确 `control_mode` 的 DCEMS 控制语义），使 T03 在固定风况下稳定响应。
3. 复跑 T01/T02/T03/T04/T05。
