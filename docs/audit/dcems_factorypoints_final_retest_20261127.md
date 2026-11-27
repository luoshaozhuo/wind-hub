# DCEMS FactoryPoints 最终整改独立复测报告（2026-11-27）

## 0. 模板与范围

- 仓库未发现《报告模板.md》，本报告未声称符合模板。
- 范围：FactoryPoints 扩展补丁审查、Java 编译与行为验证、真实 Server 运行时 A/B、T02 控制回归与静态门禁。
- 未修改 wind-hub 其他模块生产代码、未覆盖正式发布 JAR/镜像、未创建分支/PR、未合并 main。

## 1. Git 基线

| 项目 | 值 |
|---|---|
| 分支 | `feat/dcems-gateway-smoke-20261126` |
| 整改 HEAD | `9b8752eb41d8df5dec521067bd623ae979c4ac03`（fetch 后本地/远端一致） |
| 本轮核实的 3 个整改提交 | `abf03f68`（扩展补丁）、`7cc726c1`（hunk 行数同步）、`9b8752eb`（契约测试） |
| 本轮修改 | 见第 9 节 |

## 2. 补丁审查：PATCH_APPLY = PASS

- 目标：`server/emslib/.../FactoryPoints.java`，与发布对应源码（`/home/luo/dc-ems`，commit `4c45932`）逐行匹配。
- 隔离副本（`run/fp-final`，已清理）执行：

```text
patch -p1 --fuzz=0 --dry-run  → exit 0（仓库根 a/server 布局）
patch -p1 --fuzz=0            → exit 0
patch -p3 --fuzz=0（模块根）   → exit 0
```

- 无 fuzz、无 reject、未修改原始源码仓库、未覆盖正式 JAR。

### 2.1 补丁范围（9 个 hunk）

ChatGPT 原有：

1. `checkIsLimit`：三个 `Boolean` 标志 `Boolean.TRUE.equals` 防拆箱；四个 `Double` 输入显式判空后保持严格 `<` 语义。
2. `calComOkValues`：`emsPowAct` 先 `ifNull` 再参与 `max(0,…)`；`emsTurboOutPowerCalEnable`、`emsRunState` 改 `Boolean.TRUE.equals`。

本轮独立运行时验证新增（均为同模式空值守卫，`null → 默认语义`，不改算法）：

3. `factoryActUpdate` 回调：`iCtrlPowerPoint`（单参 `new Value<>(0)` 默认 `val=null`）判空。
4. `factoryReactUpdate` 回调：`iCtrlReaPowerPoint` 同类判空。
5. `calComOkValues` 尾部：`emsReactAvoidCurrentOverFlowEnable` 改 `Boolean.TRUE.equals`。
6. `calNumStatistic`/`calTheoryPower`/`newOptimizePower`/`optimizePower` 中 **12 处 `emsWorkMode` 拆箱**判空（`emsWorkMode = new Value<>(0)` 单参默认 null；修复过程中曾因 `&&`/`||` 优先级产生错误守卫，已用括号修正并复测）。

契约测试 `test_dcems_release_deployment_contract.py` 已同步为 9 个 hunk 头与新增守卫断言。

## 3. JAVA_COMPILE = PASS

| 项目 | 值 |
|---|---|
| JDK | OpenJDK 1.8.0_472（Zulu，`~/miniconda3/envs/dc-ems`） |
| Maven | Apache Maven 3.6.3（离线 `-o`，本地 `~/.m2`） |
| 源码 commit | `/home/luo/dc-ems` `4c45932`（发布标识 `1.1.4-583d5da`，`git_dirty=true`） |
| 命令 | `mvn -o -DskipTests package`、`mvn -o test` |
| 结果 | 两版均 `BUILD SUCCESS`（emslib 无 `src/test`，测试由独立 harness 承担） |
| 产物 | 补丁版 `emslib-1.0.jar` `548c4d6a…`；基线 `df778035…` |
| 依赖问题 | 见第 4 节（完整 Server 构建阻断） |

**关键一致性证据**：基线源码树编译出的 `WindTurbo/FactoryConfig/GlobalData/Value/FactoryPoints.class` 与发布 JAR 内对应类 **SHA256 逐字节一致**（同 JDK/同参数），证明源码树与发布物等价。

## 4. 完整 Server 构建：BLOCKED（记录）

- `server/dc-ems` 离线构建失败：授权源码树缺少 `cn.hedpy.scada.ems.core.proface.models.*`（`ModbusDataModel` 等），全 git 历史均无；发布 JAR 中存在该包 → 源码树相对发布物不完整（发布自 `git_dirty=true` 构建）。
- 另有离线插件缺口：`maven-surefire-plugin:3.2.5`、`junit-jupiter:5.6.3` 不在本地仓库（隔离副本内曾临时调整，未提交、未触碰原仓库）。
- 因此按任务允许路径，运行时验证采用**派生 JAR**：发布 JAR 中仅 `FactoryPoints.class` 替换为补丁构建版本。
  - 派生 JAR `c9681ee0…`：`FactoryPoints.class` == 补丁构建；**其余 26075 个类与发布 JAR 逐字节一致**；`testzip` 通过。
  - 该派生 JAR 仅用于隔离测试容器（只读 bind-mount 覆盖 `/opt/dc-ems/dc-ems.jar`），**不修改正式镜像/发布包**，不作为发布验收产物。

## 5. Java 行为验证（真实类 + 反射调用私有方法）

Harness：`evidence/java/FactoryPointsNullTest.java`（包 `cn.hedpy.scada.emslib`，直接使用真实 `FactoryPoints`/`WindTurbo`/`GlobalData`）。

### 5.1 checkIsLimit（12 用例）

| 工况 | 补丁版 | 基线 |
|---|---|---|
| 三个 Boolean 全 null | false，无异常 | false（内部 NPE 被 catch，记错误日志） |
| 三个全 false | false | false |
| 一个 true、其余 null、限值内 | **true** | **false（FAIL：合法调度被错误拒绝）** |
| 四个 Double 各自单独为 null | 均 false，无异常 | false（catch 掩盖） |
| 全有效 + 使能 + 限值内 | true | true |
| 使能但超限 | false | false |
| 未使能 + Double 全 null | false，无异常 | false |
| 恰好等于限值 | false（严格 `<` 保持） | false |
| 略小于限值 | true | true |

### 5.2 calComOkValues（20 用例）

覆盖：功率 null/0/负（配置开/关截断）/正、运行状态 null/false/true、空列表、三机混合，断言有功和、绝对有功和、并网台数、并网容量、平均出力。

- 补丁版：**全部 PASS**（含残余项检查 `RESIDUAL_OK none`）。
- 基线：**6 FAIL**（`cco_power_null_no_npe`、`cco_power_zero_sum_zero`、`cco_negative_*`、`cco_run_null_not_counted` 等 NPE/中断），缺陷复现成立。

行为证据：`evidence/java/patched-behavior.txt`（32 PASS，exit 0）、`baseline-behavior.txt`（6 FAIL，exit 1）。

## 6. 运行时验证：FACTORYPOINTS_RUNTIME = PASS

隔离环境：WTG 单风机链（NATS `14223`、9 令牌、官方机器码+GenLicense 授权、`run_wtg_dcems_server.sh`）。

### 6.1 A/B 对照（同一最小种子）

| 运行 | 窗口 | `NullPointerException` | `check factory limit status error` | 线程死亡（`Exception in thread`） |
|---|---|---|---|---|
| 发布 JAR（基线） | 启动+75s | **103**（全部 `checkIsLimit:1103`） | **103** | 0 |
| 派生补丁 JAR v5 | 启动+100s | 284（**全部为 `WindTurbo.reactStateCheck:749`，被 `mainData` catch**） | **0** | **0** |
| 派生补丁 JAR v5 + T02 全量 | 累计约 5 分钟 | 同上（均为 catch 类） | **0** | **0** |

结论：**此前反复出现的 `checkIsLimit`/`calComOkValues` NPE 与 `dataMain`/`reactPow` 线程死亡全部消失**；容器内 JAR SHA256 与派生 JAR 一致（`8717660c…`→v5 `c9681ee0…`）；Web/API 正常（200、`https listened`、授权无错误）。

证据：`evidence/runtime/baseline-released-jar.txt`、`patched-v5-window1.txt`、`patched-v5-post-t02.txt`。

### 6.2 残留（P2，交 ChatGPT）

- `WindTurbo.reactStateCheck(WindTurbo.java:749)`：`emsWorkMode.getVal()` 拆箱 NPE，**被 `mainData` catch（不杀线程）**，但每秒重复一条 ERROR 日志，且该周期该机组无功状态计算被跳过。
- 成因：补丁使 `calComOkValues` 能执行到 `wt.mainData()`（基线在此之前已中止），属推进后新暴露；历史发布日志中该错误为 0 次。
- `WindTurbo` 另有多处 `emsWorkMode` 拆箱（777/942/990/1041/1633），仅修 749 只会移动 catch 位置；建议 ChatGPT 对 `WindTurbo` 做同类系统性判空（不在本轮 `FactoryPoints` 补丁范围内）。
- 最小种子下 `emsWorkMode` 无 SCADA 数据源为 null；真实厂站有数据时不会触发。

## 7. DCEMS_CONTROL_REGRESSION = PASS

派生补丁 JAR 下执行原 T02 六项（`test_wtg_dcems_server_live.py`）：

```text
6 passed, 244 warnings in 22.87s
```

- 有功：Server `setParam 450` → 事件 `d8ec3a9f…` → Commander `4f717539…` `CONFIRMED_SUCCESS` → DG `APPLIED` → Gateway2 `450000 W` → **物理有功 450005.6 W**。
- 无功、错误密码、无 token、越界 3000 kW、Gateway1 断连恢复：全部通过。
- 证据：`evidence/regression/pytest-t02-v5.txt`、`dcems-server-control-evidence.json`、`dcems-server-reactive-evidence.json`。

## 8. STATIC_GATES = PASS

```text
patch --fuzz=0 dry-run/apply              exit 0
bash -n third_party/dc-ems/deploy.sh      OK
ruff check tests/third_party/dc_ems       All checks passed
ruff format --check tests/third_party/dc_ems   40 files formatted
pytest test_dcems_release_deployment_contract  3 passed
sha256sum -c MANIFEST.sha256              MANIFEST OK
gen-license.sh 是否列入 MANIFEST            否（0 处；发布包外管理员工具，符合预期）
mvn -o test（emslib 补丁版）               BUILD SUCCESS
```

证据：`evidence/static/static-gates.txt`。

## 9. 本轮修改文件

| 文件 | 说明 |
|---|---|
| `tests/third_party/dc_ems/patches/factorypoints-null-dispatch-flags.patch` | 扩展至 9 hunk：回调配置判空 + `emsWorkMode` 12 处判空（含 OR 链括号修正） |
| `tests/third_party/dc_ems/test_dcems_release_deployment_contract.py` | 同步 9 个 hunk 头与新增守卫断言 |
| `tests/third_party/dc_ems/compose.wtg_patched_jar.yaml` | 新增：运行时复测专用只读派生 JAR 挂载 |
| `tests/third_party/dc_ems/run_wtg_dcems_server.sh` | `DCEMS_WTG_PATCHED_JAR` 环境变量门控引入上述 overlay |
| `docs/audit/dcems_factorypoints_final_retest_20261127_evidence/` | 14 个证据文件（已脱敏） |

## 10. 资源清理

- WTG Server/DB 容器与卷、wind-hub 控制链、NATS 容器全部停止/删除；端口全部释放。
- 临时根 `dcems-control-wtg`、`dcems-license-source`、`fp-final`（含派生 JAR/源码副本）已删除。
- `~/.m2` 中临时替换的 `emslib-1.0.jar` **已恢复原文件**（SHA 与备份一致）。
- 证据敏感扫描：无令牌/授权/密码。

## 11. 分项裁决

| 验收项 | 裁决 |
|---|---|
| `PATCH_APPLY` | **PASS** |
| `JAVA_COMPILE` | **PASS** |
| `CHECK_IS_LIMIT` | **PASS** |
| `CAL_COM_OK_VALUES` | **PASS** |
| `NULL_SAFETY_REGRESSION` | **PASS** |
| `FACTORYPOINTS_RUNTIME` | **PASS** |
| `DCEMS_CONTROL_REGRESSION` | **PASS** |
| `STATIC_GATES` | **PASS** |

## 12. 生产缺陷是否真正关闭

**已关闭（运行时证明）**：

1. `checkIsLimit` Boolean 拆箱 NPE；
2. `checkIsLimit` Double 输入空值；
3. `calComOkValues` 功率/运行状态空值 NPE 及其导致的 `dataMain`/`reactPow` 线程死亡；
4. 补丁推进后新暴露的 `emsWorkMode` 拆箱（`calNumStatistic` 等 12 处）与两处更新回调配置空值。

**未完成的发布步骤（须 ChatGPT 处理）**：

1. 在**官方发布流水线**中应用该补丁、重建 `emslib`/`dc-ems.jar`/镜像并更新 `MANIFEST.sha256`；本轮派生 JAR 仅为测试证据，不是发布物。
2. 补齐授权源码树缺失的 `proface/models` 包（否则完整 Server 无法从源码构建）；离线补 `surefire 3.2.5`/`junit-jupiter 5.6.3` 或调整构建配置。
3. `WindTurbo.reactStateCheck` 等 `emsWorkMode` 拆箱的系统性判空（P2，catch 类日志噪声 + 周期性无功计算跳过）。
4. 发布后在正式验收环境复跑本报告第 5–8 节。

## 13. 未验证 / 限制

- 厂站级 AGC/AVC/PFR、真实风机/PLC、长稳。
- 真实厂站 SCADA 数据下 `emsWorkMode` 非空路径的统计正确性（本轮仅最小种子）。
- 派生 JAR 方案的等价性基于“字节级一致”证明；官方重建后仍需独立验收。
