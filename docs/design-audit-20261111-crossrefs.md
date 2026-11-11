# 十份设计文档全文交叉引用机械扫描（第一阶段）

- 基线：`567e30ffb1aa1c1a519c0156991ca22811f389cf`
- 生成方式：连接 GitHub 获取完整原文，逐行扫描 Markdown 章节编号、§/第x.x节引用、规范版本引用和代码围栏；未剔除全部历史文字，检测为候选项而非必然缺陷。
- 范围：docs/design 十份新版本；排除 Web Server/Admin。

| 文件 | 行数 | 编号标题 | 待复核章节引用 | 非现行版本提及 | 代码围栏不闭合 |
|---|---:|---:|---:|---:|---|
| Contracts_软件设计_v1_19.md | 1648 | 70 | 0 | 2 | NO |
| Core_软件设计_v1_32.md | 3594 | 169 | 0 | 0 | NO |
| Configuration_软件设计_v1_14.md | 1110 | 56 | 3 | 0 | NO |
| wind-hub_总体逻辑架构设计_v1_3.md | 430 | 12 | 0 | 0 | NO |
| Collector_软件设计_v1_19.md | 3930 | 131 | 2 | 0 | NO |
| Commander_软件设计_v1_29.md | 2825 | 173 | 3 | 0 | NO |
| industrial_gateway_软件设计_v1_26.md | 2020 | 110 | 1 | 0 | NO |
| Data_Generator_软件设计_v1_22.md | 2395 | 105 | 2 | 0 | NO |
| Sink_Service_软件设计_v1_49.md | 3866 | 178 | 0 | 0 | NO |
| CLI_软件设计_v1_25.md | 2686 | 105 | 0 | 0 | NO |

## 候选问题（每份最多展示 22 条）

### Contracts_软件设计_v1_19.md

Unresolved section references (heuristic, verify manually):
- none

Outdated version mentions (may include historical descriptions):
- L1519: CLI v1_4 → v1_25
- L1519: CLI v1_6 → v1_25

### Configuration_软件设计_v1_14.md

Unresolved section references (heuristic, verify manually):
- L548: §7.1 — 参照《软件系统建模图形与表达规范.md》第 5.1 节 Flowchart、第 7.1 节 DFD 与第 8.1 节算法规范。业务活动图的控制流箭头表示处理顺序，DFD 箭头表示数据流，**两者不得混用**。图中应以不同形状区分处理、判断和数据。
- L548: §8.1 — 参照《软件系统建模图形与表达规范.md》第 5.1 节 Flowchart、第 7.1 节 DFD 与第 8.1 节算法规范。业务活动图的控制流箭头表示处理顺序，DFD 箭头表示数据流，**两者不得混用**。图中应以不同形状区分处理、判断和数据。
- L907: §14.5 — 下表规定**稳定语义接口**，不假定全部名称都必须新建。既有入口的 Python 签名可以沿用，但参数与结果必须满足 §13.2 和 §14.5 的明确结构契约；允许用同一实现类/函数承载多个逻辑职责，不允许以“编码阶段再定”为由遗漏必需字段、错误和边界。

Outdated version mentions (may include historical descriptions):
- none

### Collector_软件设计_v1_19.md

Unresolved section references (heuristic, verify manually):
- L549: §8.1 — 参照《软件系统建模图形与表达规范.md》第 5.1 节 Flowchart、第 7.1 节 DFD 与第 8.1 节算法规范。业务活动图的控制流箭头表示处理顺序，DFD 箭头表示数据流，**两者不得混用**。图中应以不同形状区分处理、判断和数据。
- L3922: §12.4 —   Metrics                 §12.3、§12.4；无样本不返回伪造数值；分子/分母可合并，跨重启不虚报连续   空窗口、重启、TTL、NATS 故障

Outdated version mentions (may include historical descriptions):
- none

### Commander_软件设计_v1_29.md

Unresolved section references (heuristic, verify manually):
- L549: §8.1 — 参照《软件系统建模图形与表达规范.md》第 5.1 节 Flowchart、第 7.1 节 DFD 与第 8.1 节算法规范。业务活动图的控制流箭头表示处理顺序，DFD 箭头表示数据流，**两者不得混用**。图中应以不同形状区分处理、判断和数据。
- L2817: §12.2.1 — / UR-06 / UC-02 请求查询 / 只读 GetRequestResult、状态使用 Contracts v1_19 第 12.2.1 节固定值域 / 未开始/执行中/全部成功/全部未发送/全失败/部分/UNKNOWN 的穷举矩阵；request_i
- L2822: §12.2.1 — **状态约束：**请求级聚合状态和 Commander 单项控制状态不是同一个枚举。前者严格使用 Contracts v1_19 第 12.2.1 节规定的 `ACCEPTED/EXECUTING/SUCCEEDED/FAILED/NOT_SENT/PARTI

Outdated version mentions (may include historical descriptions):
- none

### industrial_gateway_软件设计_v1_26.md

Unresolved section references (heuristic, verify manually):
- L550: §8.1 — 参照《软件系统建模图形与表达规范.md》第 5.1 节 Flowchart、第 7.1 节 DFD 与第 8.1 节算法规范。业务活动图的控制流箭头表示处理顺序，DFD 箭头表示数据流，**两者不得混用**。图中应以不同形状区分处理、判断和数据。

Outdated version mentions (may include historical descriptions):
- none

### Data_Generator_软件设计_v1_22.md

Unresolved section references (heuristic, verify manually):
- L549: §8.1 — 参照《软件系统建模图形与表达规范.md》第 5.1 节 Flowchart、第 7.1 节 DFD 与第 8.1 节算法规范。业务活动图的控制流箭头表示处理顺序，DFD 箭头表示数据流，**两者不得混用**。图中应以不同形状区分处理、判断和数据。
- L2292: §14.2 — 本章测试物理目录按照《软件设计模板_v1_1》§14.2 调整为模块优先、测试层级次之；层级 Marker、专题 Marker、场景 Marker 的含义与验收覆盖要求保持不变。

Outdated version mentions (may include historical descriptions):
- none

## 证据分级与局限

本文件是机械扫描结果，不是设计矛盾最终裁定。相同章节号可能指向另一文档，历史版本仅构成待复核提醒；只有结合上下文及正式源码才能认定冲突。Proto、Python API、配置路径、RPC 调用链必须进行第二阶段独立比对；本阶段未声称那些检查完成。
