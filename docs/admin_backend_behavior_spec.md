# Wind Hub Admin 页面功能与后端响应规范

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

### 0.6 任务恢复原则

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

Overview 是**采集系统运维总览**，不是业务功率看板。页面默认只读，不允许通过卡片点击暗中修改配置。

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

## 3.6 Verify Device

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

展示设备点的当前/最近值。

后端需支持：

- point_id；
- variable_name；
- value；
- engineering unit；
- timestamp；
- quality/read status；
- data_type；
- address；
- scale/offset。

大量点位应支持过滤和分页/虚拟化。

## 3.9 Trend

后端提供时序数据查询。

要求：

- 多 Point；
- time range；
- timestamps；
- unit；
- gaps 不得用虚构插值掩盖；
- 采集缺失应呈现为缺口。

## 3.10 Control / Send Command

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

## 4.5 Device Model — Delete

若仍有 Device 引用：**禁止删除**。

返回：

- blocking error；
- referenced device list/count。

后续如增加迁移功能，应采用显式 `Reassign and Delete`，用户指定目标 Model。

## 4.6 Device Type — Update

当前 Type 主要是分类元数据。

改显示名：

- 不影响运行；
- 无需停止任务。

若未来允许改 ID：

- 必须原子迁移所有 Model/Group 引用；
- 不允许只改 key 留下悬挂引用。

## 4.7 Device Type — Delete

存在 Model 或 Device Group 引用时禁止删除。

## 4.8 Device Group — Update Device Type

若 Group 中已有 Device：

- 必须验证每台 Device 的 Model.device_type 与新 type 兼容；
- 不兼容则阻止。

若关联 group Task：

- Preview 任务影响；
- 必要时停止受影响实例；
- 修改后重新展开。

## 4.9 Device Group — Delete

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
- 有 Device Model 引用：禁止。

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

创建后 Task Instance 默认 STOPPED。

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

# 7. Quality 页面

Quality 表示**采集服务质量**，不是电能质量。

## 7.1 Global Metrics

后端应从运行指标聚合：

- average interval；
- jitter P95；
- active interruption；
- reconnect count；
- read timeout；
- read failure。

必须带统计窗口，例如 5 min / 24 h。

## 7.2 Task Quality

每 Task/Instance 至少提供：

- expected interval；
- actual average；
- jitter；
- missed ticks；
- interruption count；
- last success；
- success rate；
-统计窗口。

订阅任务不伪造 Expected interval，应显示 subscription 语义。

## 7.3 Device Health

至少提供：

- device；
- protocol；
- last success；
- timeout count；
- failure count；
- communication state；
- current interruption start time（若中断）。

页面只读，不通过 Quality 页面直接修改配置。

---

# 8. Debug 页面

Debug 是现场诊断工具，操作不改变正式配置，除非明确属于 Write Test。

## 8.1 Ping

后端从 Wind Hub 所在运行环境执行。

返回：

- target；
- success；
- latency；
- error。

不得由 Admin Backend 所在的另一台机器代替执行，除非部署架构明确两者网络环境完全相同。

## 8.2 TCP Connect

使用设备最终解析后的 host/port。

仅验证 TCP 建连，不等价于 protocol success。

## 8.3 Protocol Connect

按设备协议执行最小握手/会话验证。

返回协议特有错误码和可读解释。

## 8.4 Read Test

对设备执行少量已知点读取，返回：

- requested points；
- per-point result；
- latency；
- protocol error。

## 8.5 Manual Read

允许手工输入协议地址：

- ADS：symbol 优先；或 index_group + index_offset；
- Modbus：register type + 0-based address；
- IEC104：IOA / 必要类型信息。

必须要求 data_type。

返回实际 raw/decoded value、时间戳和协议错误。

Manual Read 不写入 Point Table。

## 8.6 Raw Data / Watch

Start Watch：

- 创建有生命周期的 debug session；
- 返回 session_id；
- 固定最小采样周期，避免高频压垮设备；
- 不与正式采集 Task 共用不可重入句柄。

Stop：

- 幂等关闭 session。

连接断开/页面关闭时后端必须有超时回收机制。

多类型 interpretation 必须基于同一份原始数据。

## 8.7 Write Test

高风险操作，后端必须：

- 校验设备；
- 校验 point 可写；
- 校验输入值；
- 转换工程值到协议值；
- 执行单次写；
- 返回 protocol response；
- 推荐 readback。

写入测试必须记录审计日志。

禁止提供“持续写”默认行为。

---

# 9. Config 页面

Config 是直接操作配置文件的高级入口，与 Devices/Points/Tasks 的结构化编辑互补。

## 9.1 Site Edit

Update 只修改待保存配置，不应直接假装 Runtime 已生效。

若 `site_id/name` 只用于标识：

- Save 后 PENDING_APPLY；
- Apply 后更新 Runtime/Overview。

若仅改显示名且系统允许无 reload 生效，也必须有明确统一规则。

## 9.2 YAML Query

后端返回：

- 当前 applied 内容；
- 当前 saved/pending 内容（若存在）；
- revision；
- file revision/hash。

前端 Review 应比较 **applied vs 当前编辑内容**。

## 9.3 Validate

必须使用与生产配置加载相同的 schema/resolver/交叉引用规则。

Validate 不保存、不 reload。

返回：

- syntax errors；
- schema errors；
- reference errors；
- inheritance errors；
- protocol config errors；
- warnings。

## 9.4 Save

Save：

- 持久化为 pending 配置；
- 不修改运行时；
- 状态变为 PENDING_APPLY；
- 返回新的 saved revision。

必须防止覆盖其他用户的新修改。

## 9.5 Save & Apply

流程：

```text
Validate
→ Build Diff
→ Build Impact
→ Confirm（若高影响）
→ Persist
→ Apply
→ Result
```

不能只是“写文件 + 返回成功”。

## 9.6 Review

Review 本身由前端可视化，但 diff 基线由后端 revision 提供。

对于结构化配置，后端还应提供 object-level diff，例如：

- devices added/removed/updated；
- tasks added/removed/updated；
- tables changed；
- models changed。

## 9.7 Upload

Upload 不得立即覆盖正式配置。

流程：

1. upload temporary file；
2. validate；
3. compare；
4. 返回 diff + impact；
5. 用户 Save 或 Save & Apply。

### Save

覆盖 pending version，不影响运行时。

### Save & Apply

按统一 Apply 事务执行。

## 9.8 Apply Failure

Apply 失败时必须明确：

- 文件是否已经保存；
- runtime 是否仍使用旧 revision；
- 哪一步失败；
- 是否需要人工修复。

不允许只显示 `reload failed` 而缺少当前有效版本。

---

# 10. Logs 页面

## 10.1 Query

后端支持：

- level；
- source；
- object；
- keyword；
- time range；
- limit/cursor。

日志默认按时间倒序。

## 10.2 Log Sources

至少统一：

- runtime；
- task；
- ads；
- modbus；
- iec104；
- sink；
- config；
- admin/audit。

## 10.3 Streaming

若后续支持实时日志：

- 使用 WebSocket/SSE 或长连接；
- 支持断线重连；
- 前端重连后不得重复无限追加旧数据；
- 必须有最大缓存条数。

## 10.4 Audit

以下操作必须进入审计日志：

- config apply；
- task start/stop；
- device add/edit/delete；
- metadata change；
- write command；
- debug write。

---

# 11. Meta 数据统一响应规则

Meta 包括：

- Point Table；
- Point Group；
- Device Model；
- Device Type；
- Device Group；
- Unit。

## 11.1 Rename

稳定 ID 原则优先。

建议：

- Device ID、Task ID、Model ID、Type ID、Group ID 创建后默认不可改；
- Point Table / Point Group 若允许 rename，必须通过专门 Rename API 原子迁移引用；
- 不允许普通 Update 暗中完成 key rename。

## 11.2 Delete

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

## 11.3 Update

后端必须先判断字段属于：

- display-only；
- configuration-only；
- runtime-lightweight；
- connection-rebuild；
- destructive/migration。

根据类别生成正确 Change Plan。

---

# 12. 后端实现建议的 API 分层

具体 URL 可在实现阶段调整，但职责建议固定。

## 12.1 Query API

```text
GET /overview
GET /devices
GET /devices/{id}
GET /tasks
GET /points/tables
GET /metadata/...
GET /quality/...
GET /logs
GET /config/files/{name}
```

## 12.2 Command API

```text
POST /tasks/{id}/start
POST /tasks/{id}/stop
POST /devices/{id}/verify
POST /devices/verify
POST /debug/read
POST /debug/write
POST /debug/watch
DELETE /debug/watch/{session_id}
```

## 12.3 Configuration Change API

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

# 13. Apply Result

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

# 14. 后端开发验收基线

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

# 15. 当前前端原型与本规范的已知差异

当前 `wind-hub-admin` 为 mock 原型，以下行为后续需要逐步调整：

1. Point Table 的 `extends` 当前前端仍偏向复制式数据模型，应改为与真实配置一致的 `extends + remove_points + patch` 语义。
2. Point Table 删除当前存在迁移到 default table 的 mock 行为，正式逻辑应采用引用保护或显式迁移。
3. Point Group 删除当前存在迁移到 default group 的 mock 行为，正式逻辑不应静默迁移。
4. Device Model Update 当前直接修改 mock store，尚未做 Change Impact。
5. Task 编辑当前直接更新 mock 状态，未完整模拟运行实例停止/恢复。
6. Config 的 Validate/Save/Apply 当前均为 mock，后续必须进入统一配置事务。
7. Debug、Verification、Quality、Logs 当前数据主要为 mock，后端必须返回真实运行数据。
8. Overview 当前为本地聚合 mock；后续应由后端提供一致时间点的快照或可组合查询。
9. Point Connectivity Test 当前为 mock，多类型候选 UI 可以保留，但数据必须来自真实单点读取。

本文优先级高于上述 mock 行为；开发后端时不得为了迁就 mock 而固化错误语义。
