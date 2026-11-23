# 实际执行命令与退出码

所有命令均在 `/home/luo/wind-hub`、交接 HEAD `2ead92be3aba49b585cb3788db08407ac63a6dae` 上执行；未启动业务服务或中间件。

| 命令 | 退出码 | 结果/证据 |
|---|---:|---|
| `python3 scripts/dev.py env` | 0 | wind-hub 环境 PASS，Python 3.14.7 |
| `git fetch origin docs/design-consolidation-20261123` | 0 | 远端 HEAD 与本地交接 HEAD 一致 |
| `git status --short --branch && git rev-parse HEAD && git rev-parse origin/docs/design-consolidation-20261123 && git ls-remote origin refs/heads/docs/design-consolidation-20261123` | 0 | 三个 SHA 均为 `2ead92be…`，工作区初始干净；见 `git-baseline.log` |
| `python -m scripts.audit_business_points --root configs --output /tmp/wind-hub-design-config-retest-20261123/full-root-audit.csv --strict` | 1 | 预期失败：全集有两版 BusinessPoint，脚本要求显式唯一目录；见 `full-root-audit.log` |
| `python -I -`（自定义 SafeLoader 重复键拒绝，遍历 `configs/**/*.yaml`） | 0 | 78/78 解析、文件名/file_version 匹配、schema_version 为整数；重复键 0；74 个 v1、4 个 v2；见 `yaml-strict-parse.log` |
| `python -I /tmp/wind-hub-design-config-retest-20261123/gw2mw_retest.py /home/luo/wind-hub /tmp/wind-hub-design-config-retest-20261123`（CSV 修复前） | 1 | 111 行 `ads_symbol_csv_match=FAIL`，其余首轮逐点检查通过；见 `gw2mw-retest-before-csv-fix.log`、`gw2mw-results-before-csv-fix.csv` |
| `python -m scripts.audit_business_points --root /tmp/wind-hub-design-config-retest-20261123/gw2mw-closure --output /tmp/wind-hub-design-config-retest-20261123/gw2mw-strict-audit.csv --strict` | 0 | 5 YAML、1 PointTable、111 点、0 flagged；临时根只含 GW2MW 显式闭包；见 `gw2mw-strict-audit.log/.csv` |
| `python -I /tmp/wind-hub-design-config-retest-20261123/gw2mw_retest.py /home/luo/wind-hub /tmp/wind-hub-design-config-retest-20261123`（CSV 修复后） | 0 | 293 原定义未变、107 新定义完整、111/111 结构 PASS、111 OEM 待核验；见 `gw2mw-retest.log`、`gw2mw_retest_summary.json` |
| `python -I /tmp/wind-hub-design-config-retest-20261123/loader_checks.py /home/luo/wind-hub` | 0 | 检查脚本符合预期：未指定 Deployment 版本拒绝；prod-a 全设备混合固定/未固定拒绝；历史/sim/e2e 多目录歧义拒绝；GW-only 域快照 111 点 PASS；见 `loader-checks.log` |
| `git diff -- configs docs/design` | 0且无输出 | 本轮未修改生产 YAML 或任何设计文档，冻结总体保持不变 |
| `git diff --check` | 0 | 补丁无空白错误 |
| 以 `git show HEAD:<mapping CSV>` 和当前 CSV 做字段级对账 | 0 | 111 行仅 `ads_symbol` 改变，其他字段 0 变化；每行均为去除最外层字面双引号 |
| `python -I -`（对照 Sink Template `output.topic` 与 Storage `service.kafka.topic`） | 0 | prod-a `windhub.samples` != `windhub-history`；e2e-final 均为 `windhub.e2e.history`；prod-a 两版均未选 Storage；见 `kafka-storage-targets.log` |
| `python -I -`（逐当前 YAML 路径执行 `git log --diff-filter=M`，比较每个 M 提交父子 YAML） | 0 | 19 路径有 M；11 路径/14 次语义变化，8 路径/10 次仅注释变化；见 `config-history-immutability.log`。Git 历史不能单独证明修改发生于发布前还是发布后 |

临时 GW2MW 闭包由以下原文件逐字复制构成，没有改写历史文件：

```text
configs/templates/business_points_standard_v1.5.0.yaml
configs/templates/units_standard_v1.2.0.yaml
configs/templates/point_tables/point_table_ads_wtg_gw2mw_v1.2.0.yaml
configs/templates/device_models/device_model_wtg_gw2mw_v1.5.0.yaml
configs/templates/protocols/protocol_ads_v1.2.0.yaml
```

`loader_checks.py` 的首次版本遗漏站点文档，导致 GW-only 域快照构造在 `'site_id'` 处失败；该失败属于复测脚本自身，已保留在 `loader-checks-initial.log`，修正站点输入后得到上表最终结果。不得把该首轮脚本失败归责于候选点表。

Kafka/Storage 对照脚本首轮把 `service.kafka` 误写成顶层 `kafka`，退出 1 并产生 `KeyError`；原始证据保存在 `kafka-storage-targets-initial.log`，修正字段路径后得到上表退出 0 的结果。该首轮失败同样不是 YAML 缺陷。

按任务约束，Ruff、mypy、pytest、业务进程、Broker、TDengine、系统测试、E2E、性能与负载均未执行。
