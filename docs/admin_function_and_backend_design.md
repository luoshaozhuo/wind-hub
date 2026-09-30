# Wind Hub Admin 功能与后端行为设计

> 文档类型：功能与交互设计
> 状态：设计基线  
> 适用范围：`src/wind-hub-admin` 管理端及后续 Admin Backend  
> 目标：固定各页面功能、后端响应、配置变更影响和运行时后果，避免后端实现阶段重新解释前端语义。  
> 原则：本文描述“应该如何工作”，不等同于当前 mock 的实际实现。

---

## 0. 文档约束

### 0.1 系统边界

Wind Hub 分为三层：

1. **wind-hub service**：`src/wind_hub/`，负责设备通信、采集任务、Sink、配置加载与运行时重配置。
2. **Admin Backend**：待实现。负责管理端 API、配置持久化、变更预览、权限/并发控制、调用 wind-hub service 完成运行时操作。
3. **wind-hub-admin**：`src/wind-hub-admin/`，只负责展示、编辑、发起命令和呈现后端结果。

Admin Backend 不得把“前端当前 mock 的实现方式”当作业务规则。后续实现以本文规定的语义为准。

### 0.2 配置变更基本原则

所有可能改变设备、点表、任务、通信参数或运行状态的操作统一遵循：

```text
读取当前版本
→ 校验请求
→ 计算 Change Impact
→ 返回 Preview
→ 用户确认
→ 再次校验配置版本
→ 停止必要的受影响任务
→ 原子修改持久化配置
→ 调用 wind-hub reload / runtime operation
→ 校验结果
→ 恢复原先 RUNNING 且仍有效的任务
→ 返回 Apply Result
```

禁止：

- 前端自行推断生产环境影响范围；
- 未经确认执行破坏性级联修改；
- 因局部配置变化停止全部无关任务；
- 自动把引用对象静默迁移到 default 占位对象；
- 配置应用失败后仍返回成功；
- 把“配置已保存”和“运行时已生效”混为一个状态。

### 0.3 统一变更状态

后端对配置修改至少区分：

| 状态 | 含义 |
|---|---|
| VALIDATED | 请求语法与引用关系合法，但尚未保存 |
| PENDING_APPLY | 已保存为待应用配置，但运行时仍使用旧配置 |
| APPLYING | 正在停止相关任务、重载和恢复 |
| APPLIED | 持久配置与运行时均已成功切换 |
| PARTIAL_FAILED | 部分动作成功、部分失败，必须给出具体对象状态 |
| FAILED | 变更未生效，返回可诊断错误 |
| CONFLICT | 基于的配置版本已经变化，需要重新 Preview |

### 0.4 Change Impact

所有高影响变更应支持 Preview。建议统一返回：

```text
change_id
base_revision
summary
affected:
  point_tables
  point_groups
  device_models
  device_groups
  devices
  tasks
  running_task_instances
actions:
  stop_tasks
  rebuild_connections
  reinject_points
  invalidate_tasks
  restart_tasks
destructive_actions
warnings
blocking_errors
```

Preview 必须基于当前服务端配置计算，不接受前端提交“受影响对象列表”作为事实依据。

### 0.5 Apply 的并发规则

Apply 必须携带 Preview 时的 `base_revision` 或等价版本号。

若 Preview 后配置已经变化：

- 不执行旧 Change Plan；
- 返回 `CONFLICT`；
- 前端要求重新 Preview。

### 0.6 管理端交互容器设计

全站统一采用以下交互层级：

| 容器 | 用途 |
|---|---|
| Page | 核心工作区、持续操作、YAML/Debug/Quality 等完整页面 |
| Drawer | 已有复杂实体详情、复杂创建/编辑、多 Tab、主从结构、需要较大纵向空间的编辑器 |
| Dialog | 短事务、Change Impact、Delete/危险确认、少量字段输入 |
| Dropdown | 页面级次要动作与低频动作 |
| MessageBox | 内容很少的单步确认 |
| Inline Edit | 高频、低风险、字段少的局部修改 |

具体规则：

- Dialog 若需要自身长距离纵向滚动，必须改为 Drawer 或 Page；
- Drawer 在桌面端约 70%–86% 宽，平板约 85%–92%，手机 100%；
- 不允许 Drawer 内再打开复杂 Drawer；需要切换复杂管理器时关闭/切换当前工作区；
- Change Impact 与危险确认始终使用 Dialog/MessageBox；
- 页面头部只保留一个主操作，其他页面级动作进入 Actions Dropdown；
- 实体列表统一以名称/稳定 ID 作为详情与编辑入口，不提供独立 Edit / View Details 按钮；
- 点击名称进入 Drawer 后，参数直接可编辑；Overview 与 Modify 不重复维护两套字段；
- 打开 Drawer 时记录 original snapshot；仅当当前值与 snapshot 不同时 Save 才启用；
- 用户将字段改回原值后 Save 自动恢复 disabled；
- 保存成功后刷新 snapshot，Save 再次 disabled；
- 不提供普通表单 Reset；dirty Drawer 关闭时提示是否丢弃未保存修改；
- “Reset to Parent / Reset Device Overrides”属于真实配置语义，不属于表单 Reset，继续保留；
- 继承/只读属性使用 Descriptions/Text 展示，不使用大量 disabled Input 伪装成可编辑字段。

当前映射：

- Device Detail：Drawer；
- Add Device Single/Batch：Drawer；
- Manage Device Metadata：Drawer；
- Point Table/Group Manage：Drawer；
- Point Add/Edit + Connectivity Test：Drawer；
- Sink Detail / Add / Edit / Test：Drawer；
- Task Add：短表单，保留 Dialog；Task Detail / Edit / Devices & Points / Logs：Drawer；
- Change Impact / Delete / Delete All：Dialog；
- Config YAML、Global ADS：Page/Inline；
- Quality、Diagnostics：统一归入侧栏“工程”，分别负责问题发现与原因定位。

## 0.7 任务恢复原则

配置变更前记录受影响 Task Instance 的原运行状态：

- 原来 `RUNNING` 且修改后仍有效：应用成功后恢复 `RUNNING`；
- 原来 `STOPPED`：修改后仍保持 `STOPPED`；
- 修改后失效：保持 `STOPPED/INVALID`，返回失效原因；
- 某个实例恢复失败：不得影响无关实例，但整体 Apply Result 应为 `PARTIAL_FAILED`。

---

## 1. 通用 API 响应规范

### 1.1 查询

查询成功返回当前服务端事实，不依赖浏览器缓存。

至少包含：

- 数据；
- `revision` 或配置版本；
- `updated_at`；
- 必要的运行时状态时间戳。

### 1.2 命令

运行控制、设备验证、调试读取等命令必须返回明确状态：

```text
request_id
state: RUNNING | SUCCEEDED | FAILED
started_at
finished_at
result
errors
```

耗时操作不得通过 HTTP 请求长时间无状态等待。前端应能查询进度或接收事件更新。

### 1.3 错误

错误至少区分：

- 参数错误；
- 引用不存在；
- 配置冲突；
- 对象被引用导致禁止删除；
- 设备离线；
- 网络失败；
- 协议失败；
- 数据读取失败；
- 写入失败；
- reload 失败；
- 权限不足；
- 并发冲突。

错误必须包含可展示信息和机器可识别的错误码。

---

# 2. Overview 页面

Overview 是**采集系统运维总览**，不是业务功率看板。页面默认只读，不允许通过卡片点击暗中修改配置。活动告警区域必须限制直接展示条数，默认最多 5 条；总数继续显示，完整告警由后续告警/日志能力查询，避免 Overview 随告警数量无限增高。

## 2.1 Runtime

展示 Wind Hub 服务运行状态。

后端应提供：

- service state；
- service uptime；
- last restart；
- collector version；
- admin backend version（若独立）；
- 当前配置 revision；
- 最近一次 reload 时间与结果。

### 正确语义

`service uptime` 从 Wind Hub 服务最近一次成功启动开始计算，不是主机 uptime。

### 异常

服务不可达时：

- Runtime 显示 unavailable；
- 其他依赖实时 API 的区域显示 stale/unavailable；
- 不使用上一次缓存结果冒充当前正常状态。

## 2.2 Devices

展示：

- enabled；
- online；
- offline；
- disabled；
- 按 type/model/protocol 的汇总。

设备 online 状态必须来自当前通信/健康状态，不得仅由 `enabled=true` 推断。

## 2.3 Acquisition

展示采集运行概况：

- 当前活跃 Task Instance；
- 实际采样/订阅状态；
- 当前采集速率；
- 最近采集成功时间；
- overrun/missed 等。

没有运行任务时必须显示真实的 idle/0，而不是错误状态。

## 2.4 Tasks

展示：

- task definition 总数；
- enabled/disabled；
- running/stopped/invalid；
- instance 级运行状态。

Definition 与 Instance 必须分开统计，不能把一个 device-group task 当成一个运行实例。

## 2.5 Sinks

展示：

- enabled sink；
- connected/healthy；
- queue depth；
- recent write errors；
- dropped points。

Sink 配置 enabled 不等于运行健康。

## 2.6 Host / Process

后端应读取真实进程/主机指标：

- CPU；
- memory；
- disk；
- process uptime；
- PID；
- 可选文件描述符/线程等。

无法取得的指标返回 unavailable，不造默认值。

## 2.7 Protocols

按 ADS / Modbus / IEC104 汇总：

- configured devices；
- connected；
- failed；
- reconnecting；
- active acquisition。

## 2.8 Configuration

展示：

- 当前配置路径/配置集；
- revision；
- applied_at；
- reload 状态；
- pending changes。

只要存在保存但未 Apply 的配置，应明确显示 `Pending Apply`。

## 2.9 Data Freshness / Timeliness

Freshness 必须按**相对于任务周期的比例**计算，而不是固定秒数评价所有任务。

建议：

```text
age_ratio = time_since_last_success / expected_interval
```

对订阅任务采用独立超时策略。

后端应返回分类结果及阈值定义，前端不自行判断。

## 2.10 Communication

用于宏观通信状态：

- online；
- degraded；
- interrupted；
- reconnecting。

应基于设备通信状态聚合。

## 2.11 Recent Events

返回最近运行事件：

- task start/stop；
- reconnect；
- reload；
- verification；
- sink failure/recovery；
- configuration apply。

事件应带时间、对象、severity、message。

## 2.12 Active Alerts

只显示当前仍活动的问题。

恢复后：

- 从 Active Alerts 移除；
- 保留在 Event/Log 历史中。

---

# 3. Devices 页面

## 3.1 设备列表

支持搜索、type/model/status/group 过滤。

后端查询结果应包含：

- device_id；
- model；
- device_type；
- device_group；
- protocol；
- host/endpoint；
- enabled；
- runtime communication state；
- verification state；
- last_update。

大量设备应支持分页或服务端过滤。

## 3.2 Add Device

Device Connection 采用：

```text
Device Model.connection_defaults
+ Device connection overrides
= Effective Device Connection
```

Device 只保存与 Model 默认值不同的连接参数。以下属于设备身份，不参与 Reset：

- `device_id`；
- `host` / Remote IP；
- ADS `target_net_id`；
- `device_group`。

ADS `local_ip`、`local_ams_net_id` 和 route repair 是 Global ADS Settings，全局唯一，不属于 Model 或 Device。

### Batch Add

Add Device 同时支持 Single / Batch。Batch 使用受限模板：

- `{num}`；
- `{num:03}`；
- `{num+100}`；
- `{num+100:03}`。

例如 `wtg-{num:03}`、`192.168.151.{num}`、`192.168.151.{num}.1.1`。Exclude 支持 `5,17,30-32`。

必须先 Preview 并检查 Device ID、Host、AMS Net ID 重复或格式错误；任一生成行失败时整批禁止 Create。正式后端应以事务方式创建整批 Device。

### 校验

必须检查：

- device_id 唯一；
- model 存在；
- device_group 存在且与 model.device_type 兼容；
- endpoint 合法；
- 协议扩展参数合法；
- model 的 point_table 存在且协议一致；
- default placeholder point table 不得作为可运行设备的最终配置。

### Preview

普通新增一般无需停止现有任务，但应提示：

- 新设备是否会自动进入某个现有 device_group task；
- 若进入，该 Task Definition 当前是否 RUNNING；
- 新实例创建后的初始行为。

### Apply

推荐规则：

- 保存设备；
- reload；
- 建立协议对象/连接；
- 展开相关 Task Instance；
- **新出现的 Task Instance 默认 STOPPED**，除非产品明确决定“跟随已运行 group task 自动启动”。

该策略必须全系统一致，不能一处自动启动、一处不启动。

## 3.3 Edit Device Config

Device Detail 使用 Drawer。点击 Device ID 即进入详情/编辑上下文；Config 直接可编辑，不再提供独立 Edit 按钮。继承字段仍以只读信息展示。

View 明确分为：

- Device Identity：Device ID、Group、Host、Target AMS Net ID；
- Model Binding：Model、Type、Manufacturer、Hardware Model、Protocol、Point Table、Read Mode；
- Effective Connection：Model Default 与 Device Override 合并后的最终参数，并标识 Inherited / Override。

点击 Edit 后仅允许编辑 Device 级字段：Model Binding、Group、Host/Remote IP、Target AMS Net ID，以及允许 override 的连接参数。Protocol、Point Table、Read Mode、Manufacturer、Hardware Model 属于 Device Model，应在 Manage Metadata 中修改。

设备字段按影响分级。

### 低影响字段

例如 device_group 改变：

- Preview 受影响 group tasks；
- 停止仅受影响实例；
- 修改；
- 重新展开实例；
- 原 RUNNING 且仍存在的实例恢复。

### 通信字段

例如 host、port、AMS Net ID、unit_id 等：

- Preview 当前设备相关运行实例；
- 确认后停止这些实例；
- 关闭旧连接；
- 更新配置；
- 建立新连接；
- 恢复原 RUNNING 实例。

连接失败：

- 配置是否回滚必须采用统一策略；
- 推荐配置 Apply 为事务：无法建立必要运行环境时返回失败并保持旧已应用版本；
- 若采用“配置提交但运行失败”策略，则必须返回 PARTIAL_FAILED，并明确当前 runtime/config 不一致，不能返回成功。

## 3.4 Enable / Disable Device

Disable：

- 停止该设备所有运行 Task Instance；
- 从 group task 的有效实例集合中移除；
- 关闭或保留协议连接由运行时策略决定，但必须统一；
- 返回实际停止的实例。

Enable：

- 设备重新进入有效设备集合；
- 建立连接；
- 新实例默认 STOPPED；
- 不因为 enabled 切换静默启动采集。

## 3.5 Delete Device

必须先 Preview。

若有运行实例：

- 提示将停止哪些实例。

若 Task Definition 指向单设备：

- 删除设备后该 Task 应变为 INVALID，而不是删除 Task Definition。

若 group task：

- 仅移除对应实例，其他设备实例不受影响。

确认后：

- 停止相关实例；
- 删除设备；
- 关闭连接；
- reload；
- 返回受影响 Task/Instance。

### Delete All Devices

Devices 页面提供 `Delete All Devices`：

- 展示 Devices、Task Definitions、RUNNING Tasks 等影响数量；
- 要求输入 `DELETE ALL` 二次确认；
- 停止全部 Device 相关 Task Instance；
- 删除全部 Device 和 Device Verification；
- 保留全部 Task Definition，并重新校验；
- 单设备 Task / 无目标 Group Task 变为 INVALID；
- 保留 Device Model、Type、Group、Point Table、Point Group、Global ADS Settings；
- 不自动删除或迁移 Meta。

正式后端必须按事务性批量变更处理。

## 3.6 Verify Device

Device Config 中 Connectivity 的状态与事实信息分离：状态列仅显示 Passed / Failed / Warning 并使用状态色；IP、协议端点、点数、latency 等 detail 使用普通正文色，禁止整行绿色粗体。

验证顺序：

```text
Network
→ Protocol
→ Point Read
```

后端必须真实执行，不以在线标志模拟。

结果至少包括：

- state；
- network；
- protocol；
- point_total；
- point_success；
- point_failed；
- latency；
- errors；
- verified_at。

某点失败不得把具体错误折叠成简单 false。

## 3.7 Verify All

必须：

- 防重复触发；
- 返回异步 operation_id；
- 一次操作内记录目标设备快照；
- 最终批量更新结果；
- 单设备失败不终止其他设备验证。

## 3.8 Data

Data 是当前设备 resolved points 的当前值观察页。必须提供手动 Refresh、固定刷新频率和 Auto Refresh 开关。

- Refresh 执行一次设备当前点读取，刷新过程中按钮只 disabled，不插入 loading icon、不修改按钮文本，避免 Auto Refresh 周期触发布局抖动；
- Auto Refresh 仅在 Device Drawer 的 Data Tab 激活时运行；
- 切离 Data、关闭 Drawer、组件卸载时必须停止 timer；
- 建议频率：500 ms / 1 s / 2 s / 5 s / 10 s；
- 页面显示 Last Refreshed、成功点数和失败点数；
- 单点失败必须显示错误，不得继续把旧值伪装为最新成功值。

## 3.8.1 Read Test

Device Drawer 在 Config 与 Data 之间提供 Read Test。它用于快速验证**已经配置好的单个 Point**，不是 Manual Read：

- Point 只能从当前设备 resolved Point Table 中选择；
- 前端不得允许修改 symbol/address/data_type；
- 展示 Point ID、variable/address、data_type、scale/offset、unit、groups、description；
- 返回 timestamp、latency、raw data、decoded value、engineering value；
- 失败时保留 error category、protocol/error code、message；
- 切换 Point 清空上一次结果。

正式 API 建议只接收 `point_id`，由后端根据 Device → Model → Point Table 解析真实协议地址，防止绕过配置。

## 3.9 Trend

Trend 的范围选择表示“最近多长时间”，不使用含义模糊的 Real-time。统一为 1 min / 5 min / 15 min / 1 h；Auto Update 固定每 1 s 刷新一次滑动窗口，Time Window 只控制窗口长度。Refresh 执行一次历史窗口刷新。顶部布局采用“摘要 + 主操作 / 视图控制 / 图表 Legend”分层，避免把所有控件挤在一行；Raw Recording 的说明放 Tooltip，不长期占据主区域。

后端提供时序数据查询。

要求：

- 多 Point；
- time range；
- timestamps；
- unit；
- gaps 不得用虚构插值掩盖；
- 采集缺失应呈现为缺口。

## 3.10 Control / Send Command

选择 Command Point 后，必须先展示该 Point 的定义信息：point_id、description、variable/address、data_type、scale/offset、unit、groups、current value、last updated。控制候选仅来自明确的 control Point Group。

写命令是高风险操作。

请求必须包含：

- device；
- point；
- requested engineering value；
- request_id。

后端必须：

1. 校验设备与 point；
2. 校验 point 可写；
3. 按 data_type/scale/offset 正确转换；
4. 执行协议写；
5. 返回协议级结果；
6. 可选执行 readback。

禁止前端仅凭点存在就允许写入。

---

## 3.11 Trend Recording

Device Trend 提供录波导出功能。导出范围必须与当前 Trend 时间窗一致，信号范围为当前选中的 Trend Signals。

关键约束：**导出必须读取并保存该时间段的全量原始采样记录，不得使用 ECharts 当前渲染数据、抽样数据或像素反推数据。** 图表为性能允许抽样/降采样，但录波查询与导出必须独立请求 raw samples，并保留 timestamp、device_id、point_id、value、unit 等字段。

---

# 4. Device Metadata Manager

包括 Device Model、Device Type、Device Group。

## 4.1 Device Model — Create

校验：

- model ID 唯一；
- device_type 存在；
- protocol 合法；
- point_table 存在且 protocol 一致；
- connection_defaults 符合协议。

创建 Model 本身不影响运行设备时无需停止任务。

## 4.2 Device Model — Update Point Table

这是运行相关变更。

Preview 必须列出：

- 使用该 Model 的 devices；
- 相关 Task Instance；
- RUNNING instances；
- 新旧 point table；
- 新表是否满足相关 point_group。

Apply：

- 停止受影响 RUNNING instances；
- 更新 Model；
- 重新解析 DeviceConfig；
- 重注入点映射；
- 校验 task；
- 恢复仍有效且原为 RUNNING 的 instances。

## 4.3 Device Model — Update Protocol

高风险。

Preview 至少包括：

- 所有关联 devices；
- 所有运行 instances；
- point table compatibility；
- connection defaults 将变化；
- protocol connections 将重建。

Apply：

- 停止全部关联实例；
- 校验并切换 point table；
- 更新协议与参数；
- 关闭旧协议连接；
- 建立新协议连接；
- 校验；
- 恢复合法的原 RUNNING 实例。

不能自动猜测不兼容 Point Table。

## 4.4 Device Model — Update Connection Defaults

影响所有未被设备实例 override 的相关字段。

Preview 必须计算**实际受影响设备**，不能简单认为全部 Model 设备都受影响。

对实际连接参数变化的设备：

- 停实例；
- rebuild connection；
- 恢复。

纯展示属性变化无需触碰运行时。

## 4.5 Reset Device Overrides

Device Model 编辑页提供 `Reset Device Overrides`。其语义是删除关联 Device 对 `connection_defaults` 的 override，使 Effective Connection 回落到当前 Model 默认值。

必须保留 Device ID、Host / Remote IP、ADS Target AMS Net ID、Device Group。操作前 Preview 受影响设备、override 数和 RUNNING Task；Apply 时只停止必要实例，清除 override 后恢复原 RUNNING 且仍有效的实例。

## 4.6 Device Model — Delete

若仍有 Device 引用：**禁止删除**。

返回：

- blocking error；
- referenced device list/count。

后续如增加迁移功能，应采用显式 `Reassign and Delete`，用户指定目标 Model。

## 4.7 Device Type — Update

当前 Type 主要是分类元数据。

改显示名：

- 不影响运行；
- 无需停止任务。

若未来允许改 ID：

- 必须原子迁移所有 Model/Group 引用；
- 不允许只改 key 留下悬挂引用。

## 4.8 Device Type — Delete

存在 Model 或 Device Group 引用时禁止删除。

## 4.9 Device Group — Update Device Type

若 Group 中已有 Device：

- 必须验证每台 Device 的 Model.device_type 与新 type 兼容；
- 不兼容则阻止。

若关联 group Task：

- Preview 任务影响；
- 必要时停止受影响实例；
- 修改后重新展开。

## 4.10 Device Group — Delete

存在 Device 或 Task Definition 引用时禁止直接删除。

禁止静默将设备移到其他 Group。

---

# 5. Points 页面

后端配置语义应以真实 Point Table 继承模型为准：

```text
Resolved Child
= Resolved Parent
- remove_points
+ overrides
+ local points
```

前端不应把“继承”实现成一次性复制。

## 5.1 Point Table 列表

Point 列表以 `point_id` 作为详情与编辑入口；点击 Point ID 打开 Point Drawer。行内不提供 Edit 按钮，仅保留 Reset Override、Delete 等语义不同的动作。

查询应同时返回：

- raw metadata：protocol / extends；
- local points 数；
- inherited points 数；
- overrides 数；
- effective/resolved points 数；
- child tables；
- referenced models；
- referenced devices；
- affected tasks。

## 5.2 Create Base Point Table

要求：

- ID 唯一；
- protocol 必填；
- 初始 points 可为空；
- 不影响现有运行对象时直接创建。

## 5.3 Create Child Point Table

要求：

- parent 存在；
- 不允许继承环；
- protocol 必须与 parent 一致；
- 子表 raw 配置只保存 overrides/local/remove_points；
- 不复制父表 point 数据。

创建完成后后端返回 resolved preview。

## 5.4 Rename Point Table

若允许改 ID，必须先 Preview。

影响：

- child extends 引用；
- Device Model.point_table 引用；
- 页面当前选择；
- 相关设备和任务。

Apply 必须原子迁移所有引用。

若实现成本较高，也可以像 Model ID 一样规定“创建后 ID 不可修改”。二者只能选一种，不应部分支持。

## 5.5 Change Base Table Protocol

高风险。

若存在 Child Table：

- Child 不允许跨协议继承；
- 后端不得简单把 `extends` 静默清掉。

建议规则：

- 有 Child 或 Model 引用时禁止直接修改 protocol；
- 要求用户先解除引用/迁移；
- 或提供独立的 Migration Plan。

## 5.6 Change Child Parent

Preview：

- 原 parent；
- 新 parent；
- inherited point changes；
- overrides 是否仍可应用；
- remove_points 是否仍有效；
- 受影响 devices/tasks。

若某 override/remove_points 对新 parent 无效：

- 阻止 Apply；
- 明确列出冲突 point_id。

Apply 时重新 resolve，不物理复制父表点。

## 5.7 Edit Base Point

修改 Base Point 会级联影响所有后代 resolved table。

Preview 必须递归计算：

- descendant tables；
- effective point 变化；
- models；
- devices；
- running tasks。

Apply：

- 停止必要的受影响 RUNNING instances；
- 修改 base raw point；
- 重新 resolve 全继承链；
- 保留 Child 的 overrides / local points / remove_points；
- 校验；
- 更新运行时；
- 恢复原 RUNNING 且仍有效的 instances。

## 5.8 Edit Inherited Point in Child

不得直接修改 inherited 实体。

正确动作：

- 在 Child raw table 创建/更新同 point_id 的 override patch；
- resolved value 由 resolver 计算。

UI 应显示为 Override。

## 5.9 Reset Child Override

删除 Child 的 override patch：

- 不删除 point；
- point 恢复为 parent 当前定义；
- Preview 对相关设备/tasks 的影响。

## 5.10 Add Local Point to Child

只写入 Child local points。

必须满足完整 Point 定义，并通过协议地址校验。

## 5.11 Delete Point

### Base/Local Point

删除本地定义前 Preview descendants 和运行影响。

### Inherited Point in Child

不得删除 parent 点。

正确动作是：

- 将 point_id 加入 Child `remove_points`。

### Override Point

若“Delete”含义是彻底从 Child effective table 删除：

- 删除 override；
- 同时加入 `remove_points`。

若只想恢复父表，应提供 `Reset Override`，二者语义不得混淆。

## 5.12 Point Add/Edit Connectivity Test

测试使用**当前未保存 draft**。

后端根据当前 Point Table protocol，仅允许选择兼容设备。

测试流程：

1. 校验 draft address；
2. 校验设备 protocol；
3. 建立/复用协议连接；
4. 单点读取；
5. 返回原始读取结果的多 data-type interpretation。

当前 UI 只要求展示固定的候选解释表，不要求展示 Raw 和 Configured 区块。

失败必须区分：

- device offline；
- protocol connect error；
- symbol/address invalid；
- timeout；
- remote protocol error；
- decode error。

测试不得修改 Point 配置。

## 5.13 Delete Point Table

推荐强引用策略：

- 有 Child Table 引用：禁止；
- 有 Device Model 引用：禁止；
- Device 实例数量为 0 不代表 Point Table 可删除，因为 Device Model 和 Table inheritance 仍是独立强引用；
- UI 在删除前即展示 blocker：Delete disabled，并列出具体 Child Table / Device Model 名称。

返回完整 blocking references。

禁止自动：

- 将 Model 迁移到 default table；
- 清空 Child extends。

若以后提供迁移删除，必须是显式 Migration Plan。

## 5.14 Point Group — Create/Update

Rename ID 若允许：

- 原子迁移所有 Point.point_groups；
- 原子迁移 Task.point_group；
- Preview 受影响对象。

若仅改 display name，则不影响运行。

## 5.15 Point Group — Delete

存在 Task 引用时：

- 推荐禁止直接删除。

存在 Point 引用但无 Task：

- 也不应默默给 point 分配 default；
- 应要求用户先移除/替换引用，或提供显式迁移。

Default Group 仅是占位，不是正常自动迁移目的地。

---

# 6. Tasks 页面

## 6.1 Query Tasks

Task 列表不提供独立 Edit 或 View Details 按钮。点击 Task ID 打开 Task Detail Drawer；Summary 同时承载可编辑 Definition 与只读 Runtime，另外保留 Devices & Points、Logs。普通编辑不提供 Reset。

后端返回：

- definition；
- valid/invalid；
- invalid_reason；
- instance count；
- running/stopped instance count；
- last operation/error。

## 6.2 Create Task

校验：

- task_id 唯一；
- device 与 device_group **必须且只能有一个**；
- target 存在；
- point_group 存在且不是 default placeholder；
- interval 合法；
- sinks 至少一个且存在；
- 目标设备使用有效 Point Table；
- point_group 在目标 resolved points 中至少存在可采点。

创建后 Task Instance 默认 STOPPED，并由后端写入 `created_at` 与 `updated_at`。修改 Task Definition 只更新 `updated_at`；Start/Stop、运行错误等 Runtime 变化不得修改这两个 Definition 时间字段。

## 6.2.1 Task Detail

Task Detail 不再拆分 Overview 与 Config。二者合并为 **Summary** 工作区：

- 左侧 Definition：Task 配置直接可编辑；只有 Definition 发生变化时 Save 才启用；不提供普通 Reset。
- 右侧 Runtime：当前状态、Instance 数、Point Binding 数、Target、Point Group、Sinks；
- Devices & Points：目标 Device 列表，以及选择单台 Device 后该 Task 实际采集的 resolved Point 列表；
- Logs：该 Task/Instance 的最近运行日志；默认 Latest 20，可切换 Latest 50 / 100，完整检索仍由 Logs 页面负责。

这样避免“Overview 展示一遍、Config 再展示一遍”的重复。

Device Group Task 不应一次展开所有点造成长页面。桌面端采用“左侧 Device 列表 + 右侧所选 Device Points”主从布局；移动端改为纵向布局。

## 6.3 Edit Task

若 Task 当前有 RUNNING instances：

### 仅 targets/sinks 变化

若运行时支持无中断切换，可不停止 acquisition；否则按实现停相关实例。

### interval / point_group / target 变化

必须：

- Preview；
- 记录原运行状态；
- 停止受影响实例；
- 更新；
- 重新展开；
- 恢复仍存在且有效的原 RUNNING 实例。

### enabled=false

立即停止全部实例。

## 6.4 Start Task

Start 是运行控制，不修改 definition。

后端：

- 重新校验 task；
- 展开所有目标实例；
- 对可启动实例执行 start；
- 返回每个 instance 的结果。

部分设备失败：

- Task 操作结果为 PARTIAL_FAILED；
- 成功实例可以继续运行；
- 返回失败设备原因。

## 6.5 Stop Task

停止该 Task 的全部运行实例。

必须幂等：

- 已 STOPPED 再 Stop 仍返回成功状态；
- 不影响其他 Task。

## 6.6 Start/Stop Single Instance

后端应保留实例级控制接口，便于设备诊断和局部恢复。

## 6.7 Delete Task

若正在运行：

- Confirm 明确将停止多少实例；
- 先停止；
- 再删除 definition。

删除失败时不得留下“definition 已删但 handle 仍运行”的孤儿状态。

---

# 7. Sinks 页面

Sinks 页面负责输出端配置、运行状态、健康检查和独立写入测试。当前正式 Sink 类型与 wind-hub service 保持一致：Kafka、PostgreSQL、File。

## 7.1 列表与状态

页面至少展示：

- name / type / enabled；
- endpoint 摘要；
- runtime health；
- verification summary（Never / PASS x/y / FAIL x/y）与 last verified；
- referencing tasks；
- queue depth；
- last test / last write；
- write failures / dropped points。

`enabled=true` 不等于 healthy。健康状态必须来自 Sink runtime 的 `health()`、写入结果及队列指标。

## 7.2 Create / Edit

Add Sink 和复杂编辑使用 Drawer。已有 Sink 的 Summary 将参数编辑与 Runtime 合并在一个工作区，参数修改后才启用 Save；不再拆分 Overview / Config，也不提供普通 Reset。**Create 模式不使用 Tabs**，Drawer 打开后直接显示 Name、Type 和对应参数表单；已有 Sink 点击 name 后仅使用 Summary / Test：Summary 合并参数编辑与 Runtime，Test 展示 Verification stages 与 Write Test。

Sink name 创建后固定。Type 创建后也固定；如需跨类型迁移，应新建 Sink 后迁移 Task 引用。

参数必须按真实 Sink Driver 区分：

- Kafka：`bootstrap_servers`、`topic`、`key_field`、`compression_type`、`acks`、`retries`、`batch_size`、`linger_ms`；
- PostgreSQL：`dsn`、`table`、`batch_size`、`create_table`、`pool_min_size`、`pool_max_size`、可选 schema；
- File：`path`、`format`、rotation、compression、buffer/flush 参数。

密码/DSN 在只读展示时必须脱敏。

修改被 Task 引用的 Sink 前必须 Preview 受影响 Task。Apply 时关闭旧 Sink、构建并 open 新 Sink；失败不得把新配置伪装成 healthy。

## 7.3 Enable / Disable

Disable 被 Task 引用的 Sink 时必须确认。

正式规则：

- Sink 变为 disabled；
- 引用 disabled Sink 的 Task Definition 保留；
- Task 变为 INVALID；
- 已运行的相关 Task Instance 停止；
- 重新 Enable 后 Task 重新校验，但不得自动启动原本 STOPPED 的实例。

## 7.4 Delete

存在 Task 引用时禁止删除 Sink。不得自动从 Task targets 中移除。

## 7.5 Connection Test

Connection Test 不发送业务数据：

- Kafka：创建 producer / broker connection，验证 bootstrap/topic 相关连接能力后关闭；
- PostgreSQL：建立连接池/连接并执行轻量连接验证后关闭；
- File：验证目录创建权限、文件可打开/追加能力，不污染正式业务内容。

返回：state、latency、stage、错误类型、原始错误摘要、tested_at。

## 7.6 Write Test

Write Test 会向**真实目标**发送一条合成 `PointValue`，属于有副作用的测试，必须在执行前确认。

固定测试数据应显式标记为测试来源，例如：

```text
device_id = sink-test-device
point_id  = sink_test
value     = 1.0
quality   = GOOD
source    = admin_sink_test
```

后端应执行完整 `open → write → flush → close` 并返回写入结果。Kafka 需要等待 broker ack；PostgreSQL 会产生实际表记录；File 会产生实际文件内容，因此 UI 必须明确告知副作用。

## 7.7 Verify All Sinks

只执行无业务副作用的 Connection Verification，不批量执行 Write Test。各 Sink 独立返回 stage 结果，单个失败不能中止其他 Sink。

统一结果包含：check name、state（passed/failed/skipped）、latency_ms、detail、error_code、checked_at。

检查链按层次展开：

- Kafka：DNS → ICMP（辅助）→ TCP Port → Broker Session → Metadata → Topic；
- PostgreSQL：DNS → ICMP（辅助）→ TCP Port → PostgreSQL Session → Authentication → SELECT 1；
- File：Parent Path → Permission → Open / Append capability。

ICMP 仅作为 Network Reachability 的辅助信息，失败显示 Warning，但不得阻断 TCP Port 检查；远端端口可达性必须通过真实 TCP connect 判断，不能用本机 `ss` 代替。网络类 Sink 的结果需显示解析后的 host/IP/port target。

列表必须展示 Verification 与 Last Verified；点击 Sink 后 Test 页展示完整 stages。Test 表格保持紧凑，只保留 Check、Result、Evidence、Time 四列：Check 合并 layer/name，Evidence 合并 target/detail/error，避免横向滚动。页面不保留长期占位的大型“Verify All”说明 Card；批量执行后只显示紧凑 Last Verification Summary（checked/passed/failed/warning/time）。Verify All 的说明放入 Actions 语义，不与 Write Test 混淆。

---

# 8. Quality 页面

Quality 负责发现问题、确定影响范围、展示证据，不执行主动网络或协议诊断。

## 8.1 统一交互

Quality 的 Summary、Channel、Quality Dimension、Active Issue 都打开同一个 **Quality Detail Drawer**。Drawer 不改变当前 Tab、筛选、分页和图表状态；关闭后仍停留在原页面上下文。

Drawer 统一包含 Problem Location、Evidence、Affected Objects、Recent Logs（最近 20 条）、Suggested Investigation。大量对象统一 Table + Pagination；Drawer 内禁止继续打开第二层 Drawer / Dialog。

## 8.2 Channel Quality

Check / Auto Check / Check Interval 只作用于 Channel Quality。Healthy / Degraded / Interrupted 的定义通过 Info Tooltip 解释。

Channel Summary、Delivery / Acquisition Object、Latency bucket、Channel State slice 均打开同一套 Quality Detail Drawer。

## 8.3 Data Quality

Data Quality 使用统一统计窗口 1 h / 24 h / 7 d。Summary、五个 Quality Dimension、Active Data Issues 均打开 Quality Detail Drawer。

Recent Logs 必须由后端按 issue context 关联；正式实现中前端不得从全局日志自行推断。

---

# 9. Diagnostics 页面

Diagnostics 定位为工程探索工作台。

## 9.1 Target

Desktop 使用左右分栏：左侧 Target，右侧 Explorer；Tablet / Mobile 改为上下布局。Target 支持 Defined Object（Device / Sink / Point）与 Manual Target。

左侧使用标准 Element Plus Form 垂直排列，不自行定义另一套字体、label 或控制器样式。Diagnostics 页面 CSS 只负责布局、间距与响应式；表单 Label、Tabs、Segmented、Table 等字体由全局 Typography Token 与 Element Plus 全局主题统一提供。Manual Target 只保存 Host / IP 与 Protocol；CIDR、Port Profile 等属于具体 Network Tool。

## 9.2 Explorer

Explorer 只保留 Network / Read / Write 三个一级 Tab，不再保留独立 Protocol Explorer。

Network 内部统一采用 Parameters → Run → Results，并提供：

- Reachability；
- Port Probe；
- Protocol Check：验证协议会话能否建立，不读业务点、不写数据；
- Host Discovery。

批量结果必须 Table + Pagination。ADS 的 TCP 48898 与 ADS Target Port 801 必须区分。

Read 支持 Defined Point / Manual Address。Write 独立于 Read，执行单次写、二次确认、审计和 readback。

---

# 10. Configuration

配置拆为 System Settings 与 Configuration Files。

## 10.1 System Settings

未保存变更操作统一命名为 **Discard Changes**，行为是恢复最近 Saved / Applied snapshot，不使用语义含糊的 Cancel。

## 10.2 Configuration Files

Actions 固定为：

- Import；
- Download Current File；
- Create Backup (.zip)。

Download Current File 只下载当前 YAML。Create Backup 下载包含当前全部 YAML 的 ZIP，不创建 Revision，也不写入 History。History 仅记录 Apply / Import / Restore 等配置 Revision。

Import 使用 Drawer，在一个 Drawer 内完成 Upload → Validate → Diff → Impact → Apply，不切换主页面、不出现 Back to Configuration Files、不再叠加确认 Dialog。

Files / History 使用同一页面 Tab 切换。History 不使用 Back。Restore 仍需以新 Revision 应用，不能直接覆盖当前 Revision。

样式必须复用现有 Theme Token 与 Element Plus Token，不新增页面级颜色、字体、圆角体系。

Admin 页面 Typography 必须使用 `typography.css` 中统一的应用级语义：`app-page-title`、`app-section-title`、`app-panel-title`、`app-body`、`app-caption`、`app-metric`。禁止依赖浏览器默认 `h1/h2/h3` 视觉样式；业务页面不得局部建立字号、字重、行高体系。Element Plus 的 Form Label、Tabs、Segmented、Table 等组件文字由全局 Theme 控制。

---

# 11. Logs 页面

## 11.1 Query

后端支持：

- level；
- source；
- object；
- keyword；
- time range；
- limit/cursor。

日志默认按时间倒序。

## 11.2 Log Sources

至少统一：

- runtime；
- task；
- ads；
- modbus；
- iec104；
- sink；
- config；
- admin/audit。

## 11.3 Streaming

若后续支持实时日志：

- 使用 WebSocket/SSE 或长连接；
- 支持断线重连；
- 前端重连后不得重复无限追加旧数据；
- 必须有最大缓存条数。

## 11.4 Audit

以下操作必须进入审计日志：

- config apply；
- task start/stop；
- device add/edit/delete；
- metadata change；
- write command；
- debug write。

---

# 12. Meta 数据统一响应规则

Meta 包括：

- Point Table；
- Point Group；
- Device Model；
- Device Type；
- Device Group；
- Unit。

## 12.1 Rename

稳定 ID 原则优先。

建议：

- Device ID、Task ID、Model ID、Type ID、Group ID 创建后默认不可改；
- Point Table / Point Group 若允许 rename，必须通过专门 Rename API 原子迁移引用；
- 不允许普通 Update 暗中完成 key rename。

## 12.2 Delete

默认采用**引用保护**：

```text
存在强引用
→ BLOCK
→ 返回 references
```

不要自动迁移到 default placeholder。

Default 仅用于：

- 初始化占位；
- 表示尚未完成配置；
- 防止 schema 无引用。

它不是删除对象时的“垃圾桶”。

## 12.3 Update

后端必须先判断字段属于：

- display-only；
- configuration-only；
- runtime-lightweight；
- connection-rebuild；
- destructive/migration。

根据类别生成正确 Change Plan。

---

# 13. 后端实现建议的 API 分层

具体 URL 可在实现阶段调整，但职责建议固定。

## 13.1 Query API

```text
GET /overview
GET /devices
GET /devices/{id}
GET /tasks
GET /sinks
GET /sinks/{name}
GET /points/tables
GET /metadata/...
GET /quality/...
GET /logs
GET /config/files/{name}
```

## 13.2 Command API

```text
POST /tasks/{id}/start
POST /tasks/{id}/stop
POST /devices/{id}/verify
POST /devices/verify
POST /sinks/{name}/test-connection
POST /sinks/{name}/test-write
POST /sinks/verify
POST /debug/read
POST /debug/write
POST /debug/watch
DELETE /debug/watch/{session_id}
```

## 13.3 Configuration Change API

推荐统一：

```text
POST /changes/preview
POST /changes/{change_id}/apply
```

结构化页面和 YAML 页面最终都进入同一套 Preview/Apply 管线。

这样可以确保：

- Points 改表；
- Device Model 改协议；
- Config 页面直接改 YAML；

三种入口的运行时后果一致。

---

# 14. Apply Result

配置应用完成后统一返回：

```text
change_id
previous_revision
current_revision
state
affected
stopped_instances
restarted_instances
invalid_instances
connection_rebuilds
warnings
errors
duration_ms
applied_at
```

前端必须根据实际结果刷新相关页面，不自行乐观修改为成功状态。

---

# 15. 后端开发验收基线

实现任何 Admin Backend 功能时至少检查：

- 查询返回的是服务端真实状态；
- 修改前有完整校验；
- 高影响修改有 Preview；
- Preview 能列出受影响对象；
- Apply 有 revision 并发保护；
- 只停止必要的相关任务；
- 原 STOPPED 不被误启动；
- 原 RUNNING 且仍有效的实例能够恢复；
- invalid 对象不会强行运行；
- 强引用对象不会被静默删除/迁移；
- default placeholder 不作为自动迁移目标；
- 配置 Save 与 Apply 状态明确区分；
- 部分失败有对象级错误；
- 调试写/控制写有审计；
- 所有运行命令幂等或具备 request_id 去重；
- 前端刷新后能够从后端恢复完整事实状态。

---

# 16. 当前前端原型与本设计的已知差异

当前 `wind-hub-admin` 仍为 mock 原型，但关键配置语义已按本文对齐：

1. Point Table 使用真实继承语义并采用引用保护。
2. Device Connection 使用 Model Defaults + Device Overrides。
3. Global ADS Settings 独立于 Model/Device，符合当前进程级 ADS Router 语义。
4. Device Model 支持 Reset Device Overrides，并保留设备身份字段。
5. Devices 支持 Single / Batch Add，Batch 有模板与 Preview 校验。
6. Delete All Devices 保留 Task Definition 并重新标记有效性。
7. 高影响 Meta、Point、Device、Task 修改已表达 Change Impact。
8. Sinks 页面已提供管理、Connection Test、Write Test 的 mock 交互；正式后端必须调用真实 Sink Driver。
9. Config、Debug、Verification、Quality、Logs 的真实后端行为仍待 Admin Backend 实现。

本文优先级高于 mock 数据细节。
