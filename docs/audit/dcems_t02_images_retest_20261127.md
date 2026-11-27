# DCEMS T02 镜像补充与真实 Server 环境复测（2026-11-27）

## 0. 模板与范围

- 仓库未发现《报告模板.md》，本报告未声称符合模板。
- 本轮补充了可信发布镜像，完成校验/导入、隔离 runtime/seed、预检、Compose 契约与静态测试。
- 由于缺少合法 `LICENSE_CODE`，未启动 DCEMS Server/DB/Web，未执行 T02 live。
- 未修改 `third_party/dc-ems/`、`src/`、镜像内容或授权。

## 1. Git 基线

| 项目 | 值 |
|---|---|
| 分支 | `feat/dcems-gateway-smoke-20261126` |
| 起始 HEAD | `ed8be88bfd1f71e36e10ab80c0211c5f878883d0`（本地/远端一致） |
| 本轮测试修改/证据 | 见第 11 节 |
| 报告提交 | 本文件提交 |

未创建 PR、未合并 main、未 force push/rebase。

## 2. 镜像补充与完整性

新增文件位于 `third_party/dc-ems/images/`：

| 文件 | SHA256 | MANIFEST 匹配 |
|---|---|---|
| `dc-ems-server.tar` | `5694498dfbfd6e6fd5ad6b9759674cd217f3084e9d34c8f49ccc527713427ac7` | 是 |
| `timescaledb.tar` | `8a501f98cd6cad490fe99786991ad8dba88235b0834a7b1faa69e36fe68944b5` | 是 |

`docker load` 结果：

- `dc-ems-server:1.1.4-583d5da` → `sha256:30779cafbfc62090718db55e93c9bb857b2f7e831dad4ccc927cd8a137fe4e4e`
- TimescaleDB tar 无 RepoTag → 按 MANIFEST/发布锁中的镜像 ID 打标签
  `dc-ems-timescaledb:2.7.0-pg14` → `sha256:0f5553d37abbe0f20bfe0a04dab5d1238955b5cda2711aba403090f95b6e51ff`

镜像内验证：

- `/opt/dc-ems/dc-ems.jar` SHA256 = `1df1c053265c7d1127755b4792646a2e5f3503fd5ba6f23fae43c6cbcf9ae7d2`，与发布 MANIFEST 一致。
- `/opt/dc-ems/html/index.html`、`data.json`、`log4j.properties` 存在。
- TimescaleDB 内 `postgres --version` = PostgreSQL 14.17，与 `images.lock.json` 一致。

### 2.1 发布镜像集合澄清

发布包 `package_images.py` 仅导出 `dc-ems-server.tar` 与 `timescaledb.tar`，不导出
`dc-ems-web` 镜像。Server 镜像内含 `html/`，并在同一 HTTPS 443 提供 `/dc-ems`
静态页与 `/api`；`deploy.sh` 也只 load 上述两个 tar。

因此测试侧移除了虚构的 `dc-ems-web` 服务与镜像要求：

- `compose.wtg_control.yaml` 仅保留 DB + Server，Server 绑定 `127.0.0.1:18453:443`。
- 预检只要求 Server + TimescaleDB 两个发布镜像。
- `README_wtg_control.txt` 已同步真实发布契约与登录/token 说明。
- `test_wtg_dcems_server_live.py` 改用 `https://127.0.0.1:18453`，执行真实
  `/api/login` 获取 token，后续 `/api/config/setParam` 携带 `token` 头；
  新增错误密码、无 token、越界设定值拒绝测试。

这不是降低验收：真实 Server、真实 Web/API、真实登录会话与真实 FC16 仍为必需。

## 3. 隔离 runtime 与种子

已在 `run/dcems-control-wtg/` 准备：

- wind-hub 控制链配置（`--prepare`、`--check` 通过）。
- `data.json`：单设备 `WTG01`、地址 `1.4.41555`/`1.4.41557`、CDAB、自动算法关闭。
- `docker/dcems.env`、`docker/config/db.property`、`docker/config/log4j.properties`。
- `docker/config/ssl/dc-ems.jks`：按发布说明使用 keytool 生成的自签名 JKS（非授权码）。
- DB 密码为本地随机值，仅存于 0600 运行文件；未提交、未输出。
- `LICENSE_CODE` 为空；未生成/伪造任何授权码。

## 4. 预检结果

```text
READY:      image:dc-ems-server:1.1.4-583d5da
READY:      image:dc-ems-timescaledb:2.7.0-pg14
READY:      seed
READY:      runtime:env
READY:      runtime:db
READY:      runtime:jks
READY:      runtime:logging
MISSING:    license:configured
UNVERIFIED: database:business_schema
UNVERIFIED: server:actual_modbus_fc16
READY_FOR_START_ATTEMPT=False
```

`--json` 退出码 2；`run_wtg_dcems_server.sh --check` 在预检阶段退出 1，未创建容器。
`docker compose config --quiet` 通过；`--down` 在无服务时安全退出 0，未删除卷。

## 5. 静态与测试

| 项目 | 结果 |
|---|---|
| `ruff check tests/third_party/dc_ems` | PASS |
| `ruff format --check` | 39 files |
| `mypy` 预检/预检测试/live 测试 | PASS |
| preflight + prepare_runtime + seed | **15 passed** |
| contract + WTG chain config | **10 passed** |
| wind-hub `run_wtg_control_chain.sh --check` | PASS |
| `bash -n` 启动脚本 | PASS |
| Compose 契约 | PASS |

## 6. 授权阻断

缺少合法 `LICENSE_CODE`，发布 Server 的 `EmsApp.checkLicense` 在启动阶段拒绝空授权，
因此本轮停止在启动前，符合任务约束。

已使用发布镜像官方命令生成机器码，供向授权方申请授权：

```text
见交付摘要中的机器码（未写入 Git、未写入证据目录）
```

取得合法授权后，下一步：

1. 将 `LICENSE_CODE` 写入隔离 `docker/dcems.env`（0600，不提交）。
2. `run_wtg_dcems_server.sh --check` → `--up`。
3. 核查 DB 健康、DbStructService 建表/初始化日志，以及 `ems_config`、设备、
   `edge_addr_set` 中 `WTG01`/`ctrlActPow`/`ctrlReActPow` 实际记录。
4. 核查 Server 业务健康、`comOk`、FC03 轮询、Gateway1 可达。
5. 启动 wind-hub 控制链与 DG Scenario，执行 `DCEMS_WTG_DCEMS_LIVE=1` T02 live。

## 7. T02 真实控制

`T02_SERVER_NOT_RUN`

- 未启动 Server/DB；
- 未产生真实 Server FC16；
- 无 Commander/DG/物理响应证据；
- 不因预检或镜像完整性通过而宣布 T02 通过。

## 8. 安全

- 未提交/输出 DB 密码、JKS、license、令牌或机器码。
- 未执行 `docker build`、未拉取未知镜像。
- 未启动任何 DCEMS 容器；`--down` 仅按项目标签清理容器/网络，卷保留。
- 未修改已通过的 `WTG_PHYSICS_PASS`、`WINDHUB_CONTROL_PASS` 生产代码/测试。

## 9. 问题清单

- P0：无。
- P1：无。
- P2（测试侧，已修复）：
  1. 测试 overlay 虚构 `dc-ems-web` 镜像；已按真实发布契约改为 Server 同端口 Web/API。
  2. 预检镜像集合由 3 改为真实的 2。
  3. live 测试缺少真实登录/token；已补充并新增负向鉴权/越界测试。
- 环境阻断：合法 LICENSE_CODE；DB schema 与 FC16 仍未验证。

## 10. 未验证

- Server/DB 启动、授权生效、数据库自动初始化。
- 真实 Server FC16、comOk、FC03 轮询。
- Web 登录/token、错误密码、无 token、越界拒绝的真实运行结果（测试已编写，NOT_RUN）。
- Commander/DG/物理响应由真实 Server 驱动。
- Docker bridge → Gateway1 在本轮未重测（上一轮已实测可达）。

## 11. 修改文件

| 文件 | 说明 |
|---|---|
| `tests/third_party/dc_ems/compose.wtg_control.yaml` | 移除虚构 web 服务，Server 同端口提供 Web/API |
| `tests/third_party/dc_ems/run_wtg_dcems_server.sh` | 两镜像/无 web 资源检查/HTTPS 提示 |
| `tests/third_party/dc_ems/preflight_wtg_dcems_server.py` | 两镜像、无 web:index |
| `tests/third_party/dc_ems/test_preflight_wtg_dcems_server.py` | 适配真实镜像集合 |
| `tests/third_party/dc_ems/test_wtg_dcems_server_live.py` | HTTPS/登录 token/鉴权与越界负向测试 |
| `tests/third_party/dc_ems/README_wtg_control.txt` | 真实发布契约说明 |
| `docs/audit/dcems_t02_images_retest_20261127_evidence/` | 证据（无 secret） |

## 12. 最终裁决

- **`T02_STATIC_PASS`**
- **`T02_ENVIRONMENT_BLOCKED`**（仅缺合法 `LICENSE_CODE`；镜像/runtime/seed 已就绪）
- **`T02_SERVER_NOT_RUN`**
- **`DCEMS_CONTROL_E2E_BLOCKED`**

提供合法授权后即可继续真实 Server 启动、DB 初始化验证与 T02 live；在取得真实 Server
FC16 与下游物理响应前，不得宣布 T02 或完整 DCEMS 跨系统控制链通过。
