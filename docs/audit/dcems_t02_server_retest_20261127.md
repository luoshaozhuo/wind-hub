# DCEMS T02 发布环境与真实 Server 联测报告（2026-11-27）

## 0. 模板与范围

- 仓库未发现《报告模板.md》，本报告未声称符合模板。
- 本轮只做 T02 发布资源预检、环境核查、脚本/预检工具安全测试；未取得资源时停止在部署前。
- 未修改 `third_party/dc-ems/`、`src/`、生产配置、镜像或授权。

## 1. Git 基线

| 项目 | 值 |
|---|---|
| 分支 | `feat/dcems-gateway-smoke-20261126` |
| 起始 HEAD | `ed8be88bfd1f71e36e10ab80c0211c5f878883d0`（本地/远端一致） |
| 最近 4 个提交 | `1ca3b4c3` `98c86510` `fdff1f69` `ed8be88b` |
| 本轮测试修复与证据 | 见第 10 节 |
| 报告提交 | 本文件提交 |

未创建 PR、未合并 main、未 force push/rebase。

## 2. 静态与专项测试

| 项目 | 结果 |
|---|---|
| `ruff check tests/third_party/dc_ems` | All checks passed |
| `ruff format --check tests/third_party/dc_ems` | 39 files formatted |
| `mypy`（预检工具及测试） | Success: 2 source files |
| 请求的专项测试：preflight / prepare_runtime / seed | **15 passed** |
| 非依赖配置/契约：contract / WTG chain config | **10 passed** |
| `bash -n` 两个启动脚本 | PASS |

测试侧修复：

1. 预检测试函数名 `N802`、两文件格式问题。
2. 预检工具返回类型改为 `dict[str, Any]`，消除测试/工具 mypy 错误。
3. `inspect()` 对缺失/非法 `release/VERSION` 返回失败报告而不是 traceback，并新增两条测试。
4. `run_wtg_dcems_server.sh --down` 在 runtime 资源缺失或 Compose 无法解析时，按项目标签仅清理本项目容器/网络，**不删除卷**；`--check/--up` 仍先执行预检。

## 3. 预检工具独立审查

| 检查项 | 结果 |
|---|---|
| 发布版本解析 | PASS：仅接受单个 `release_version=`，拒绝 `../evil`、空值、空格、`test:latest` 等 |
| 三个镜像存在性 | PASS：`docker image inspect`，不 pull/build |
| `data.json` | MISSING（隔离根未准备） |
| Web `index.html` | MISSING |
| `.env` | MISSING |
| `db.property` | MISSING |
| JKS | MISSING |
| `log4j.properties` | MISSING |
| 授权配置 | MISSING（不读取/不输出值） |
| 数据库业务 Schema | `UNVERIFIED`（始终不自动判 READY） |
| 实际 Server FC16 | `UNVERIFIED`（始终不自动判 READY） |
| 密码/令牌/授权码泄露 | 未发现；报告仅含状态与路径名，单测断言 secret 不出现在报告 |
| 缺失资源失败状态 | PASS：`--json` 退出码 2，`ready_for_start_attempt=false`，`ready_for_T02_acceptance=false` |
| `--down` 资源缺失清理 | PASS：退出码 0，未删除本项目卷，未触碰其他容器/卷 |

实测预检缺失清单（`run/dcems-control-wtg`）：

```text
image:dc-ems-server:1.1.4-583d5da
image:dc-ems-web:1.1.4-583d5da
image:dc-ems-timescaledb:2.7.0-pg14
seed
web:index
runtime:env
runtime:db
runtime:jks
runtime:logging
license:configured
```

`run_wtg_dcems_server.sh --check` 在预检阶段以退出码 1 停止，输出
`T02 release prerequisites missing; see preflight report`；未启动任何容器。

## 4. 本机发布资源搜索

| 资源 | 结果 |
|---|---|
| Docker 本地镜像 | 无任何 `dc-ems` / `timescale` 镜像 |
| Docker 项目容器/卷 | 无 `dcems-wtg-control` 项目容器或卷 |
| `/home/luo` 下的镜像/发布归档 | 未找到 `dc-ems*.tar`、`images*.tar`、`timescaledb*.tar` |
| `/home/luo/dc-ems/release` | 不存在 |
| `/tmp`、`/var/tmp`、`/mnt`、`/media` 镜像归档 | 未找到 |
| JKS / `db.property` | 未找到 |
| 授权码环境变量/合法授权文件 | 未找到 |
| `third_party/dc-ems/images/` | 不存在 |
| 本机 DCEMS 源码树 | 有 JAR/Web 源码与构建脚本，但无发布镜像；不执行 `docker build` |

### 4.1 随仓 MANIFEST 完整性

`sha256sum -c MANIFEST.sha256`：

- 所有非镜像文件（README、VERSION、JAR、Web 静态资源、config、脚本、compose）均 **OK**。
- 两个 MANIFEST 明确列出的镜像归档缺失：

```text
5694498dfbfd6e6fd5ad6b9759674cd217f3084e9d34c8f49ccc527713427ac7  ./images/dc-ems-server.tar
8a501f98cd6cad490fe99786991ad8dba88235b0834a7b1faa69e36fe68944b5  ./images/timescaledb.tar
```

- `dc-ems-web` 镜像/归档未出现在当前 MANIFEST 条目中，发布包不完整。

## 5. 环境阻断结论

`T02 = ENVIRONMENT_BLOCKED`

必需但缺失：

1. `dc-ems-server:1.1.4-583d5da`
2. `dc-ems-web:1.1.4-583d5da`
3. `dc-ems-timescaledb:2.7.0-pg14`
4. 对应可信 `images/*.tar` 与完整 MANIFEST
5. 隔离 runtime：`docker/dcems.env`、`docker/config/db.property`、`docker/config/ssl/dc-ems.jks`、`docker/config/log4j.properties`
6. 合法 `LICENSE_CODE`
7. 隔离种子 `data.json` 与 Web 资源（可由现有生成器准备，但授权/runtime 无法伪造）
8. 隔离数据库卷与经真实初始化验证的业务 Schema

不得以 JAR 直接替代发布镜像、不得 `docker build`、不得拉取来源不明镜像、不得伪造授权。

建议取得方式：

- 从授权发布渠道获取与 `1.1.4-583d5da` 完全匹配的发布包；
- 校验 `VERSION` 与 `MANIFEST.sha256`，确认 `images/dc-ems-server.tar`、`images/timescaledb.tar`（及 web 镜像）齐全；
- 由现场提供合法 `db.property`、`dc-ems.jks`、`LICENSE_CODE`；
- 仅在资源齐备后执行 `prepare_runtime`、`run_wtg_dcems_server.sh --check/--up`；
- 通过 DbStructService 日志与真实表/记录确认数据库初始化，再执行 T02 live。

## 6. T02 真实控制

`T02_SERVER_NOT_RUN`

- 未启动 DCEMS Server/Web/DB。
- 未产生真实 Server FC16。
- 无 Commander/DG/物理响应证据。
- 不因预检/静态通过而推断 T02 通过。

## 7. 安全与清理

- 未输出密码、JKS、license 或令牌。
- 预检报告不含 secret 值；单测覆盖 redaction。
- `--down` 在资源缺失时安全退出 0，仅按 `com.docker.compose.project=dcems-wtg-control` 清理本项目容器/网络，保留卷。
- 未执行全局 Docker 清理；未删除其他项目卷。
- 未修改已通过的 `WTG_PHYSICS_PASS` 与 `WINDHUB_CONTROL_PASS` 代码/测试。

## 8. 问题清单

- P0：无。
- P1：无。
- P2（测试侧，已修复）：
  1. 预检缺失 `VERSION` 时 traceback；
  2. 预检/测试 mypy 类型与 ruff N802/格式；
  3. `--down` 在 runtime 缺失时被 Compose 前置检查阻断。
- 环境阻断：见第 5 节。

## 9. 修改文件

| 文件 | 说明 |
|---|---|
| `tests/third_party/dc_ems/preflight_wtg_dcems_server.py` | 缺失/非法 VERSION 失败状态；返回类型 |
| `tests/third_party/dc_ems/test_preflight_wtg_dcems_server.py` | 新增缺失/非法 VERSION 测试；修复 N802 |
| `tests/third_party/dc_ems/run_wtg_dcems_server.sh` | `--down` 安全清理；预检顺序 |
| `docs/audit/dcems_t02_server_retest_20261127_evidence/` | 证据目录（无 secret） |

提交与最终 HEAD 见交付摘要。

## 10. 未验证

- 真实 DCEMS Server/Web/DB 启动、授权生效、`comOk`、FC03 轮询。
- 数据库业务 Schema/记录初始化。
- 真实 Server FC16、Commander/DG/物理响应。
- DCEMS Web 认证/会话、越界拒绝、Server→Gateway 断连、恢复。
- Docker bridge → Gateway1（上一轮已实测可达；本轮无 Gateway 运行，未重测）。

## 11. 最终裁决

- **`T02_STATIC_PASS`**
- **`T02_ENVIRONMENT_BLOCKED`**
- **`T02_SERVER_NOT_RUN`**
- **`DCEMS_CONTROL_E2E_BLOCKED`**

解除条件：提供匹配 `1.1.4-583d5da` 的可信镜像/完整发布包、合法授权、JKS/db.property、隔离 DB schema，完成真实 Server 启动与库表初始化，再执行 T02 live。在此之前不得宣布 DCEMS 跨系统控制链通过，也不得进入 AGC/AVC/PFR 自动算法联测。
