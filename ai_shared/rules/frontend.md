# Web 前端编码规则

适用于 wind-hub-admin。默认基础组件库为 Element Plus，图表库为 ECharts。

## 1. 开始修改前

1. 先读取现有 Theme、Semantic Token、共享组件、布局和相似页面。
2. 优先复用现有 Element Plus Theme System、公开 Token、Props、Variants 和 Size Variants。
3. 不为当前页面建立平行设计体系；全局问题在 Theme/共享层修复。
4. 已定型页面的视觉和行为不因无关任务改变。

## 2. 设计系统边界

推荐层次：

```text
Element Plus Design System
→ Project Theme Configuration
→ Application Semantic Tokens
→ Shared Business Components
→ Layout / Interaction / Responsive
→ Page
```

规则：

1. 单一 Element Plus 项目不预建 Theme Adapter/Mapping；只有多组件库、白标或明确迁移需求时才引入。
2. Button/Input/Select/Dialog/Drawer/Table/Switch 等标准组件优先直接使用 Element Plus。
3. `primary/success/warning/danger`、`small/default/large`、标准 Radius、Typography、State、组件内部 Spacing 不在项目层复制。
4. Application Semantic Token 只补组件库不知道的业务语义，例如 running/fault/fresh/stale。
5. 页面不得硬编码品牌色、状态色、任意字号、控件高度、圆角和零散 spacing；优先使用 Theme/Token/统一语义类。
6. 不依赖 Element Plus 私有变量、内部 DOM 或深层选择器。

## 3. Typography、Spacing 与布局

1. Page / Section / Panel / Body / Label / Caption / Metric 使用统一 Typography 语义。
2. 默认控件使用 Element Plus `default`；高密度 Toolbar/行操作可用 `small`；`large` 慎用。
3. 同一 Toolbar / Form Group 控件 Size 保持一致。
4. Layout spacing 使用项目统一尺度和语义，不用局部 margin/height 修补全局尺度问题。
5. 使用统一 Page Container、Section Gap、Grid Gap 和 Pagination 位置。
6. 普通 Section 不强制套 Card；避免 Card 套 Card；普通后台 Card 优先 Border，Shadow 主要用于真正浮层。

## 4. 组件语义

选择组件先判断：业务意图、信息范围、是否保留上下文、复杂度、数据规模、风险和持续时间。

| 语义 | 推荐组件 |
|---|---|
| 跨一级功能导航 | Menu / Router |
| 同一对象平级视图 | Tabs |
| 当前任务模式 | Segmented |
| 参数选择 | Radio / Select / Checkbox |
| 立即生效二元状态 | Switch |
| 普通动作 | Button / Dropdown Action |
| 高风险确认 | Popconfirm / MessageBox |
| 短流程/少量字段 | Dialog |
| 保留页面上下文的复杂详情或编辑 | Drawer |
| 长期复杂工作区 | Page |
| 辅助定义 | Tooltip / Popover |

禁止：

1. Button 模拟 Tabs/Segmented。
2. Segmented 执行 Save/Delete/Run。
3. 需要 Save 后才生效的表单字段使用 Switch 表示即时状态。
4. 关键状态只藏在 Tooltip。
5. 复杂 `Drawer → Drawer`、`Dialog → Dialog`、`Dialog → Drawer` 嵌套。

同一操作上下文原则上只有一个 Primary Action；行内高频操作原则上不超过 2 个，其余进入 More。

## 5. Table / Form / Chart

### Table

1. 多对象横向比较优先 Table；大量设备、任务、点表和日志不改成 Card Grid。
2. 默认启发式：
   - `≤20` 且简单：可不分页；
   - `20~100`：默认分页；
   - `>100`：分页、虚拟化或服务端查询；
   - 持续增长数据：服务端分页；
   - Drawer 内 Table：默认分页。
3. Search / Filter / Sort 作用于完整数据集，不只当前页；改变筛选后回第 1 页。
4. Summary / Chart 默认基于完整 filtered dataset，而不是当前页。
5. 窄屏优先保留主标识、状态和关键业务列，再隐藏次要列或横向滚动。

### Form

1. Save 在无 Dirty Changes 时 disabled。
2. 提交前统一校验；提交期间 loading + disabled，阻止重复提交。
3. 长表单按业务 Section 分组，不因字段多就机械使用 Steps。
4. Page/Drawer/Dialog 的 Action 放在对应 Footer。
5. Save / Discard / Apply / Reset / Cancel 语义保持一致。

### Chart

1. 统一使用 ECharts。
2. 时间趋势 Line、类别比较 Bar、分布 Histogram、相关性 Scatter；精确值仍用 Table。
3. 多序列显隐优先使用 ECharts Legend。
4. 图表明确数据窗口和聚合范围，不使用 3D Chart 或装饰性 Gauge 替代普通百分比。

## 6. 异步与工业控制状态

通用状态按需要显式表达：

```text
Idle / Disabled / Dirty / Validating / Confirming / Submitting
Loading / Progress / Success / Partial Success / Error / Cancelled / Stale
```

1. Loading 范围与操作范围一致；局部请求不锁死整页。
2. Button Loading 保持尺寸稳定，Auto Refresh 不造成布局跳动。
3. 不知道真实比例时只显示 Processing，不伪造百分比。
4. 批量操作必须能表达 Total / Succeeded / Failed / Skipped 和失败对象原因。
5. Device Start/Stop、PLC Write、Task Start/Stop、Config Apply、Delete、Sink Write Test 等采用 Server-confirmed UI：

```text
Click
→ Pending/Starting/Applying
→ Backend confirms
→ Success / Error
```

不得点击后立即乐观显示最终生产状态。

## 7. Refresh / Streaming

1. Manual Refresh：普通管理数据。
2. Auto Refresh：Health/Quality/Status；显示 interval 和 Last Updated。
3. Streaming：Logs、实时值、高频趋势；必须考虑 Pause、Reconnect、最大缓存和滚动行为。
4. 用户编辑 Form 时，实时刷新不得覆盖未保存 Draft。
5. 高风险动作依赖的数据已 Stale 时，先 Refresh/Revalidate。

## 8. Responsive / Scroll / Accessibility

统一断点：

```text
Mobile  < 768
Tablet  768~1199
Desktop >= 1200
```

响应式是组件变体而非只改单列：

- Drawer → Mobile full-width；
- Dialog → Tablet/Mobile near-fullscreen；
- Toolbar → wrap / More；
- Master-Detail → Mobile List → Detail；
- 多列 Form → Mobile single-column；
- Table → column priority + horizontal scroll。

一个视觉区域原则上只有一个主要纵向滚动容器；避免 Page + Card + Table + Drawer 多层纵向滚动。

最低可访问性：

1. 关键状态不只靠颜色。
2. Icon-only Button 提供 Tooltip 或 `aria-label`。
3. 主要交互可见 Focus。
4. Form Error 与字段关联。
5. Mobile 不依赖 hover 才能获取关键说明。

## 9. 修改完成检查

确认：

- 未建立平行 Theme/Token/Size/Radius/Typography；
- 未重复实现 Element Plus 已有组件；
- 组件语义与业务意图一致；
- Loading/Disabled/Error/Empty/Stale 和重复提交已处理；
- Table 分页/查询策略明确；
- Desktop/Tablet/Mobile 行为明确；
- 高风险工业操作采用 Server-confirmed UI；
- 没有不必要的局部硬编码和深层 DOM 覆写。
