# 第二轮实际执行命令与退出码

工作目录 `/home/luo/wind-hub`，固定审查 HEAD `8f31ed59cb7f06e72f80880c564346399e882019`。所有 Loader 均为只读纯文件调用，未启动业务服务或中间件。

| 命令 | 退出码 | 结果/证据 |
|---|---:|---|
| `python3 scripts/dev.py env` | 0 | wind-hub 环境 PASS；见 `environment.log` |
| `git fetch origin docs/design-consolidation-20261123` | 0 | 本地、远端跟踪和 ls-remote 均为交接 HEAD，工作区干净；见 `git-baseline.log` |
| `python -m scripts.audit_business_points --root deploy/config_roots/prod-a --output /tmp/wind-hub-design-config-retest-r2/prod-a-strict-audit.csv --strict` | 0 | 29 YAML、2 PointTables、121 点、0 flagged；见 `prod-a-strict-audit.log/.csv` |
| `python -m scripts.audit_business_points --root deploy/config_roots/sim-a --output /tmp/wind-hub-design-config-retest-r2/sim-a-strict-audit.csv --strict` | 0 | 23 YAML、5 PointTables、103 点、0 flagged；见 `sim-a-strict-audit.log/.csv` |
| `python -m scripts.audit_business_points --root deploy/config_roots/e2e-final --output /tmp/wind-hub-design-config-retest-r2/e2e-final-strict-audit.csv --strict` | 0 | 35 YAML、5 PointTables、103 点、0 flagged；见 `e2e-final-strict-audit.log/.csv` |
| `python -I /tmp/wind-hub-design-config-retest-r2/loader_checks.py /home/luo/wind-hub` | 0 | prod-a 10 台、sim-a 5 台、e2e-final 6 台完整 Domain Snapshot PASS；点集 121/103/103；见 `loader-checks.log` |
| `python -I /tmp/wind-hub-design-config-retest-r2/static_root_checks.py /home/luo/wind-hub /tmp/wind-hub-design-config-retest-r2` | 1 | 102 个源 YAML 与 87 个根 YAML 严格解析、引用、来源点、Topic/Broker、哈希全部完成；唯一失败为既有 `sink_kafka_history_v1.1.0.yaml` 同版本 M；见 `static-root-checks.log`、`static-root-summary.json`、`root-source-hash-manifest.csv` |
| `python -I /tmp/wind-hub-design-config-retest-r2/migration_checks.py /home/luo/wind-hub` | 0 | 8 个新 PointTable 除版本/依赖外语义相同；Device/Model/Delivery/Storage 仅预期迁移变化；见 `migration-checks.log` |
| `python -I docs/audit/design_config_freeze_retest_20261123_evidence/gw2mw_retest.py /home/luo/wind-hub /tmp/wind-hub-design-config-retest-r2` | 0 | 293 原定义未变、107 新定义完整、111/111 点结构 PASS、111 OEM PENDING；见 `gw2mw-retest.log`、`gw2mw_retest_summary.json` |
| `python -I -`（对照 prod/sim CLI 的 endpoint、manager target、state_dir、TLS profile） | 0 | 五个共同管理目标 endpoint/target 均相同，state_dir 相同且 sim 保留 mTLS profile；见 `domain-target-overlap.log` |
| `git diff --name-status 227323ac..HEAD -- configs/` | 0 | 24 个新增候选路径；1 个既有路径 M：`sink_kafka_history_v1.1.0.yaml` |
| `git hash-object docs/design/wind-hub_总体逻辑架构设计_v1_27.md` | 0 | 与 HEAD blob 同为 `891486c006c5f964ffa2e61e950104102004f9ab`，本轮未修改冻结总体 |
| 报告链接、45 个问题 ID、旧问题状态计数、87 行哈希及 111 点结果自检；`git diff --check` | 0 | 链接无缺失，ID 唯一，状态计数与报告一致，证据完整，补丁无空白错误 |

## 复测脚本自身的首轮失败

`loader_checks.py` 首轮错误读取不存在的 `SharedDomainSnapshot.devices` 属性，三个域均在 Snapshot 已构建后触发 `AttributeError`，退出 1。该问题属于统计脚本，不是配置 Loader 拒绝。原始日志保存在 `loader-checks-initial.log`；改用 `SharedDocuments.devices` 统计后，最终命令退出 0。未删除首轮失败证据。

## 未执行

Ruff、mypy、pytest、单模块业务测试、系统测试、E2E、性能、真实发布、Repository validate-only、业务进程、Broker、TDengine 和现场设备均为 NOT_RUN。纯文件 Loader/Domain Snapshot 不属于软件测试套件通过声明。
