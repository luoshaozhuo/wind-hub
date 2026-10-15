// ECharts 共享基础主题：字体、字号、文本/轴线/分割线颜色与序列色板全部从
// Application Token / Element Plus CSS 变量读取，页面不再各自硬编码。
// 只提供公共属性（textStyle / tooltip 排版 / 轴线样式 / 色板）；
// series、axis 数据、legend 与业务 tooltip 仍由各页面自行定义。

function css(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim()
}

function px(name: string, fallback: number): number {
  const v = parseFloat(css(name))
  return Number.isFinite(v) ? v : fallback
}

/** 序列色板：按应用状态语义排序，供多序列图表取色。 */
export function chartPalette(): string[] {
  return [
    css('--app-action-primary'),
    css('--app-status-healthy'),
    css('--app-status-warning'),
    css('--app-status-fault'),
    css('--app-status-info'),
    css('--app-status-disabled'),
  ].filter(Boolean)
}

/** 图表公共属性：字体族、文本色、tooltip 排版与色板。 */
export function baseChartOption() {
  return {
    animation: false,
    color: chartPalette(),
    textStyle: {
      fontFamily: css('--app-font-family'),
      fontSize: px('--app-font-body', 12),
      color: css('--app-text-regular'),
    },
    tooltip: {
      trigger: 'axis',
      textStyle: {
        fontFamily: css('--app-font-family'),
        fontSize: px('--app-font-body', 12),
      },
    },
  }
}

/** 类目/时间轴标签样式。 */
export function baseAxisLabel() {
  return { color: css('--app-text-muted'), fontSize: px('--app-font-caption', 10) }
}

/** 轴线样式。 */
export function baseAxisLine() {
  return { lineStyle: { color: css('--app-border') } }
}

/** 网格分割线样式。 */
export function baseSplitLine() {
  return { lineStyle: { color: css('--app-border-soft') } }
}
