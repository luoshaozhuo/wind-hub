# Wind-hub CLI 三阶段独立验收报告（Round 1）

- 验收日期：2026-10-30
- 验收人：Claude Code（独立验收，未参与生产业务逻辑开发）
- 工作分支：`feat/interactive-diagnostic-cli`
- 生产基准 HEAD（验收起点）：`b67d88fbbef7dc58455266a01646dbcd16377ba9`
- Merge-base（与 main）：`cb380ddb3bee249defb75e5922e2b70987a18211`
- 验收后代码/测试 HEAD：`c609b0a51f6ca7a4428404ceb7a03e4f22836acc`
  （本报告为其后一个 docs 提交）
- Worktree：验收提交后 clean；未创建 PR、未合并、未 rebase、未 force push、未推送远端

## 0. 最终结论

**FAIL（针对交付基准 b67d88fb）→ 修复后 PASS WITH LIMITATIONS（针对 c609b0a5）。**

基准 HEAD 上 `wind-hub` 控制台入口**完全无法启动**（SyntaxError），且
`mypy src` 有 10 个错误、`python -m cli.main` 为空操作；即三阶段代码在
交付时未通过任何静态与导入冒烟。验收方按授权做了**零业务逻辑变更**的
静态阻断修复（commit `1a2059e2`）并重建了全部 CLI 测试（commit
`c609b0a5`，109 项）。修复后静态门禁全绿、新测试 109/109、core+cli
861/861、全树 2032 项可收集无残留导入。

遗留：2 个 P2 生产行为问题、2 个 P2 契约/文档一致性问题（见第 5 节），
真实硬件/netem/soak 未运行（NOT_RUN）。

## 1. 环境与静态检查

环境检查 `python3 scripts/dev.py env`：RESULT: PASS（wind-hub conda，
Python 3.11.16，pytest/ruff/mypy/importlinter 齐备）。

### 基准 b67d88fb（修复前，失败证据）

```text
$ ruff check src/cli tests/cli
src/cli/main.py:3:1   I001 import 块未排序
src/cli/main.py:12:20 F401 typing.Any 未使用
src/cli/main.py:170:9 N802 do_EOF（cmd.Cmd 框架强制名）
src/cli/main.py:271   B018 无用表达式（实为配置门禁，见修复说明）
src/cli/main.py:389   E731 赋值 lambda
src/cli/presentation.py:91:43 SyntaxError: Expected ')', found newline
src/cli/protocol_diagnostic.py:3:1 I001
Found 7 errors.
$ ruff format --check src/cli
error: Failed to parse src/cli/presentation.py:91:43
6 files would be reformatted
$ mypy src/cli
10 errors（inventory cfg 类型重绑定 8、emptyline 签名 1、payload 类型收窄 1）
$ python -m cli.main --help ; echo $?
0            # 无任何输出：缺少 __main__ 守卫
$ python -c "import cli.main"
SyntaxError: '(' was never closed (presentation.py:91)
```

`lint-imports`：基准即 **11 kept, 0 broken**（含 “cli 只依赖 shared
core，不依赖进程包”）。

### 验收后 c609b0a5（按门禁实际命令）

```text
$ python -m ruff check src tests scripts
All checks passed!
$ ruff format --check src/cli tests/cli
12+ files already formatted
$ mypy src
Success: no issues found in 312 source files
$ lint-imports
Contracts: 11 kept, 0 broken.
```

`pyproject.toml` / `poetry.lock` / CLI 入口：本分支相对 merge-base **无
任何变更**；入口仍为 `wind-hub = "cli.main:main"`。删除 `cli/client.py`
后全仓（src/tests/pyproject）无 `cli.client` 残留引用；全树 2032 项测试
`--collect-only` 成功。唯一残留文案：`pyproject.toml:280` 注释仍提
“cli client”（P2-4）。

### 验收方静态修复内容（commit 1a2059e2，零业务逻辑变更）

- `presentation.py:91` 补闭合括号；补 `from typing import Any`（函数签名
  已在使用）。
- `main.py`：删除未用导入；`gateways` 的 `self.config.current` 改为
  `_ = self.config.current`（保留“必须已加载配置”门禁语义）；listen
  内联赋值 lambda 改为等价嵌套函数；`emptyline` 返回 bool 对齐
  `cmd.Cmd` 父类签名（return False）；benchmark 结果变量改名
  `benchmark_payload`，消除与 listen 返回类型的 mypy 收窄冲突；
  补 `if __name__ == "__main__": raise SystemExit(main())`（其余 6 个
  进程包均有此守卫）。
- `inventory.py`：三协议配置使用独立变量名并显式标注
  `effective: dict[str, Any]`，纯类型修复。
- `protocol_diagnostic.py`：超长英文字符串隐式拼接（E501）。
- 全目录 `ruff format`（7 文件存在格式漂移）。
- **1 处 noqa 披露**：`do_EOF` 增加 `# noqa: N802`。理由：cmd.Cmd 按
  固定方法名分发 Ctrl-D，改名即破坏 EOF 退出；项目已有同类框架强制名
  先例（`src/core/infrastructure/sink/modbus/server.py:38` 行内 noqa、
  pyproject 对 3 个 grpc server 的 N802 per-file-ignore）。如裁决不接受
  noqa，可改为 per-file-ignore 或在 `default` 中处理 `"EOF"`。

## 2. 测试重建与结果

旧 4 个测试文件（test_cli/test_direct_control/test_placement_cli/
test_real_runtime，共 425 行）已按授权删除。新测试 8 个文件 109 项：

```text
tests/cli/conftest.py                       真实 YAML 副本 + FakeDriver/句柄/录制器
tests/cli/test_config_session.py            24 项
tests/cli/test_inventory.py                  8 项
tests/cli/test_shell.py                      8 项
tests/cli/test_network_diagnostic.py         9 项
tests/cli/test_protocol_diagnostic.py       30 项
tests/cli/test_pipeline.py                  19 项
tests/cli/test_results_and_output.py        11 项
```

覆盖任务书 12 类契约：config validate/read/reload/diff/show（缺文件、
YAML 错、引用错、失败保留旧快照、校验门禁、`--scope all`）、inventory
（含 gateway NOT_DEFINED）、shell（help/解析/参数错误/补全/写历史清理/
HOME 隔离）、network（loopback 通/拒/超时、DNS、route、ping 形状、
设备名解析 endpoint、子进程超时 kill 回收）、protocol 七操作、取消与
ADS prepare/release 配对、UNKNOWN 写入不自动重试（write_calls==1）、
自动诊断顺序/失败即停/断点恢复（指纹变化、--restart、篡改历史、
failed_index 越界）、result/report（空结果、原子导出 fsync+os.replace、
无 .tmp 残留、目录/后缀校验）、退出码 0/1/2/3 与 JSON 信封。

```text
$ python -m pytest tests/cli -q
109 passed in 1.48s
$ python -m pytest tests/core tests/cli -q
861 passed in 47.22s
$ python -m pytest --collect-only -q tests
2032 tests collected
```

未跑全量 system/services 套件：本机存在用户遗留容器
`wind-hub-test-redis` 占用 6379（与历轮记录的环境冲突同态），
compose 型测试在收集阶段即 ERROR（例：
`tests/collector/test_grpc_server.py`，ComposeError: Bind 0.0.0.0:6379
already allocated）。该错误与本分支无关（CLI 不改 compose/服务），
标记 **UNRELATED（环境）**；merge-branch 统一验证前需清理遗留容器后
复跑。

## 3. 重点审查结论（任务书第三节）

| 审查点 | 结论 | 证据 |
|---|---|---|
| 自动诊断 Connection 生命周期/失败清理 | PASS | pipeline `close_driver` 在切换、sink 前、finally 三处关闭；fail-fast 用例实证两台 driver 均 close=1；取消用例实证 close+release |
| 同设备只建一次会话 | PASS | ads 双设备夹具：每设备 connects=1，check+3 点读复用；切换序 ads-001.close < ads-010.connect |
| 是否可能误报 PASS | PASS（带 P2-2 边界） | health=false/connect 异常/create_driver 异常均 FAIL；BAD=WARNING；歧义 sink=UNKNOWN；汇总优先级 FAIL>UNKNOWN>WARNING>PASS |
| IEC104 GI 部分成功/超时语义 | PASS | incomplete→UNKNOWN 信封且保留已收样本；通用 TimeoutError→FAIL；shell 退出码 3/1 分别实证 |
| Ctrl+C 后句柄/任务/连接释放 | PASS（真机 NOT_RUN） | task.cancel 实证 subscribe_stop→close 顺序；listen 无 duration 等 Event，finally 先停句柄再关连接；network 子进程 BaseException kill+wait |
| 配置指纹与快照一致 | PASS | 指纹=8 文件（名+长度+字节）sha256；校验前后双读检测中途变更；恢复严格比对指纹与已完成步骤三元组 |
| 凭据泄漏 | PASS | 向临时配置注入真实 redis 用户名/密码，inventory/CLI JSON 输出均不含；sink 白名单仅 host/port/database/endpoint/bootstrap_servers 等；失败信封的 protocol_options 来自解析后安全字段 |
| Help/参数/行为一致性 | 基本一致（P2-3） | 见第 5 节 |
| 旧 CLI 契约残留 | 基本清理（P2-4） | client.py 删除无引用；--config/--format/--timeout/console script 正常；交互冒烟 9 条命令行为正确 |
| 写入确认 | PASS | 单次执行必须 YES 或 --yes；NaN/null/list/dict 值拒绝；UNKNOWN 明示不得盲目重试 |

## 4. 复现过的交互冒烟

```text
$ printf 'config read .../example_modbus\ndevices\nsinks\ngateways\n' | python -m cli.main
# PASS 信封 + 生效 endpoint/options；sinks 无密码；gateways UNKNOWN/NOT_DEFINED
$ printf 'diagnose tcp\necho-after\nexit\n' | python -m cli.main
# usage error 后整个 shell 直接退出（P2-1 实证）
```

## 5. 遗留问题（交 ChatGPT 处理，验收方未改业务逻辑）

### P2-1 网络子命令缺参/未知参数会杀死整个交互 Shell

- 位置：`src/cli/main.py` `_diagnose` 网络分支（argparse `parse_args`，
  约 300-303 行）。
- 复现：交互模式输入 `diagnose tcp`（缺 host）→ argparse 抛 SystemExit(2)，
  `default()` 的异常白名单（ConfigError/ValueError/OSError/RuntimeError/
  KeyError）不捕获，cmdloop 终止，后续命令不执行。未知 `--flag` 同理。
- 建议：与 `_options` 一致改用 parse_known_args + 手动 ValueError，
  或在 `default` 中把 SystemExit 转为 FAIL 信封（single-shot 仍退出码 2）。
- 现状钉死：`tests/cli/test_shell.py::
  test_network_missing_host_argparse_kills_shell_is_known_gap`。

### P2-2 plan() 静默省略白名单外 Sink，PASS 不披露未检项

- 位置：`src/cli/pipeline.py:33`，仅 redis/tdengine/kafka/grpc 进入计划；
  modbus 类入站 sink（以及未来新类型）被静默跳过，scope/limitations
  文案也未枚举排除集合。只有“歧义 TCP 地址”才给 UNKNOWN。
- 风险：现场仅有非白名单 sink 时自动诊断仍可能整体 PASS，运维误以为
  全部 Sink 已检。
- 建议：对 enabled 但不参与 TCP 探测的 sink 显式生成 SKIPPED/UNKNOWN
  步骤，或在 limitations 中列明类型集合。

### P2-3 Help 与行为的轻微不一致

- `help gateways` 标注 `[GATEWAY_ID]`，但 dispatch 对参数静默忽略。
- `config validate PATH --scope=all`（等号形式）不被接受，仅接受
  `--scope all`。
- 全局参数只接受固定次序（--config → --format → --timeout），与 help
  行不矛盾但 argparse 用户可能尝试任意顺序。

### P2-4 旧契约文案残留

- `pyproject.toml:280` 注释仍提 “cli client”，该模块已删除。

## 6. NOT_RUN 与边界

- 真实 TwinCAT/ADS L3、真实 Modbus/IEC104 链路、netem 七项、长稳 soak：
  本机无硬件/无损伤环境，未运行；ADS 本地 AMS 生命周期仅经
  prepare/release 替身配对测试验证。
- 真实 redis sink TCP 探测：6379 被遗留容器占用，未做活链路；
  pipeline sink 分支经 monkeypatch tcp/地址解析单测覆盖。
- `ping` 真实用例在无 ping 二进制或无 ICMP 权限环境自动 skip，
  不断言 PASS。
