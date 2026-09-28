# Web 前端编码原则

> 用于 Claude、Codex 等代码生成工具。目标是让新增或修改页面稳定遵守项目现有设计系统，避免局部自定义破坏全局一致性。

## 1. 执行顺序

开始编码前必须按顺序执行：

1. 读取项目现有组件库、Theme、Token、共享组件、布局和图表配置。
2. 优先复用现有设计系统，不新建平行体系。
3. 若项目尚未定义视觉基础，先确定：
   - Component Library；
   - Color System；
   - Typography；
   - Style Profile。
4. 建立或复用 Theme Mapping 和 Application Semantic Tokens。
5. 再实现页面、组件和响应式行为。
6. 完成状态、可访问性和生成检查。

默认组件库为 **Element Plus**；默认图表库为 **ECharts**。

---

## 2. 核心原则

1. **组件优先**：优先使用成熟组件库已有能力。
2. **语义优先**：业务页面使用 Semantic Token，不直接依赖原始颜色、字号、圆角和间距。
3. **全局优先**：品牌色、字体、圆角、控件尺寸通过 Theme 统一修改，不逐页覆盖。
4. **一致优先**：相同业务场景使用相同组件、布局和交互 Pattern。
5. **响应式默认成立**：Desktop、Tablet、Mobile 必须同时考虑。
6. **局部样式最小化**：仅在组件库和全局规则无法覆盖时写局部 CSS。
7. **实现可替换**：业务语义和应用级 Token 不依赖具体组件库。

---

## 3. 设计系统分层

按以下层次组织设计：

```text
Foundation
  ↓
Theme / Theme Mapping
  ↓
Application Semantic Tokens
  ↓
Component Tokens / Shared Components
  ↓
Pattern / Layout
  ↓
Page
```

### Foundation

包含：

- Color Palette / Color Scale；
- Typography；
- Spacing；
- Radius；
- Size；
- Elevation。

基础值不得散落到业务页面。

### Theme

负责组件库统一视觉配置。

Element Plus 项目优先通过官方 Theme、Design Tokens 和 CSS Custom Properties 实现。

### Application Semantic Tokens

仅定义项目业务和布局语义，例如：

```css
--app-page-padding
--app-content-max-width
--app-section-gap
--app-card-gap
--app-status-healthy
--app-status-warning
--app-status-fault
```

### Component Tokens

仅用于项目自定义共享组件，例如：

```css
--app-card-padding
--app-panel-radius
--app-toolbar-gap
```

---

## 4. Color 与 Theme

一个 Theme 必须基于明确颜色体系，至少包含：

- Primary；
- Success；
- Warning；
- Danger / Error；
- Info；
- Neutral。

推荐每种主要颜色使用统一色阶，如：

```text
50 / 100 / 200 / ... / 900 / 950
```

业务页面不得按“颜色名称”选色，应按语义使用 Token。

默认语义：

| Token | 用途 |
|---|---|
| Primary | 主操作、选中、交互强调 |
| Success | 成功、正常、健康 |
| Warning | 告警、退化、需关注 |
| Danger / Error | 故障、错误、删除、高风险操作 |
| Info | 中性提示 |
| Text Primary | 标题、主要正文、关键内容 |
| Text Regular | 常规正文 |
| Text Secondary | 辅助说明、元数据 |
| Border | 分隔、边界、输入框 |
| Background / Surface | 页面和容器层级 |

禁止使用状态色做无业务含义的装饰。

---

## 5. Typography、Spacing、Layout

### Typography

页面必须复用统一字体体系。

应用级文字层级至少统一：

- Page Title；
- Section Title；
- Card Title；
- Body；
- Caption；
- Metric。

禁止页面自行建立字号和字体体系。

### Spacing

统一使用项目 Spacing Scale。默认可采用：

```text
4 / 8 / 12 / 16 / 24 / 32 px
```

Page Padding、Section Gap、Card Padding、Form Gap、Toolbar Gap、Inline Gap 必须从统一尺度中选取。

### Layout

必须遵守：

- 使用统一 Page Container；
- 统一页面最大宽度和水平留白；
- Section、Card、Toolbar、Form、Grid 使用统一 Gap；
- 同类页面保持一致 Header / Toolbar / Content / Pagination 层次；
- 优先使用组件库或共享布局组件；
- 不自行实现组件库已有的分栏、分割线和基础容器；
- Table、Chart、Panel 必须定义窄屏降级方案；
- 阴影和层级统一使用 Theme 或 Application Token。

---

## 6. 组件选择优先级

实现 UI 时按以下顺序选择：

1. 当前组件库标准组件；
2. 组件库推荐组合；
3. 项目共享业务组件；
4. 专业领域组件库，例如 ECharts；
5. 自定义实现。

原则上不得为了少量需求引入第二套完整通用 UI 组件库。

禁止重复实现组件库已有能力。

---

## 7. 统一交互 Pattern

相同业务语义必须采用相同交互。

常见约定：

- Primary Button：页面主要操作；
- Default Button：次要操作；
- Danger Button：删除、停用等高风险操作；
- Dialog：短流程、局部编辑；
- Drawer：复杂编辑且需要保留页面上下文；
- Tooltip：补充说明，不承载关键业务信息；
- Message / Notification：操作结果反馈；
- MessageBox：高风险确认。

统一 Pattern：

```text
列表页：Page Header → Toolbar / Filter → Content → Pagination
详情页：Header → Summary → Sections
配置修改：Edit → Validate → Submit → Feedback
删除操作：Confirm → Submit → Result
异步操作：Idle → Loading → Success / Error
```

---

## 8. Table、Form、Chart

### Table

必须：

- 保持列定义、对齐、排序、筛选和行操作位置一致；
- 重要列优先保留；
- 窄屏下隐藏次要列或横向滚动；
- 不通过极端压缩破坏可读性；
- 明确处理 Loading、Empty、Error；
- 大数据量使用分页、虚拟化或服务端查询；
- 高风险行操作必须确认。

### Form

必须：

- 统一 Label、控件宽度、必填标识和错误提示；
- 字段附近显示校验错误；
- 提交时统一校验；
- 提交期间 Loading 并阻止重复提交；
- 多列 Form 在窄屏自动降为单列；
- 高风险或不可逆修改必须二次确认。

### Chart

统一使用项目指定图表库，默认 ECharts。

必须：

- 使用 Theme / Semantic Token 颜色；
- 同类指标统一颜色、单位、Tooltip、Legend；
- 处理 Loading、Empty、Error、Resize；
- 多序列显隐优先使用图表库原生 Legend；
- 不用装饰性视觉替代数据语义；
- 窄屏优先调整布局、Legend 和 Tooltip，不简单压缩文字。

---

## 9. 响应式

项目断点必须集中定义并全局复用。

默认：

```text
Mobile:  < 768 px
Tablet:  768 ~ 1199 px
Desktop: >= 1200 px
```

默认行为：

| 对象 | Desktop | Tablet | Mobile |
|---|---|---|---|
| Grid / Card | 多列 | 减少列数 | 单列优先 |
| Toolbar | 单行优先 | 可换行 | 换行或折叠次要操作 |
| Form | 多列可用 | 视宽度降列 | 单列 |
| Table | 完整列 | 隐藏次要列或滚动 | 横向滚动、折叠次要信息 |
| Dialog | 常规宽度 | 限制最大宽度 | 近全宽；复杂编辑改 Drawer / Fullscreen |
| Chart | 可并排 | 减列 | 单列、自适应宽度 |
| Navigation | 展开 | 可折叠 | 折叠或 Drawer |

禁止各页面自行定义独立断点体系。

---

## 10. 状态与可访问性

组件和页面至少处理：

```text
hover
focus
active
disabled
loading
success
warning
error
empty
```

异步操作必须阻止重复提交。

Error 和 Empty 必须提供可理解信息，必要时提供恢复入口。

最低可访问性要求：

- 关键状态不能只依赖颜色；
- 交互元素必须有明确 Focus；
- 图标按钮必须有 Tooltip、`aria-label` 或等价文本；
- 表单错误必须与字段关联；
- 正文和状态文字保持足够对比度；
- 主要流程可通过键盘操作；
- 响应式变化后主要功能仍可访问。

---

## 11. Element Plus 项目约束

Element Plus 项目优先使用：

- 官方组件；
- Theme System；
- 公开 Design Tokens；
- CSS Custom Properties；
- 官方 Props、Variant 和尺寸能力。

品牌级视觉统一优先在统一入口配置，例如：

```text
src/styles/element/index.scss
```

建议样式结构：

```text
src/styles/
├── element/
│   └── index.scss
├── tokens/
│   ├── semantic.css
│   └── component.css
├── typography.css
├── layout.css
├── utilities.css
└── index.css
```

职责：

| 文件 | 职责 |
|---|---|
| `element/index.scss` | Element Plus Theme Override |
| `tokens/semantic.css` | 应用级语义 Token |
| `tokens/component.css` | 自定义组件 Token |
| `typography.css` | 页面文字语义 |
| `layout.css` | Padding、Gap、最大宽度、响应式 |
| `utilities.css` | 少量通用辅助样式 |
| `index.css` | 全局样式入口 |

---

## 12. 禁止事项

禁止：

- 在业务页面硬编码品牌色和状态色；
- 页面自行建立字体体系；
- 页面任意定义常规字号、圆角、控件高度、边框；
- 大量使用任意 Spacing Value；
- 重复实现组件库已有组件；
- 使用深层选择器依赖组件内部 DOM；
- 依赖组件库私有变量或私有 DOM 结构；
- 为单页效果破坏全局 Token 或 Pattern；
- 新建与现有 Theme、Token、共享组件重复的体系；
- 各页面自行定义响应式断点；
- 用局部 CSS 修补本应由 Theme 或共享组件解决的问题。

---

## 13. 修改现有项目时

修改已有页面前：

1. 先查现有 Theme、Token、共享组件和相似页面；
2. 尽量复用现有 Pattern；
3. 不改变已定型页面的视觉和行为，除非任务明确要求；
4. 若发现重复实现，优先收敛到共享组件或统一 Token；
5. 不做与当前任务无关的大规模重构；
6. 修改组件库适配时，避免影响无关页面。

---

## 14. 生成前检查

提交代码前逐项确认：

- [ ] 已复用现有 Component Library、Theme、Token 和共享组件；
- [ ] 页面未硬编码无必要的品牌色、状态色、字号、圆角和间距；
- [ ] 未重复实现组件库已有能力；
- [ ] 相同业务语义使用统一组件和 Pattern；
- [ ] Desktop / Tablet / Mobile 行为明确；
- [ ] Loading / Empty / Error / Disabled 状态完整；
- [ ] 异步操作已阻止重复提交；
- [ ] 高风险操作有确认机制；
- [ ] 窄屏下主要功能仍可访问；
- [ ] Table / Form / Chart 遵守统一规则；
- [ ] 未依赖组件库私有 DOM 或私有变量；
- [ ] 新代码未建立平行设计体系。

若任一项不满足，应优先修正后再提交。
