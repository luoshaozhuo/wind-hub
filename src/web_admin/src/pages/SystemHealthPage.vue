<script setup lang="ts">
import * as echarts from 'echarts'
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import DataSourceBadge from '../components/DataSourceBadge.vue'
import { dataSourceState } from '../domain/dataSourceState'
// System Health 数据源（§23）：曲线 / 风险卡片 / 资源明细 / 存储挂载全部来自
// backend System Health API 的确定性模型；切窗口只改变查询的 range 输入。
import { useSystemHealth, type HealthRange } from '../composables/useSystemHealth'
import { baseAxisLabel, baseAxisLine, baseChartOption, baseSplitLine } from '../utils/chartTheme'

const range = ref<HealthRange>('24 h')
const { query, data, series, risks, details, mounts } = useSystemHealth(range)
const sourceState = computed(() =>
  dataSourceState({
    isPending: query.isPending.value,
    isError: query.isError.value,
    hasData: data.value != null,
  }),
)

const memoryEl = ref<HTMLElement | null>(null)
const diskEl = ref<HTMLElement | null>(null)
const cpuEl = ref<HTMLElement | null>(null)
const charts: echarts.ECharts[] = []
let resizeObserver: ResizeObserver | null = null

function baseOption() {
  return {
    ...baseChartOption(),
    grid: { left: 46, right: 18, top: 38, bottom: 34 },
    xAxis: {
      type: 'category',
      boundaryGap: false,
      axisLine: baseAxisLine(),
      axisLabel: baseAxisLabel(),
    },
    yAxis: { type: 'value', axisLabel: baseAxisLabel(), splitLine: baseSplitLine() },
  }
}
function initChart(el: HTMLElement | null, option: any) {
  if (!el) return
  const chart = echarts.init(el)
  chart.setOption(option)
  charts.push(chart)
  resizeObserver?.observe(el)
}
function renderCharts() {
  if (sourceState.value !== 'valid') {
    charts.splice(0).forEach((chart) => chart.dispose())
    return
  }
  if (!data.value) return
  charts.splice(0).forEach((c) => c.dispose())
  resizeObserver?.disconnect()
  resizeObserver = new ResizeObserver(() => charts.forEach((c) => c.resize()))
  const s = series.value
  const base = baseOption()
  initChart(memoryEl.value, {
    ...base,
    legend: { top: 4, right: 8, textStyle: { fontSize: 10 } },
    xAxis: { ...base.xAxis, data: s.axis },
    yAxis: { ...base.yAxis, name: 'GB', nameTextStyle: { fontSize: 10 } },
    series: [
      { name: 'Host used', type: 'line', showSymbol: false, data: s.memoryHost },
      { name: 'wind-hub RSS', type: 'line', showSymbol: false, data: s.memoryRss },
    ],
  })
  initChart(diskEl.value, {
    ...base,
    legend: { top: 4, right: 8, textStyle: { fontSize: 10 } },
    xAxis: { ...base.xAxis, data: s.axis },
    yAxis: { ...base.yAxis, name: 'GB', nameTextStyle: { fontSize: 10 } },
    series: [
      { name: 'Actual free', type: 'line', showSymbol: false, data: s.diskFree },
      {
        name: 'Forecast',
        type: 'line',
        showSymbol: false,
        lineStyle: { type: 'dashed' },
        data: s.diskForecast,
      },
    ],
  })
  initChart(cpuEl.value, {
    ...base,
    legend: { top: 4, right: 8, textStyle: { fontSize: 10 } },
    xAxis: { ...base.xAxis, data: s.axis },
    yAxis: [
      { ...base.yAxis, min: 0, max: 100, name: 'CPU %', nameTextStyle: { fontSize: 10 } },
      {
        type: 'value',
        min: 40,
        max: 100,
        name: '°C',
        position: 'right',
        axisLabel: baseAxisLabel(),
        splitLine: { show: false },
        nameTextStyle: { fontSize: 10 },
      },
    ],
    series: [
      { name: 'Host CPU', type: 'line', showSymbol: false, yAxisIndex: 0, data: s.cpuHost },
      { name: 'wind-hub CPU', type: 'line', showSymbol: false, yAxisIndex: 0, data: s.cpuProcess },
      { name: 'CPU temperature', type: 'line', showSymbol: false, yAxisIndex: 1, data: s.cpuTemp },
    ],
  })
}
watch([data, sourceState], () => nextTick(renderCharts), { immediate: true })
onBeforeUnmount(() => {
  resizeObserver?.disconnect()
  charts.forEach((c) => c.dispose())
})
</script>

<template>
  <div class="standard-page system-health-page">
    <div class="head">
      <div>
        <h1>System Health</h1>
        <p>宿主机与 wind-hub 进程资源健康，重点关注趋势、持续退化和容量耗尽风险。</p>
      </div>
    </div>

    <section class="health-section">
      <div class="section-title">
        <div>
          <h2>Current Risks <DataSourceBadge :state="sourceState" /></h2>
          <p>当前最需要处理的资源风险。</p>
        </div>
      </div>
      <p v-if="sourceState !== 'valid'" class="source-empty">— · 风险数据不可用</p>
      <div v-else class="risk-grid">
        <el-card v-for="r in risks" :key="r.name" shadow="never">
          <div class="risk-head">
            <b>{{ r.name }}</b
            ><el-tag :type="r.type" size="small">{{ r.state }}</el-tag>
          </div>
          <strong>{{ r.summary }}</strong>
          <span class="risk-detail">{{ r.detail }}</span>
        </el-card>
      </div>
    </section>

    <section class="health-section">
      <div class="section-title trends-title">
        <div>
          <h2>Resource Trends <DataSourceBadge :state="sourceState" /></h2>
          <p>趋势比单个瞬时值更重要；图表来自后端监控采样历史。</p>
        </div>
        <el-segmented
          v-model="range"
          :options="['1 h', '24 h', '7 d', '30 d']"
          @change="renderCharts"
        />
      </div>
      <p v-if="sourceState !== 'valid'" class="source-empty">— · 资源趋势数据不可用</p>
      <div v-show="sourceState === 'valid'" class="chart-grid">
        <el-card shadow="never"
          ><div class="chart-head"><b>Memory</b><span>Host used / wind-hub RSS</span></div>
          <div ref="memoryEl" class="health-chart"
        /></el-card>
        <el-card shadow="never"
          ><div class="chart-head"><b>CPU / Thermal</b><span>CPU usage + temperature</span></div>
          <div ref="cpuEl" class="health-chart"
        /></el-card>
        <el-card shadow="never"
          ><div class="chart-head"><b>Storage</b><span>Free space + forecast</span></div>
          <div ref="diskEl" class="health-chart"
        /></el-card>
      </div>
    </section>

    <section class="health-section">
      <div class="section-title">
        <div>
          <h2>Resource Details <DataSourceBadge :state="sourceState" /></h2>
          <p>当前值用于确认风险背景，不替代趋势判断。</p>
        </div>
      </div>
      <p v-if="sourceState !== 'valid'" class="source-empty">— · 资源明细不可用</p>
      <div v-else class="detail-grid">
        <el-card v-for="group in details" :key="group.group" shadow="never">
          <h3>{{ group.group }}</h3>
          <div class="detail-list">
            <div v-for="item in group.items" :key="item[0]">
              <span>{{ item[0] }}</span
              ><b>{{ item[1] }}</b>
            </div>
          </div>
        </el-card>
        <el-card shadow="never" class="storage-detail-card">
          <h3>Storage</h3>
          <el-table :data="mounts" size="small" table-layout="fixed">
            <el-table-column prop="mount" label="Mount" width="70" />
            <el-table-column prop="used" label="Used / Total" min-width="120" />
            <el-table-column prop="free" label="Free" width="80" />
            <el-table-column prop="usage" label="Usage" width="70" />
            <el-table-column prop="growth" label="24 h Growth" min-width="110" />
            <el-table-column prop="estimated" label="Estimated Full" min-width="105" />
          </el-table>
        </el-card>
      </div>
    </section>
  </div>
</template>

<style scoped>
.source-empty {
  color: var(--app-text-muted);
  padding: var(--app-space-3);
}
.health-section {
  margin-top: var(--app-space-6);
}
.section-title {
  margin-bottom: var(--app-space-3);
}
.trends-title {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: var(--app-space-4);
}
.section-title h2 {
  margin: 0;
  font-size: var(--app-font-section-title);
  font-weight: var(--app-font-weight-semibold);
}
.section-title p {
  margin: var(--app-space-1) 0 0;
  color: var(--app-text-muted);
  font-size: var(--app-font-caption);
}
.risk-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: var(--app-space-3);
}
.risk-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--app-space-2);
}
.risk-head b {
  font-size: var(--app-font-panel-title);
}
.risk-grid strong {
  display: block;
  margin-top: var(--app-space-3);
  font-size: var(--app-font-panel-title);
  font-weight: var(--app-font-weight-semibold);
}
.risk-detail {
  display: block;
  margin-top: var(--app-space-1);
  color: var(--app-text-muted);
  font-size: var(--app-font-caption);
}
.chart-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: var(--app-space-4);
}
.chart-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: var(--app-space-3);
}
.chart-head b {
  font-size: var(--app-font-panel-title);
}
.chart-head span {
  color: var(--app-text-muted);
  font-size: var(--app-font-caption);
}
.health-chart {
  height: var(--app-chart-height-md);
}
.detail-grid {
  display: grid;
  grid-template-columns: minmax(0, 0.8fr) minmax(0, 0.8fr) minmax(0, 1.8fr);
  gap: var(--app-space-3);
}
.detail-grid h3 {
  margin: 0 0 var(--app-space-2);
  font-size: var(--app-font-panel-title);
}
.detail-list {
  display: grid;
}
.detail-list > div {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--app-space-3);
  padding: var(--app-space-2) 0;
  border-bottom: 1px solid var(--app-border-soft);
}
.detail-list > div:last-child {
  border-bottom: 0;
}
.detail-list span {
  color: var(--app-text-muted);
  font-size: var(--app-font-caption);
}
.detail-list b {
  font-size: var(--app-font-body);
  font-weight: var(--app-font-weight-semibold);
}
.storage-detail-card {
  min-width: 0;
}
@media (max-width: 1199px) {
  .risk-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .chart-grid {
    grid-template-columns: 1fr;
  }
  .detail-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .storage-detail-card {
    grid-column: 1/-1;
  }
}
@media (max-width: 767px) {
  .risk-grid,
  .detail-grid,
  .chart-grid {
    grid-template-columns: 1fr;
  }
  .storage-detail-card {
    grid-column: auto;
  }
  .trends-title {
    align-items: flex-start;
    flex-direction: column;
  }
  .health-chart {
    height: var(--app-chart-height-sm);
  }
}
</style>
