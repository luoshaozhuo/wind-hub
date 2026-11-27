# DCEMS WindTurbo + FactoryPoints 联合补丁最终复测报告（2026-11-27）

## 0. 模板与范围

- 仓库未发现《报告模板.md》，本报告未声称符合模板。
- 范围：两份源码补丁联合审查、WindTurbo/FactoryPoints 行为测试、JDK8/Maven 编译、真实 Server A/B 运行时（≥5 分钟）、T02 设备级回归、静态门禁与清理。
- 未修改 wind-hub 其他模块生产代码、未覆盖正式发布 JAR/镜像、未修改 DCEMS 原始源码工作树、未创建分支/PR、未合并 main。

## 1. Git 基线

| 项目 | 值 |
|---|---|
| 分支 | `feat/dcems-gateway-smoke-20261126` |
| 起始 HEAD | `a9b2b2009e1452b22d6c05ec53b02b144ee10e38`（fetch 后本地/远端一致） |
| 核实的 2 个整改提交 | `ba56eea1`（WindTurbo 补丁）、`a9b2b200`（契约测试） |
| 本轮修改 | 见第 10 节 |

## 2. PATCH_APPLY = PASS

- 目标源码：`/home/luo/dc-ems`（commit `4c45932`），与发布 JAR 字节级一致（见第 5 节）。
- 两份补丁在独立临时目录（`run/joint`，已清理）联合应用：

```text
factorypoints-null-dispatch-flags.patch  -p1 --fuzz=0 dry-run/apply  exit 0
windturbo-null-workmode.patch            -p1 --fuzz=0 dry-run/apply  exit 0
联合应用（模块根 -p3）                    exit 0
```

- 无 fuzz、无 reject、无 offset、无重复补丁、两补丁目标文件不同无冲突。
- 原始源码工作树未被修改（应用均在副本中）。

### 2.1 补丁内容与本轮修复

**FactoryPoints 补丁**（9 hunk，沿用上一轮已验证版本，本轮未改动）。

**WindTurbo 补丁**：ChatGPT 原 7 hunk 覆盖 `reactStateCheck:749`、`777`、`emsWorkModeUpdate:929/942`、`calRunStateShanDong:990`、`calInertiaAlterState:1041`、`checkWinIsOk:1633`。

本轮独立测试发现并最小修复（同模式空值守卫，`null → 字段声明默认值`，不改调度算法），补丁扩至 **23 hunk**：

| # | 位置 | 问题 |
|---|---|---|
| 1 | `reactStateCheck` 727 | `emsConverterRatio/capacity` 单参默认 null 拆箱 |
| 2 | `reactStateCheck` 728 | `emsReactCanCtrl` 拆箱 |
| 3-5 | `reactStateCheck` 754/760/769 | `iCtrlReaLogicPoint` 单参默认 null（null→声明默认 0） |
| 6 | `reactStateCheck` 730/737/744 + 731/738/745 | `emsPowReact` null 存入统计（null→0） |
| 7-9 | `reactStateCheck` 732/741/748 | `Math.abs(...getVal())` 拆箱——**运行时 292 次 NPE 的真实位置** |
| 10 | `reactStateCheck` 759/761/762 | `cos/emsPowAct` 拆箱（reaLogic 1/0 分支） |
| 11 | `emsWorkModeUpdate` 924/927/933 | `emsComState/mOughtPowerT/emsFaultState` 拆箱 |
| 12 | `calLimitState` 970 | `emsActPowerMaxSetFeedback/capacity` 拆箱 |
| 13 | `calRunStateShanDong` 984/994/996/997/1008/1021/1027/1029 | comState/容量/功率/状态/风速/转速/桨距拆箱 |
| 14 | `calInertiaAlterState` 1043/1046/1049 | `emsPowAct/capacity` 拆箱 |
| 15 | `calWorkMode` 1372+ | `workMode/emsWinSpd` 归一化为声明默认值（0），再传入下游分类 |
| 16 | `calRepairState:1312`、`checkRunDetail:1274/1285`、`checkNotRunDetail:1126/1129` | `emsFaultState/workMode/fWTAngleDiff/emsActPowerMaxSetFeedback` 拆箱 |
| 17 | `calSampleDispatch:1070` | `isSample` 拆箱 |

修复过程中曾出现 `&&/||` 优先级与 `double/Double` 类型两处自身错误，均已定位、修正并重新编译验证（见 §4）。

契约测试 `test_windturbo_patch_guards_work_mode` 已同步 hunk 计数与关键守卫断言。

## 3. JAVA_COMPILE = PASS

| 项目 | 值 |
|---|---|
| JDK | OpenJDK 1.8.0_472（Zulu，`~/miniconda3/envs/dc-ems`） |
| Maven | Apache Maven 3.6.3（`mvn -o` 离线） |
| 源码 commit | `/home/luo/dc-ems` `4c45932` |
| 命令 | `mvn -o -DskipTests package`、`mvn -o test` |
| 基线编译 | BUILD SUCCESS（`37acef72…`） |
| 联合编译 | BUILD SUCCESS（`90b7f8e8…`） |
| 类一致性 | 基线与联合 jar 的类集合相同；**仅 `FactoryPoints.class` 与 `WindTurbo.class` 两个类变化**（无 Lambda 内部类增减） |
| 与发布物等价 | 基线构建的两个目标类与发布 JAR 内同名类 **SHA256 逐字节一致**（`f60fabf4…`、`c9f82d5b…`） |

**完整 Server 源码构建仍阻断**（如实记录）：授权源码树缺少 `cn.hedpy.scada.ems.core.proface.models.*`；另有离线 `surefire 3.2.5`/`junit-jupiter 5.6.3` 缺口。运行时按任务允许使用**派生 JAR**：发布 JAR（`1df1c053…`，与 MANIFEST 一致）仅替换上述 2 个类；其余 26074 个类与发布 JAR 完全一致（`testzip` 通过）。派生 JAR `8ef25c4a…` 仅只读挂载于隔离测试容器。

## 4. Java 行为验证

Harness：`evidence/java/WindTurboNullTest.java`（真实 `WindTurbo`/`GlobalData` + 反射调用私有方法），FactoryPoints 32 用例 harness 沿用上一轮（`FactoryPointsNullTest`）。

### 4.1 WindTurbo（35 用例）

| 方法/工况 | 联合补丁 | 基线（未打补丁） |
|---|---|---|
| `reactStateCheck`：workMode null/4/8/16/32/64 | PASS（mode32→`emsReaRun=1`；mode4/8→4 等原语义） | FAIL（NPE） |
| `reactStateCheck`：`isSample=null` | PASS（不推定样板机） | FAIL |
| `reactStateCheck`：`emsLocalOnOff=true + 功率/限值反馈 null`（**运行时真实路径**） | PASS | FAIL（NPE） |
| `reactStateCheck`：`forbidReActPower / canCtrl / reaLogic=1/0 + 功率 null` | PASS | FAIL（NPE） |
| `emsWorkModeUpdate`：comState false/null | PASS（null→非通信，保守早退） | FAIL（null 时 NPE） |
| `emsWorkModeUpdate`：faultState=null | PASS（null→不推定故障） | FAIL |
| `emsWorkModeUpdate`：mode32 并网 / mode64 故障 / mode8 | PASS（`emsRunState=true` / `FaultOrMain` / 分类正确） | 部分 FAIL |
| `calRunStateShanDong`：comState null、mode null/4/32、功率 null | PASS（分类 4/1/2、容量/功率统计正确） | FAIL（NPE 或 `state=null`） |
| `calInertiaAlterState`：mode null、功率 null、高功率 | PASS（false/false/true） | FAIL |
| `checkWinIsOk`：comState null/false、并网就绪、mode4 | PASS（false/false/true/false） | FAIL |
| 三机混合（32/4/null+null） | PASS（分类 a=1,b=2,c=4） | FAIL（c=null 未分类） |

联合 **35/35 PASS（exit 0）**；基线 **7 PASS / 28 FAIL（exit 1）**——问题复现与修复闭环。

### 4.2 FactoryPoints 联合回归（33 用例）

`checkIsLimit` 12 项 + `calComOkValues` 20 项 + 残余检查：联合补丁下 **ALL_CASES_PASS（exit 0）**，与上一轮单补丁结果一致，无回归；调度使能、严格小于限值、功率统计、并网台数/容量、回调配置空值断言全部保留。

## 5. 运行时 A/B：JOINT_RUNTIME = PASS

隔离环境：WTG 单风机链（NATS `14223`、9 令牌、官方机器码+GenLicense、隔离 DB/卷）。

### 5.1 A：原始发布 JAR（`1df1c053…`）

- 窗口：启动 + ≥60 s + 一次真实控制探针。
- `NullPointerException`：**6**（`checkIsLimit:1103` ×4、`calComOkValues:1394` ×2）。
- **线程死亡 2**（`dataMain`、`reactPow` 因 `calComOkValues:1394` 未捕获异常终止）。
- 控制探针：`setParam 450` → Gateway1 事件 `76650a25…` → Commander `SUCCEEDED/CONFIRMED_SUCCESS` → DG `APPLIED` step 2298（控制链独立于统计线程，仍可用）。
- 证据：`runtime/A-released.txt`、`runtime/A-control-probe.json`。

### 5.2 B：联合补丁派生 JAR（`8ef25c4a…`）

- 容器内 JAR SHA256 与派生 JAR 一致；Web `https://127.0.0.1:18453` 返回 200；`https listened`。
- **连续 ≥5 分钟**（12:50:32 → 12:55:38，含 T02 全量执行）：

| 指标 | A（发布） | B（联合补丁） |
|---|---|---|
| `checkIsLimit` NPE | 4 | **0** |
| `calComOkValues` NPE | 2 | **0** |
| `reactStateCheck` NPE | 0（未推进到该处） | **0**（中间版曾 292，见下） |
| NPE 总计 | 6 | **0** |
| 线程死亡 | **2** | **0** |
| `dataMain`/`actPow`/`reactPow` | 死亡 2 个 | **全部存活**（容器内 PID 1，77 线程） |
| 授权错误 | 0 | 0 |
| Web/API | 200 | 200 |
| DB 业务表 | — | `data.d_wtg01` 等存在（473 行） |

中间迭代（`runtime/B-joint-5min.txt`，仅 FactoryPoints+WindTurbo 初版补丁）曾出现 `reactStateCheck:748` 292 次被 catch 的 NPE：运行时 `emsLocalOnOff` 分支被真实状态触发，而该分支在默认配置下不可达、harness 未覆盖。已按任务要求定位到具体行、补最小守卫并新增 5 个分支用例复现/验证，最终窗口归零。

证据：`runtime/B-final-5min.txt`（含线程存活核验）、`runtime/db-tables.txt`、`java/artifact-sha256.txt`。

## 6. DCEMS_T02_REGRESSION = PASS

联合补丁派生 JAR 下 T02 六项：**`6 passed, 244 warnings in 23.17s`**（`regression/pytest-t02-joint-final.txt`）。

- 有功：Gateway1 事件 `c315f37ad4284f4394221d0e8317d50d` → Commander `27e573ac-a7dd-4e98-a88b-4289c2731b13` `CONFIRMED_SUCCESS` → DG `APPLIED` → Gateway2 `450000 W` → 物理有功 `450007.18 W`（`dcems-server-control-evidence.json`）。
- 无功 125 kvar、错误密码、无 token、越界 3000 kW、Gateway1 断连恢复全部通过。
- 未使用测试客户端模拟 DCEMS 出口（全部经真实 Server `/api/config/setParam`）。

## 7. STATIC_GATES = PASS

```text
bash -n third_party/dc-ems/deploy.sh                OK
ruff check tests/third_party/dc_ems                 All checks passed
ruff format --check tests/third_party/dc_ems        40 files formatted
pytest contract/preflight/prepare_runtime/seed      19 passed
sha256sum -c MANIFEST.sha256                        MANIFEST OK
docker compose (base + WTG overlay)                 compose OK
两补丁 patch -p1 --fuzz=0 dry-run/apply             exit 0 / 0
mvn -o test（联合 emslib）                           BUILD SUCCESS
```

证据：`static/static-gates.txt`。

## 8. 生产发布仍需完成的事项（PRODUCTION_RELEASE_READY = NO）

1. **官方流水线应用两份补丁**重建 `emslib`/`dc-ems.jar`/Docker 镜像，更新 `MANIFEST.sha256` 并走正式发布验收；本轮派生 JAR 仅测试用途。
2. 补齐授权源码树 `proface/models` 缺失包与离线构建依赖（`surefire 3.2.5`、`junit-jupiter 5.6.3`），恢复完整 Server 源码构建能力。
3. 发布后在正式环境复跑本报告 §4–§7。
4. 遗留 P2（不阻断本轮）：
   - `reactStateCheck` 中 `emsTheoryReactPower/emsPowReactLow` 在极端配置下写入 0 而非测量值（保守语义，已文档化）；
   - `cos=0` 时 `reaLogic==1` 分支除零得 Infinity/NaN 为**既有行为**（声明默认值即 0），本轮仅消除其空指针路径；
   - 最小种子下 `data.d_fp` 等表在启动早期的 `relation does not exist` 日志为既有启动顺序噪声（表随后创建成功）。

## 9. 资源清理

- WTG Server/DB 容器与卷、wind-hub 控制链、NATS 容器全部停止/删除；端口 `14223/15433/18453/15531/15421/52152/52161/52173/52174` 全部释放。
- 临时根 `dcems-control-wtg`、`dcems-license-source`、`joint`、`joint-mod` 及临时令牌/授权/机器码文件已删除。
- 未修改 Maven 缓存（本轮未向 `~/.m2` 写入；上一轮的替换已恢复并校验）。
- 证据敏感扫描：无令牌/授权/密码。
- 未触碰其他项目资源。

## 10. 本轮修改文件

| 文件 | 说明 |
|---|---|
| `tests/third_party/dc_ems/patches/windturbo-null-workmode.patch` | 7 → 23 hunk：命名方法 + 运行时可达分支的空值守卫（含优先级/类型自纠） |
| `tests/third_party/dc_ems/test_dcems_release_deployment_contract.py` | hunk 计数与关键守卫断言同步 |
| `docs/audit/dcems_factorypoints_windturbo_joint_retest_20261127_evidence/` | 16 个证据文件（已脱敏） |

FactoryPoints 补丁本轮未改动；`run_wtg_dcems_server.sh`/`compose.wtg_patched_jar.yaml` 沿用上一轮已提交的测试侧 JAR 挂载支持。

## 11. 分项裁决

| 验收项 | 裁决 |
|---|---|
| `PATCH_APPLY` | **PASS** |
| `JAVA_COMPILE` | **PASS** |
| `WINDTURBO_NULL_SAFETY` | **PASS** |
| `FACTORYPOINTS_REGRESSION` | **PASS** |
| `JOINT_RUNTIME` | **PASS** |
| `DCEMS_T02_REGRESSION` | **PASS** |
| `STATIC_GATES` | **PASS** |
| `PRODUCTION_RELEASE_READY` | **NO**（官方重建/发布未完成，见 §8） |

## 12. 未验证 / 限制

- 厂站级 AGC/AVC/PFR、真实风机/PLC、长稳。
- 真实厂站 SCADA 全量数据下的统计正确性（本轮为最小种子 + 受控分支注入）。
- `cos=0`、NaN 传播等既有数值边界（非本轮空指针范围）。
- 派生 JAR 等价性基于字节级证明；官方重建后仍需独立验收。
