# DCEMS T02 真实 Server 控制链独立验收报告（2026-11-27）

## 0. 模板与范围

- 仓库未发现《报告模板.md》，本报告未声称符合模板。
- 本轮完成发布镜像导入、合法授权生成、隔离 DB/Server/Web 启动、T02 真实控制、断连恢复与资源清理。
- 范围仅限 WTG 设备级控制；不代表厂站级 AGC/AVC/PFR 自动算法通过。

## 1. Git 基线

| 项目 | 值 |
|---|---|
| 分支 | `feat/dcems-gateway-smoke-20261126` |
| 任务基线 HEAD | `ed8be88bfd1f71e36e10ab80c0211c5f878883d0` |
| 本轮开始时本地 HEAD | `3bea0b1bf601842992687379d0e95d98385735cc`（基线 + 3 个未推送本地提交） |
| 本轮测试修改与证据 | `fba51044` |
| 报告提交（最终功能性 HEAD） | `83f1dac0` |
| HEAD 元数据提交 | 仅更新本表、不改变裁决 |

未创建 PR、未合并 main、未 force push/rebase；未修改 `src/` 或发布镜像/JAR/Web。

## 2. 发布镜像与授权

### 2.1 镜像

`third_party/dc-ems/images/`（gitignored，未提交）：

| 文件 | SHA256 | MANIFEST |
|---|---|---|
| `dc-ems-server.tar` | `5694498d…` | 匹配 |
| `timescaledb.tar` | `8a501f98…` | 匹配 |

`docker load` 后：

- `dc-ems-server:1.1.4-583d5da` → `sha256:30779caf…`
- TimescaleDB → 按发布锁打标签 `dc-ems-timescaledb:2.7.0-pg14` → `sha256:0f5553d3…`

镜像内校验：

- `dc-ems.jar` SHA256 = `1df1c053…`，与 MANIFEST 一致。
- `html/index.html` 存在；Server 在 HTTPS 443 提供 `/dc-ems` 与 `/api`。
- `postgres --version` = 14.17，与 `images.lock.json` 一致。

发布包不导出独立 `dc-ems-web` 镜像；测试 overlay 已改为 Server 同端口 Web/API。

### 2.2 合法授权

- 用户提供的 `gen-license.sh` 与官方管理工具 SHA 完全一致（未提交）。
- 使用发布 JAR 内官方 `GenLicense` 从 **Server 实际打印的机器码** 生成 172 字符授权码。
- 授权码写入隔离 `data.json`、`docker/dcems.env`、DB `ems_config`，权限 0600；未提交、未输出。
- 为 WSL/容器 dmidecode 回退场景固定隔离 `machine-id`，使授权跨容器重启稳定。
- 未伪造、破解或绕过授权。

## 3. 隔离环境与数据库

- Compose 项目 `dcems-wtg-control`：DB `127.0.0.1:15433`，Server/Web `127.0.0.1:18453`。
- 独立卷 `dcems-wtg-control_database` 及 files/logs/cache/models。
- Server 启动后 DbStructService 自动建表成功：

| 检查 | 结果 |
|---|---|
| `data.d_fp/d_fre/d_inertia/d_wtg01` | 4 张表存在 |
| `data.d_wtg01` 行数 | 668 |
| `device` | `WTG01 / isolated-DCEMS-WTG01 / capacity 2000` |
| `edge_addr_set` | `ctrlReActPow 1.4.41555 F highBit=1 isCtrl=t`；`ctrlActPow 1.4.41557` 同 |
| `edge_server` | `host.docker.internal:15531 MODBUSTCP interval=500` |
| 自动算法开关 | `emsActPowerEnable=false`、`emsReactPowerEnable=false`、`bFreCtrlEnableFlag=false` |
| 授权行 | `ems_config.license` 存在，长度 172 |

## 4. Web 认证与真实 Server 输出

- Web `/dc-ems/` 返回 200 HTML；`/api/login` 真实登录 200 + token。
- 错误密码 → `400 用户密码错误`。
- 无 token `setParam` → `401 无权限`。
- 越界 3000 kW → `400 有功设定值超出范围: 0-2000.0`，无 Gateway 写入。

### 4.1 FC16 原始帧（真实 Server 发出）

经宿主只读 Modbus 代理捕获（仅 15532→15531，抓取后恢复）：

```text
请求: 00300000000b0110a255000204000043e1
       transm=0x0030 unit=1 FC=0x10 address=0xA255(41557) qty=2 bytes=4 data=0000 43e1
响应: 0030000000060110a2550002
```

`0000 43e1` 为 F32 CDAB = **450.0 kW**。同时捕获 82 个 FC03 轮询帧：

```text
003... 03 a253 0004  (address 0xA253 = 41555, qty 4)
```

FC03 连续读取 41555–41558，返回 125.0/450.0 CDAB，证明真实 `comOk` 读取路径。

## 5. T02 功能测试：6 PASS

| 用例 | 结果 |
|---|---|
| 真实 Server 有功 450 kW → DG | PASS |
| 真实 Server 无功 125 kvar → DG | PASS |
| 错误密码拒绝 | PASS |
| 无 token 拒绝 | PASS |
| 越界 3000 kW 拒绝 | PASS |
| Gateway1 断连可观测、恢复后控制可用 | PASS |

### 5.1 有功

| 环节 | 证据 |
|---|---|
| Server | `POST /api/config/setParam {"WTG01_ctrlActPow":450.0}` → 200 |
| FC16 | 地址 41557、CDAB 450.0 |
| Gateway1 事件 | `0ee33ea2c4fc49a290321d40c4982dfc` |
| Commander | `c6fae2ce-7709-42c2-b0df-1f92974cd0bc`，`SUCCEEDED/CONFIRMED_SUCCESS` |
| DG | `APPLIED`，测试断言 `effective_step>0`（同环境前次捕获 6346） |
| Gateway2 | 41557 `[47616,18651]` = 450000 W |
| 物理模型 | 有功稳定 **450008.84 W** |

### 5.2 无功

| 环节 | 证据 |
|---|---|
| Server | 125.0 kvar → 200 |
| FC16 | 地址 41555、CDAB 125.0 |
| Gateway1 事件 | `54c012443a054fb8a88e40d36d06ea54` |
| Commander | `23b3045d-7a5e-48ca-a625-dbae1c5d22c5`，`CONFIRMED_SUCCESS` |
| DG | `APPLIED`（前次捕获 step 6438） |
| Gateway2 | 41555 `[9216,18420]` = 125000 var |

### 5.3 断连与恢复

- 停止 Gateway1 Modbus listener（保留 gRPC 管理面）后，`setParam 450`：
  - Web 仍返回 `200 控制成功`；
  - Server 日志记录 `DataGetterModbusImpl.control` 抛
    `ModbusTransportException: java.net.ConnectException`；
  - Gateway1 无协议写事件，Commander/DG 无新命令。
- 重新启动 listener 并等待 FC03/comOk 恢复后，同一 `setParam` 产生真实 FC16、Commander
  `CONFIRMED_SUCCESS`、DG `APPLIED`。
- 每条命令 `commands` 长度为 1，无重复执行。

## 6. 静态与测试

| 项目 | 结果 |
|---|---|
| `ruff check/format tests/third_party/dc_ems` | PASS（39 files） |
| `mypy` 预检/预检测试/T02 live | PASS |
| preflight + prepare_runtime + seed | **15 passed** |
| contract + WTG chain config | **10 passed** |
| T02 live | **6 passed** |
| wind-hub 控制链 `--check` | PASS |
| Compose 配置校验 | PASS |

## 7. 生产问题（交 ChatGPT）

### P1 — `deploy.sh` 机器码 SYSCODE 不一致

- `third_party/dc-ems/deploy.sh:113` 使用
  `LiscenceUtil machine-code DCEMS`；
- Server `G.java:57` 定义 `SYSCODE = "DC-EMS"`；
- 由 deploy.sh 机器码生成的授权码启动时报 `授权码与服务不匹配`（已实测两次）；
- 使用 Server 错误日志打印的正确机器码（SYSCODE `DC-EMS`）生成授权后启动成功。
- 影响：标准 `deploy.sh` 授权申请流程不可用。
- 建议：将 deploy.sh 的 SYSCODE 参数改为 `DC-EMS`（同步文档/示例），不动 JAR。

### P2 — WSL/容器 machine-id 回退不稳定

- dmidecode 不可用时回退 `/etc/machine-id`；一次性容器生成的机器码与 Server 容器不同。
- 测试侧通过固定隔离 machine-id 解决；真实硬件有 SMBIOS 时通常稳定。
- 建议：授权申请与 Server 运行使用同一 machine-id 上下文，或持久化 machine-id。

### P2 — 最小单风机种子引发 `FactoryPoints.calComOkValues` NPE

- 日志中工厂级后台线程 `dataMain/reactPow` 反复 NPE；设备级控制、FC16、DG 执行不受影响。
- 可能是最小种子缺少厂站点/配置，需在厂站级联测前补齐并复核；不影响本轮 WTG 设备级结论。

## 8. 清理

- `run_wtg_dcems_server.sh --down`：容器停止/删除，卷保留。
- `run_wtg_control_chain.sh --down`：wind-hub 服务停止。
- 端口 `15433/18453/15531/15421/52152/52161/52173/52174/14223/15532` 全部释放。
- 未执行全局 Docker 清理；未删除任何卷或修改其他项目数据。
- 已通过的 `WTG_PHYSICS_PASS`、`WINDHUB_CONTROL_PASS` 未回归。

## 9. 修改文件

| 文件 | 说明 |
|---|---|
| `tests/third_party/dc_ems/prepare_runtime.py` | 生成隔离 machine-id |
| `tests/third_party/dc_ems/compose.wtg_control.yaml` | 固定 machine-id；移除虚构 web 服务 |
| `tests/third_party/dc_ems/run_wtg_dcems_server.sh` | machine-id 校验/导出；两镜像；HTTPS Web |
| `tests/third_party/dc_ems/test_wtg_dcems_server_live.py` | 真实登录/token、无功、物理响应、断连恢复、无重复 |
| `docs/audit/dcems_t02_live_retest_20261127_evidence/` | 证据（已脱敏） |

`third_party/dc-ems/gen-license.sh` 为用户提供的官方工具，未提交。

## 10. 未验证 / 限制

- 厂站级 AGC/AVC/PFR 自动算法与 FactoryPoints 调度闭环。
- 真实风机/PLC 硬件。
- 长时间稳定性与并发。
- deploy.sh P1 修复后的标准授权流程（需 ChatGPT 修改第三方脚本后复验）。
- 无功物理动态响应（本轮只验证 Server→DG→Gateway2 链路与寄存器）。

## 11. 最终裁决

- **`T02_STATIC_PASS`**
- **`T02_ENVIRONMENT_READY`**
- **`T02_SERVER_PASS`**
- **`DCEMS_CONTROL_E2E_PASS`**（WTG 设备级）

限制：厂站级 AGC/AVC/PFR 尚未验证；`deploy.sh` 机器码 P1 与 machine-id P2 需 ChatGPT 修复；最小种子 FactoryPoints NPE 需在厂站级联测前复核。设备级控制链已取得真实 Server FC16、Commander、DG 与物理响应证据。
