# DCEMS 授权部署与 FactoryPoints 整改独立复测报告（2026-11-27）

## 0. 模板与范围

- 仓库未发现《报告模板.md》，本报告未声称符合模板。
- 范围：P1 标准部署授权流程、P2-1 机器身份持久化、P2-2 FactoryPoints 补丁、T02 设备级回归与静态门禁。
- 未修改 wind-hub 其他模块生产代码、未覆盖正式 JAR/镜像、未绕过授权、未创建分支/PR、未合并 main。

## 1. Git 基线

| 项目 | 值 |
|---|---|
| 分支 | `feat/dcems-gateway-smoke-20261126` |
| 整改基线 HEAD | `00545c316a9b6c5fd1abe7e2291ec50c3afb06ee`（fetch 后本地/远端一致） |
| 基线后新增提交 | 8 个（`14ffa312`…`00545c31`），涉及 deploy.sh/compose/MANIFEST/契约测试/补丁/README |
| 本轮测试侧修改与证据 | 见第 10 节 |
| 报告提交 | 本文件提交 |

## 2. 官方发布资源

| 资源 | 值 |
|---|---|
| Server 镜像 | `dc-ems-server:1.1.4-583d5da`（`sha256:30779caf…`，未修改） |
| DB 镜像 | `dc-ems-timescaledb:2.7.0-pg14`（`sha256:0f5553d3…`，未修改） |
| JAR（镜像内） | `dc-ems.jar` `sha256:1df1c053…` = 发布 MANIFEST 值 |
| 镜像归档 | `images/dc-ems-server.tar`、`images/timescaledb.tar` 与 MANIFEST SHA256 一致 |
| 授权工具 | 发布 JAR 内 `cn.hedpy.scada.license.GenLicense` / `LiscenceUtil`（官方） |
| `sha256sum -c MANIFEST.sha256` | **通过**（修改后的 deploy.sh/compose 已同步更新 MANIFEST） |

## 3. P1 — 标准部署授权流程：PASS

### 3.1 整改基线已具备（复核通过）

- `deploy.sh` 机器码系统标识为 `DC-EMS`（非 `DCEMS`），契约测试断言通过。
- 机器码生成容器与 Server 通过同一持久 `config/machine-id` 挂载到 `/etc/machine-id`，实测宿主文件与容器内身份一致。

### 3.2 本轮新发现并修复的 3 个标准流程阻断（允许范围内最小修复）

| 编号 | 缺陷 | 复现 | 修复 |
|---|---|---|---|
| B1 | `ensure_license` 使用服务名 `dc-ems-db` 作为容器名，实际容器为 `dc-ems-dc-ems-db-1` | `Error response from daemon: No such container: dc-ems-db` | 改为发布 Compose 实际容器名 |
| B2 | 所有 `psql` 未携带 `PGPASSWORD`，且错误被 `2>/dev/null` 吞掉 | 授权写入静默失败，Server 报 `授权码为空` | 统一 `psql_db()` 助手注入 `PGPASSWORD`；写入路径保留 stderr |
| B3 | 授权 SQL 使用已废弃的 `id/remark` 列（现表为 `var_path`，主键为序列 `id`） | 语法/列错误 | 改为 `var_path` 语义 + psql 变量绑定（防注入）+ 写后校验 + 写入后重启 Server |

附带修复（同属标准流程阻断）：

- `write_db_property`：Server 会把 `db.property` 回写为 Java properties（`\:` 转义、重排、加注释），原文本比较导致第二次部署必然失败。改为按字段语义比较（con/user/passwd/driver），仍拒绝连接身份变化。
- `ensure_machine_id`：新增 32 位十六进制格式校验；旧部署身份为空时明确报错并清理残留空文件（不静默生成新身份）。

### 3.3 完整流程实测（隔离副本 `run/dcems-deploy-retest`，独立端口/卷）

```text
首次 deploy.sh --mode containerized（无授权）
  → 健康检查通过，打印机器码（DC-EMS，172 字符，挂载持久 machine-id）
官方 GenLicense(machine-code, 20 年) → 172 字符授权码
写入 .env → 再次 deploy.sh
  → “正在写入授权码… 授权码已写入。 授权码检查完成。” exit=0
  → Server 启动无授权错误，https listened，Web 200
```

证据：`evidence/p1/deploy-first-no-license.txt`、`deploy-license-write-success.txt`、`deploy-repeat-skip-license.txt`（均已脱敏）。

## 4. P2-1 — 机器身份持久化：PASS

| 工况 | 结果 | 证据 |
|---|---|---|
| 首次部署生成并保存身份 | PASS（`config/machine-id` 33 字节，0600） | `p1/` |
| Server 重启授权保持 | PASS：machine-id 与容器一致，授权错误 0，容器内 Web OK | `p2_1/server-restart.txt` |
| 容器删除后重建授权保持 | PASS：`--force-recreate` 后 file=container 身份一致、授权错误 0 | `p2_1/container-recreate.txt` |
| 重复执行部署脚本 | PASS：machine-id 哈希不变、授权 172 字符跳过重写 | `p2_1/repeat-deploy.txt` |
| 已部署旧环境升级 | PASS：旧容器非空身份被 `docker cp` 迁移，新 Compose 重建后授权有效 | `p2_1/legacy-upgrade*.txt` |
| 缺少 machine-id | 有 `.deploy-mode`：迁移自原 Server 容器（PASS）；无法迁移时明确报错 | `p2_1/missing-machine-id.txt` |
| 为空 | 明确报错并清理残留 | `p2_1/reject-empty.txt`、`legacy-upgrade.txt` |
| 格式非法 | 明确报错（32 位十六进制校验） | `p2_1/reject-invalid.txt` |
| 符号链接 | 明确拒绝（`! -L`） | `p2_1/reject-symlink.txt` |
| 发布包校验 | `sha256sum -c MANIFEST.sha256` 通过 | `static/static-gates.txt` |

Docker 挂载：发布 Compose 以 `type=bind` 只读挂载 `${DEPLOY_CONFIG_DIR}/machine-id → /etc/machine-id`；在本机 Docker Desktop/WSL2 环境实测挂载有效（容器内身份与文件一致）。未修改宿主机或其他项目机器身份。

## 5. P2-2 — FactoryPoints 补丁

### 5.1 补丁本体：PASS

- 目标文件：`server/emslib/.../FactoryPoints.java`，`checkIsLimit()` 位于 1101 行，与补丁上下文逐行一致。
- **修复补丁头行数**（原声明 `@@ -1101,10 +1101,13 @@`，实际 9/12 行）→ `@@ -1101,9 +1101,12 @@`，现在 **`patch --fuzz=0` 干净应用**（原需 fuzz 1）。
- 补丁仅把三个 `Boolean` 标志改为 `Boolean.TRUE.equals(...)`，`null` 不产生拆箱，也不推定授权；`&&` 限值逻辑不变。

### 5.2 编译：PASS（隔离构建）

- 在 `run/fp-build`（隔离副本，不改 `/home/luo/dc-ems`）应用补丁，`JAVA_HOME=dc-ems env (JDK 1.8.0_472)`、`mvn -o -DskipTests package` **成功**，产出 `emslib-1.0.jar`（`sha256:830f43d1…`）；未打补丁基线同样可编译（`8ea98f1e…`）。
- 未替换发布 JAR，未重建镜像。

### 5.3 行为测试：PASS（真实 emslib 类 + 反射调用私有 `checkIsLimit`）

| 用例 | 补丁后 | 基线（未打补丁） |
|---|---|---|
| 三标志全 `null` | `false`（不抛异常，`null` 不推定授权） | `false`（内部 NPE 被 catch 吞掉并记错误日志） |
| 一个 `true`、其余 `null` | **`true`（授权生效）** | **失败：仍返回 `false`（NPE 吞掉，合法授权被忽略）** |
| 全 `false` | `false` | — |
| 授权开但超限值 | `false`（原限值逻辑不变） | — |
| 授权开且限值内 | `true` | — |

证据：`evidence/p2_2/patched-behavior.txt`、`baseline-behavior.txt`、`patch-apply.txt`、`patch-apply-compile.txt`。

### 5.4 运行时：`FACTORYPOINTS_RUNTIME = NOT_VERIFIED`

- 补丁**未进入发布 JAR**；发布镜像 `dc-ems-server:1.1.4-583d5da` 内仍是未修复字节码。
- 发布 Server 运行日志实测（上一轮同镜像证据，已摘录至本轮 `p2_2/released-jar-npe-runtime.txt`）：
  `NullPointerException at FactoryPoints.checkIsLimit(FactoryPoints.java:1103)`，每秒由 `dataMain/actPow/reactPow` 线程重复——正是补丁修复的行。
- 结论：补丁正确性已验证，但生产缺陷只有在 DCEMS 侧重新构建 JAR/镜像并独立验收后才能关闭。

### 5.5 残留同类缺陷（P2，交 ChatGPT）

1. `checkIsLimit` 中 `emsActSend/emsActLoopRef/emsLimitCoe/fWFContractCapacity` 仍为 `Double` 拆箱，`null` 会 NPE（被 catch 吞掉→静默 `false`），建议同样做空值防护。
2. 相邻计算 `calComOkValues(FactoryPoints.java:1394)` 在发布日志中同样 NPE，补丁未覆盖。
3. 仓库内 `gen-license.sh`（管理工具）未列入 `MANIFEST.sha256`（不影响 `sha256sum -c` 结果）。

## 6. T02 设备级回归：PASS（6/6）

隔离根 `run/dcems-control-wtg` 全新重建（NATS `14223`、9 个随机令牌、风链 `--prepare/--check/--up`、mode=1 初始化、官方机器码+GenLicense 授权、`run_wtg_dcems_server.sh --check/--up`）。

| 用例 | 结果 |
|---|---|
| 真实 Server 有功 450 kW → DG | PASS |
| 真实 Server 无功 125 kvar → DG | PASS |
| 错误密码拒绝 | PASS |
| 无 token 拒绝 | PASS |
| 越界 3000 kW 拒绝 | PASS |
| Gateway1 断连可观测、恢复后控制可用 | PASS |

最终证据：`regression/pytest-t02-final.txt`（`6 passed, 23.01s`）。

- 有功链：`setParam 450` → Gateway1 事件 `006999b9…` → Commander `a3efc821…` `CONFIRMED_SUCCESS` → DG `APPLIED` → Gateway2 41557=`450000 W` → **物理有功 451237.98 W**。
- 无功链：`regression/dcems-server-reactive-evidence.json` `CONFIRMED_SUCCESS` + `APPLIED` + `125000 var` 回读。
- **原始 FC16（本轮抓帧）**：`00270000000b0110a255000204000043e1`
  unit=1、FC=0x10、address=0xA255(41557)、qty=2、data=`000043e1`（F32/CDAB = 450.0）；同时捕获 105 个 FC03 轮询帧。见 `regression/modbus-frames-summary.json`、`modbus_frames.log`。

## 7. 静态门禁：PASS

```text
bash -n third_party/dc-ems/deploy.sh                     OK
pytest contract/preflight/prepare_runtime/seed           17 passed
ruff check tests/third_party/dc_ems                      All checks passed
ruff format --check tests/third_party/dc_ems             40 files formatted
sha256sum -c MANIFEST.sha256                             MANIFEST OK
docker compose (base + WTG overlay, env-file)            compose OK
```

证据：`evidence/static/static-gates.txt`。

## 8. 分项裁决

| 验收项 | 裁决 |
|---|---|
| `DEPLOY_LICENSE` | **PASS** |
| `MACHINE_ID_PERSISTENCE` | **PASS** |
| `FACTORYPOINTS_PATCH` | **PASS** |
| `FACTORYPOINTS_RUNTIME` | **NOT_VERIFIED** |
| `DCEMS_T02_REGRESSION` | **PASS** |
| `STATIC_GATES` | **PASS** |

P1/P2-1 已关闭；FactoryPoints 生产缺陷**未关闭**（补丁未进入发布 JAR，发布 Server 仍每秒 NPE）。

## 9. 资源清理

- WTG 隔离 Server/DB、标准部署测试栈、wind-hub 控制链、NATS 容器全部停止/删除。
- 测试卷（`dcems-wtg-control_*`、`dc-ems_*`）全部删除；无 dc-ems 容器/卷残留。
- 端口 `14223/15433/18453/15531/15532/15421/52152/52161/52173/52174/5432/18080` 全部释放。
- 临时根 `dcems-control-wtg`、`dcems-deploy-retest`、`dcems-license-source`、`fp-build`、`fp-patch-test` 已删除；临时令牌/授权/机器码文件已删除。
- 证据目录敏感扫描：无令牌、授权码、会话 token、DB 密码。
- 未触碰其他项目容器/卷（`wind-hub-r5-*` 等保持原状）。

## 10. 修改文件与提交

| 文件 | 说明 |
|---|---|
| `third_party/dc-ems/deploy.sh` | B1/B2/B3 + db.property 语义比较 + machine-id 格式/空旧身份校验 |
| `third_party/dc-ems/MANIFEST.sha256` | 同步 deploy.sh 摘要 |
| `tests/third_party/dc_ems/patches/factorypoints-null-dispatch-flags.patch` | 修正 hunk 行数，实现 fuzz=0 应用 |
| `tests/third_party/dc_ems/prepare_runtime.py` | machine-id 落到 `config/machine-id`（与发布 Compose 挂载一致）、0600 |
| `tests/third_party/dc_ems/compose.wtg_control.yaml` | 移除重复 machine-id 挂载（由基础 Compose 提供） |
| `tests/third_party/dc_ems/run_wtg_dcems_server.sh` | 移除失效的 machine-id 变量引用 |
| `tests/third_party/dc_ems/test_dcems_release_deployment_contract.py` | ruff format |
| `docs/audit/dcems_deployment_license_factorypoints_retest_20261127_evidence/` | 25 个证据文件（已脱敏） |

提交与最终 HEAD 见交付摘要。

## 11. 未验证 / 遗留

1. `FACTORYPOINTS_RUNTIME`：需 DCEMS 侧用补丁重建 `emslib`/JAR/镜像后复测（本轮仅隔离编译+行为验证）。
2. `calComOkValues` 及其他相邻 `getVal()` 拆箱空值缺陷（发布日志已见 NPE）。
3. `checkIsLimit` 内 Double 字段空值防护。
4. 厂站级 AGC/AVC/PFR、真实硬件、长时间稳定性。
5. `gen-license.sh` 未纳入 MANIFEST。

## 12. 最终结论

标准部署授权流程、机器身份全生命周期、静态门禁与 T02 设备级回归全部通过；FactoryPoints 补丁正确并可编译验证，但因未进入发布产物，`FACTORYPOINTS_RUNTIME=NOT_VERIFIED`，生产缺陷保持开放，等待 ChatGPT 完成 JAR/镜像重建与发布包更新。
