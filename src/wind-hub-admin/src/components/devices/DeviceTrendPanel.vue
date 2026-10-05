<script setup lang="ts">
// 趋势 panel：信号选择、时间窗、自动刷新、ECharts 展示与原始数据导出。
// 趋势业务状态在 useDeviceTrend；ECharts 配置为 view-specific，留在这里渲染。
import type { DeviceTrendFeature } from '../../composables/useDeviceTrend'

const props = defineProps<{
  trend: DeviceTrendFeature
}>()

const {
  chartEl,
  signals,
  pickerOpen,
  search,
  range,
  autoRefresh,
  lastRefreshAt,
  recording,
  candidates,
  toggleSelection,
  recordRawData,
  render,
} = props.trend
</script>

<template>
  <section class="panel-card trend-column">
    <div class="trend-header">
      <div class="trend-summary">
        <b>{{ signals.length }} signals</b>
        <span>Window {{ range }}</span>
        <span v-if="lastRefreshAt">Updated {{ lastRefreshAt }}</span>
      </div>
      <div class="trend-primary-actions">
        <el-button @click="pickerOpen = true">Select Signals</el-button>
        <div class="record-action">
          <el-button :loading="recording" :disabled="!signals.length" @click="recordRawData"
            >Record Raw Data</el-button
          >
          <el-tooltip
            content="Chart may be downsampled; recording exports all raw samples in the selected time window."
            placement="bottom"
          >
            <button type="button" class="help-dot" aria-label="Raw recording help">?</button>
          </el-tooltip>
        </div>
      </div>
    </div>

    <div class="trend-view-bar">
      <div class="trend-window-control">
        <span>Time Window</span>
        <el-segmented v-model="range" :options="['1 min', '5 min', '15 min', '1 h']" />
      </div>
      <div class="trend-update-control">
        <div class="auto-refresh-toggle">
          <span>Auto Update · 1 s</span><el-switch v-model="autoRefresh" />
        </div>
        <el-button @click="render">Refresh</el-button>
      </div>
    </div>

    <div v-if="!signals.length" class="trend-empty">
      No trend signals selected. Use “Select Signals” to add variables.
    </div>
    <div ref="chartEl" class="trend-chart control-trend-chart"></div>

    <!-- Trend picker -->
    <el-dialog v-model="pickerOpen" title="Select Trend Signals" width="var(--app-dialog-width-md)">
      <el-input
        v-model="search"
        clearable
        placeholder="Search point / variable / description..."
        class="trend-search"
      />

      <div class="trend-picker-list">
        <div v-for="r in candidates" :key="r.point_id" class="trend-picker-row">
          <div>
            <b>{{ r.point_id }}</b>
            <span>{{ r.variable_name }} · {{ r.groups.join(', ') }}</span>
          </div>
          <div class="trend-picker-value">
            <strong>{{ r.value }} {{ r.unit }}</strong>
            <el-button
              size="small"
              class="trend-select-button"
              :type="signals.some((x) => x.id === r.point_id) ? 'success' : 'default'"
              @click="toggleSelection(r)"
            >
              {{ signals.some((x) => x.id === r.point_id) ? 'Selected' : 'Select' }}
            </el-button>
          </div>
        </div>
      </div>

      <template #footer>
        <span class="subtle">{{ signals.length }} selected</span>
        <el-button type="primary" @click="pickerOpen = false">Done</el-button>
      </template>
    </el-dialog>
  </section>
</template>

<style scoped>
.trend-column {
  min-width: 0;
}
.trend-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--app-space-4);
  padding: var(--app-space-2) 0 var(--app-space-3);
}
.trend-summary {
  display: flex;
  align-items: baseline;
  gap: var(--app-space-3);
  min-width: 0;
}
.trend-summary b {
  font-size: var(--app-font-panel-title);
  font-weight: var(--app-font-weight-semibold);
}
.trend-summary span {
  color: var(--app-text-muted);
  font-size: var(--app-font-caption);
  white-space: nowrap;
}
.trend-primary-actions,
.trend-view-bar,
.trend-window-control,
.trend-update-control,
.record-action {
  display: flex;
  align-items: center;
  gap: var(--app-space-2);
}
.trend-view-bar {
  justify-content: space-between;
  padding: var(--app-space-2) 0 var(--app-space-3);
  border-top: 1px solid var(--app-border-soft);
}
.trend-window-control > span {
  color: var(--app-text-secondary);
  font-size: var(--app-font-label);
  white-space: nowrap;
}
.auto-refresh-toggle {
  display: flex;
  align-items: center;
  gap: var(--app-space-2);
  white-space: nowrap;
  color: var(--app-text-secondary);
  font-size: var(--app-font-label);
}
.help-dot {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: var(--app-help-icon-size);
  height: var(--app-help-icon-size);
  border: 1px solid var(--app-border-soft);
  border-radius: 50%;
  background: transparent;
  padding: 0;
  color: var(--app-text-muted);
  font-size: var(--app-font-caption);
  cursor: help;
}
.control-trend-chart {
  height: var(--app-chart-height-xl);
}
@media (max-width: 1199px) {
  .trend-header,
  .trend-view-bar {
    align-items: flex-start;
    flex-direction: column;
  }
  .trend-primary-actions,
  .trend-update-control {
    width: 100%;
  }
  .trend-view-bar {
    gap: var(--app-space-3);
  }
}
@media (max-width: 767px) {
  .trend-summary,
  .trend-primary-actions,
  .trend-window-control,
  .trend-update-control {
    flex-wrap: wrap;
  }
  .control-trend-chart {
    height: var(--app-chart-height-lg);
  }
}
</style>
