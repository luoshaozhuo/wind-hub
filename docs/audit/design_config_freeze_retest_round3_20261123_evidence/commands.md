# 第三轮实际执行命令与退出码

工作目录 `/home/luo/wind-hub`，固定审查 HEAD `dc71f60bb869874648f5051b34f33bef44985c98`。所有 Schema/Loader 均为纯文件只读调用，未启动业务服务、数据库或中间件。

| 命令 | 退出码 | 结果/证据 |
|---|---:|---|
| `python3 scripts/dev.py env` | 0 | wind-hub 环境 PASS；见 `environment.log` |
| `git fetch origin docs/design-consolidation-20261123` | 0 | 本地、远端跟踪和 ls-remote 均为交接 HEAD，初始工作区干净；见 `git-baseline.log` |
| `find . -name '报告模板.md' -o -name '*报告模板*.md' -o -name '*report*template*.md'` | 0，无输出 | 未找到用户指定模板；报告沿用第二轮结构并记录该交付偏差 |
| `python -m scripts.audit_business_points --root deploy/config_roots/prod-a --output /tmp/wind-hub-design-config-retest-r3/prod-a-strict-audit.csv --strict` | 0 | 29 YAML、2 表、121 点、0 flagged |
| `python -m scripts.audit_business_points --root deploy/config_roots/sim-a --output /tmp/wind-hub-design-config-retest-r3/sim-a-strict-audit.csv --strict` | 0 | 23 YAML、5 表、103 点、0 flagged |
| `python -m scripts.audit_business_points --root deploy/config_roots/e2e-final --output /tmp/wind-hub-design-config-retest-r3/e2e-final-strict-audit.csv --strict` | 0 | 35 YAML、5 表、103 点、0 flagged |
| `python -I /tmp/wind-hub-design-config-retest-r3/loader_checks.py /home/luo/wind-hub` | 0 | prod-a@1.6.0 10 台、sim-a@1.4.0 5 台、e2e-final@1.0.1 6 台 Domain Snapshot 全 PASS；见 `loader-checks.log` |
| `python -I /tmp/wind-hub-design-config-retest-r3/static_root_checks.py /home/luo/wind-hub /tmp/wind-hub-design-config-retest-r3` | 0 | 117 个源 YAML、87 个根 YAML 严格解析；引用、来源点、Topic/Broker、87 哈希、Kafka 原 Blob 全 PASS；见 `static-root-summary.json` |
| `python -I /tmp/wind-hub-design-config-retest-r3/schema_checks.py /home/luo/wind-hub` | 1 | Configuration Schema：prod 29/29、sim 23/23、e2e 33/35；sim bootstrap PASS；三个 sim 第一方运行时解析均 REJECTED；见 `schema-checks.log` |
| `python -I /tmp/wind-hub-design-config-retest-r3/sim_isolation_checks.py /home/luo/wind-hub` | 0 | 指定 530xx 端口、独立 target/state/log、无 CLI TLS、生产重叠 0、五个 Scenario Gateway 路由一致；见 `sim-isolation-checks.log` |
| `python -I -`（台账逐行对照 `git ls-files -s configs`，仅版本化 YAML） | 0 | 117/117 Blob 匹配；78 UNCHANGED_HISTORY/发布未知、39 NEW_CANDIDATE/未发布；见 `ledger-checks.log` |
| `python -I /tmp/wind-hub-design-config-retest-r3/migration_checks.py /home/luo/wind-hub` | 0 | 8 个新 PointTable 除版本/依赖外语义相同；其它迁移无意外变化 |
| `python -I docs/audit/design_config_freeze_retest_20261123_evidence/gw2mw_retest.py /home/luo/wind-hub /tmp/wind-hub-design-config-retest-r3` | 0 | 293 原定义未变、107 新定义完整、111/111 点结构 PASS、111 OEM PENDING |
| `git hash-object configs/templates/sinks/sink_kafka_history_v1.1.0.yaml` | 0 | `460fb5d96235b4a6713c85af6102efbde9f85f78`，与 N-01 要求一致 |
| `git hash-object docs/design/wind-hub_总体逻辑架构设计_v1_27.md` | 0 | `891486c006c5f964ffa2e61e950104102004f9ab`，冻结总体未改 |
| 报告链接、51 个问题 ID、状态计数、87 行哈希及 111 点结果自检；`git diff --check` | 0 | 链接无缺失，ID 唯一，状态计数与报告一致，补丁无空白错误 |

## 复测脚本自身的首轮失败

- `schema_checks.py` 首轮把实际类名 `ConfigurationApplication` 误写为 `ConfigurationService`，触发 ImportError；原文保留在 `schema-checks-initial.log`。修正类名后得到上表实际 Schema 结果，最终退出 1 来自两个 e2e 文档字段，不是导入失败。
- 台账脚本首轮把非 YAML 的 `configs/history/demo.csv` 也纳入路径分母，得到 117 对 118；原文保留在 `ledger-checks-initial.log`。按任务“版本化 YAML”过滤后，最终 117/117 通过。该首轮差异不是台账漏项。

## 未执行

Ruff、mypy、pytest、业务模块测试、系统测试、E2E、性能、真实发布、业务进程、Broker、TDengine 与现场设备均 NOT_RUN。运行时解析器的纯文件拒绝只用于登记代码兼容性，不构成业务测试。
