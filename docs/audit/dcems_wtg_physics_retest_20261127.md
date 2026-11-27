# DCEMS 风机物理模型整改独立复测报告（2026-11-27）

## 0. 模板与范围

- 仓库内未发现《报告模板.md》，本报告未声称符合模板。
- 范围：P1 风机物理模型与 DCEMS mode 初始化、P2 Gateway float32 溢出，以及 T01/T03/T04/T05 集中复测。
- T02 真实 DCEMS Server 仍为环境阻断，不因 T01/T03 通过而宣称 Server 联测通过。

## 1. Git 基线

| 项目 | 值 |
|---|---|
| 分支 | `feat/dcems-gateway-smoke-20261126` |
| 整改前 HEAD | `311d979578bf2568c701d17fafb926ad9f2cc087` |
| 整改后起始 HEAD | `fb3ff3cce7eae5172d68f5c43795dee242ee8378`（本地/远端一致） |
| 新增提交 | 7 个：`4bdc71b0` `9032d74a` `e67a2800` `f31348d3` `a8b58367` `ae8812f7` `fb3ff3cc` |
| 复测测试修改与证据提交 | 见第 10 节 |
| 报告提交 | 本文件提交 |

未创建 PR、未合并 main、未修改 `third_party/dc-ems/`、未修改 `src/` 生产代码。

## 2. 静态门禁与回归

| 项目 | 结果 |
|---|---|
| `ruff check src/data_generator src/industrial_gateway tests/third_party/dc_ems tests/src/unit/test_physical_models` | All checks passed |
| `ruff format --check` | 132 files already formatted |
| `mypy src/data_generator src/industrial_gateway` | Success: 84 source files |
| `import-linter` | 22 contracts kept, 0 broken |
| 物理模型单测 `tests/src/unit/test_physical_models/` | **118 passed** |
| 契约/种子/隔离配置 | **15 passed** |
| Gateway 回归 + PPC 兼容 | **59 passed** |
| DG 权限与输入执行 | **10 passed** |
| T01 真实控制链 | **3 passed** |
| T03 物理响应 | **1 passed**（79.48 s） |

测试侧修复：

1. `tests/src/unit/test_physical_models/conftest.py` 仍指向已删除的 `data-generator-sim.example.yaml` 与 v1.0.0 模板，导致 92 个既有测试报错；已改用 `deploy/config_roots/sim-a` 当前版本化 Service/Scenario/模板。`test_point_table_contract.py` 同步更新。
2. `test_turbine.py` 零功率顺桨观察窗由 10 s 延长到 30 s。物理依据：全行程 1.57 rad / 0.12 rad/s ≈ 13.1 s，再加转矩一阶衰减与近零尾段；最终功率断言仍为 `<1 W`，未降低阈值。
3. T03 用稳态判据替代固定 5 s/10 s 尾均值，新增升降/零/额定/mode 语义与逐点物理一致性断言。

## 3. P1 风机物理模型审查

### 3.1 Cp 边界

`turbine.py:113-141`：

- 区间内分段线性插值；低于首点按首段斜率外推，且 `slope=max(0.0, slope)`，不会向停机方向虚构增长。
- 高于末点按末段斜率外推，且 `slope=min(0.0, slope)`，高叶尖速比尾部不增长。
- 结果经 `clamp(cp*pitch_factor, 0, 16/27)`，连续、非负、不超过 Betz。
- 残余 P2：λ<4 尾部取常值而非衰减到 0；在本轮 9.1≤λ≤9.65 的运行区间无影响，但低转速启动物理近似仍可改进。

### 3.2 限功率转矩与变桨

- 限功率时电磁转矩按 `available` 跟踪当前气动捕获，先通过变桨降低捕获，再稳定到需求；避免转子越限。
- 变桨目标由线性 Cp 反演需求气动捕获；零功率目标直接顺桨；需求提高时可重新开桨。
- 保留变桨速率 0.12 rad/s、变桨一阶时间常数 1 s、转矩时间常数 0.5 s、机械惯量与超速/切出保护。
- 实测所有工况转子 1.51～1.58 rad/s、λ 9.10～9.65、fault=0，无超速或功率突变。

### 3.3 mode 语义

- mode 0：最优功率跟踪，忽略 `active_power_setpoint`；实测设定 150 kW 时仍稳定输出 2 000 000 W。
- mode 1：按 `active_power_setpoint` 恒功率；150/650/0/2000 kW 均稳定跟踪。
- mode 2：按 `power_limit_setpoint`；650 kW 限值下稳定 651 243 W。

## 4. P1 DCEMS 控制模式初始化

`run_wtg_control_chain.sh --up` 在 `run_control_chain.sh --up` 之后：

- 通过正式 `cli.main data-generator variables update` 设置隔离 Generation 的 `WTG_SIM01.control_mode=1`。
- 实测返回 `SUCCESS`、`effective_step=10`；服务重启后再次执行仍成功。
- 仅作用于 `DCEMS_CONTROL_ROOT` 指向的隔离 Generation；未修改 Scenario 文件。
- 隔离配置测试确认：PPC 参与集合已移除 `WTG_SIM01`，`PV_SIM01`/`BESS_SIM01` 仍参与且权重保持 1.0。
- `set -euo pipefail` 下 CLI 失败会中止 `--up`，不会继续假定风机已进入恒功率模式。
- T03 启动时显式断言 `control_mode==1`，初始化缺失会直接失败。

## 5. P2 Gateway float32 溢出

`runtime.py` 在缩放后、转发前对 `float32` 目标执行 `struct.pack(">f")`：

| 检查 | 实测 |
|---|---|
| FC16 写入 float32 最大值（×1000 后越界） | Modbus 写入 ACK；源寄存器回读 `[65535, 32639]`（0x7F7FFFFF CDAB） |
| Gateway 事件 | `scaled control exceeds target float32 range`（`CONTROL_SCALE_OUT_OF_RANGE`） |
| Commander | 无转发（`forward_events=[]`）；`GetRequestResult=NOT_FOUND` |
| DG | `UNKNOWN`、`effective_step=0`，无执行 |
| 原始寄存器 | 保留原始 wire 值，可追溯 |
| 合法值回归 | 随后 450 kW 写入 `SUCCEEDED/CONFIRMED_SUCCESS`，DG step=2473 |
| 历史默认倍率 | e2e-final 两个既有映射经正式 Loader 均为 1.0 |

## 6. T01 — 单位转换：PASS

| 点 | 源寄存器/解码 | Gateway2 SI 寄存器/解码 | Commander | DG |
|---|---|---|---|---|
| `ctrlActPow` 41557 | `[0,17377]` → 450.0 kW | `[47616,18651]` → 450000 W | SUCCEEDED / CONFIRMED_SUCCESS | APPLIED / step 140 |
| `ctrlReActPow` 41555 | `[0,17146]` → 125.0 kvar | `[9216,18420]` → 125000 var | SUCCEEDED / CONFIRMED_SUCCESS | APPLIED / step 160 |

无双重转换；Gateway1 `value_scale=1000`、Gateway2 默认 1.0；原始寄存器值不变。

## 7. T03 — 物理响应：PASS

工况固定：风速均值 10.0 m/s、湍流 0、空气密度 1.225、初始 rotor=额定 1.5 rad/s、`control_mode=1`。

稳态判据（事先定义）：采样 0.5 s，连续 4 s（8 点）处于目标 ±(1% 或 2 kW)；零目标 ±1 kW；最长 30 s。物理依据：全行程变桨 ≈13.1 s，转矩时间常数 0.5 s，留出转速稳定余量。

| 工况 | 稳定时间 | 稳态均值 | 稳态误差 | 稳态范围 | 末端桨距 | 转子 | λ | Cp |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 150 kW 基线 | 10.66 s | 150 370.8 W | +370.8 W | 150 036–151 208 | 84.21° | 1.527 | 9.163 | 0.0235 |
| 150→650 kW | 8.63 s | 648 216.8 W | −1 783.2 W | 644 179–649 851 | 64.85° | 1.542 | 9.254 | 0.1018 |
| 650→150 kW | 10.14 s | 150 385.5 W | +385.5 W | 150 037–151 251 | 84.21° | 1.524 | 9.144 | 0.0235 |
| 0 kW | 8.62 s | 238.8 W | +238.8 W | 21–783 | 89.95° | 1.518 | 9.105 | 0.0000 |
| 2000 kW 额定 | 16.22 s | 1 994 581.7 W | −5 418.3 W | 1 982 666–1 999 489 | 11.18° | 1.580 | 9.481 | 0.3133 |
| mode 0（设定 150 kW） | 3.55 s | 2 000 000 W | 0 | 2 000 000–2 000 000 | 5.57° | 1.609 | 9.652 | 0.3305 |
| mode 2（限值 650 kW） | 15.21 s | 651 242.6 W | +1 242.6 W | 650 131–653 948 | 64.48° | 1.570 | 9.421 | 0.1018 |

逐点物理一致性（全部工况、全部采样）：

- `active_power ≈ η × torque × gear × rotor`（1% 或 200 W 容差）。
- `tip_speed_ratio ≈ rotor × radius / wind`（1% 容差）。
- `aerodynamic_power ≈ 0.5 ρ π R² Cp v³`（2% 或 1 kW 容差）。
- `fault_status=0`、`running_status=1`、`0 ≤ pitch ≤ 90°`、`0 ≤ rotor ≤ 2.0`、`torque ≥ 0`、`power ≤ rated`。
- 150→650 上升 >400 kW；650→150 回落；零功率与额定边界均通过；无超速、无非物理突变。

## 8. T04 — 安全与异常

| 检查 | 结果 |
|---|---|
| Gateway 入口 float32 溢出拒绝 | PASS（见第 5 节） |
| 无 Commander 命令 / 无 DG 执行 | PASS（NOT_FOUND / UNKNOWN step 0） |
| 原始寄存器与错误事件可追溯 | PASS |
| 合法浮点与默认倍率 | PASS |
| 幂等/失联/UNKNOWN/状态保留 | 沿用上一轮已通过证据；本轮未见回归 |

## 9. T05 — 回归与清理：PASS

| 检查 | 结果 |
|---|---|
| 物理模型单测 | 118 passed |
| Gateway/PPC/契约/种子/DG 回归 | 15 + 59 + 10 passed |
| CLI 停机 | PASS：端口 `15531/15421/52152/52161/52173/52174` 全部释放，无服务进程 |
| CLI 重启 | PASS：四服务 RUNNING；Commander HEALTHY；mode=1 重新初始化 step=10 |
| 状态恢复 | 命令 `3a7a5f27…` 重启后仍 `SUCCEEDED/CONFIRMED_SUCCESS` |
| 默认倍率 | e2e-final 映射 1.0 |
| 资源清理 | 临时运行根、NATS 容器、令牌文件已删除；其他运行目录未触碰 |

## 10. 修改文件与提交

| 文件 | 说明 |
|---|---|
| `tests/src/unit/test_physical_models/conftest.py` | 更新到 sim-a 版本化配置与模板 |
| `tests/src/unit/test_physical_models/test_point_table_contract.py` | 同步 Service 路径 |
| `tests/src/unit/test_physical_models/test_turbine.py` | 零功率顺桨观察窗 30 s（断言不变） |
| `tests/third_party/dc_ems/test_wtg_physical_response_live.py` | 稳态判据、升降/零/额定/mode 0/2、逐点物理一致性 |
| `tests/third_party/dc_ems/test_wtg_control_chain_config.py` | 强化 PV/BESS PPC 控制权断言 |
| `docs/audit/dcems_wtg_physics_retest_20261127_evidence/` | 证据目录（无明文令牌/许可） |

提交：

- 起始 HEAD `fb3ff3cc`；测试修改与证据提交、报告提交 SHA 见交付摘要。

## 11. 问题清单

- P0：无。
- P1：无（上一轮 P1 已整改并通过实测）。
- P2-1：λ<4 的 Cp 低叶尖速比尾段为常值而非衰减到 0；连续、非负且不影响本轮运行区间，但启动/停机物理近似可进一步改进。
- P2-2：零功率稳态残余 238.8 W（<1 kW 判据内），来自 Cp 近零尾段，不是控制链误差。
- 测试基础设施：物理单测 fixture 长期引用已删除文件，本轮修复；非生产缺陷。

## 12. 未测试范围

1. 真实 DCEMS Server/Web/TimescaleDB（镜像、授权、runtime、JKS/db.property、DB schema 缺失）。
2. DCEMS Server 发出的真实 FC16 与 `comOk`。
3. 厂站级 AGC/AVC/PFR 自动算法。
4. 真实风机/PLC 硬件与长期稳定性。
5. 无功功率物理动态响应（本轮只验证 Gateway/DG 链路）。

## 13. 最终裁决

**`WTG_PHYSICS_PASS`**

- 150→650→150 kW 升降、零功率、额定上限、mode 0/1/2 均满足事先定义的稳态判据。
- 无超速、无故障、无非物理功率突变；电功率、气动功率、叶尖速比、转矩逐点物理一致。

**`WINDHUB_CONTROL_PASS`**

- T01 单位转换、T04 Gateway 溢出拒绝、T05 回归/启停/状态恢复通过。
- 控制模式初始化经正式 CLI 生效且仅作用于隔离 Generation。

**`DCEMS_SERVER_BLOCKED`**

- 本机缺少发布镜像、授权、JKS/db.property、隔离 DB schema；T02 未执行。
- 不得据此宣称真实 DCEMS Server 控制闭环通过。

后续：补齐 DCEMS 发布运行环境后执行 T02；若通过，可进入 DCEMS AGC/AVC/PFR 自动算法联测准备。
