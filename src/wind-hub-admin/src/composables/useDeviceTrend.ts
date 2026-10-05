// 设备趋势 feature：按需拉取的趋势序列缓存、信号选择、ECharts 生命周期、
// 自动刷新与原始数据导出。Trend 为页内按需缓存（非 Vue Query 托管），
// 序列与 Data 当前值同源（§7）：Command 写回后趋势在同一序列上追加新值。
import * as echarts from 'echarts'
import { computed, nextTick, onBeforeUnmount, ref, watch, type ComputedRef, type Ref } from 'vue'
import { useEventListener, useIntervalFn } from '@vueuse/core'
import { ElMessage } from 'element-plus'
import { fetchDeviceTrend } from '../api/devices'
import { baseAxisLabel, baseAxisLine, baseChartOption, baseSplitLine } from '../utils/chartTheme'
import { formatTimestamp } from '../utils/format'
import type { DataRow } from '../domain/deviceData'
import type { DeviceInst } from '../domain/types'

export interface TrendSignal {
  id: string
  label: string
  unit: string
  pointIndex: number
}

function trendRangeMs(range: string) {
  if (range === '5 min') return 5 * 60_000
  if (range === '15 min') return 15 * 60_000
  if (range === '1 h') return 60 * 60_000
  return 60_000
}

function downsampleForChart(raw: [Date, number][], maxPoints = 240) {
  if (raw.length <= maxPoints) return raw
  const step = Math.ceil(raw.length / maxPoints)
  return raw.filter((_, i) => i % step === 0 || i === raw.length - 1)
}

function csvCell(value: string | number) {
  const text = String(value)
  return /[",\n]/.test(text) ? '"' + text.replace(/"/g, '""') + '"' : text
}

/**
 * @param device 当前选中设备（Drawer 以 device_id 为 key 重建本 feature）。
 * @param active Drawer 打开且 Control & Trend tab 激活：驱动首次渲染与自动刷新。
 * @param dataRows useDeviceData 的展示行（信号候选与取值来源）。
 */
export function useDeviceTrend(
  device: Ref<DeviceInst | null>,
  active: ComputedRef<boolean>,
  dataRows: ComputedRef<DataRow[]>,
) {
  const chartEl = ref<HTMLElement | null>(null)
  let chart: echarts.ECharts | null = null
  let resizeObserver: ResizeObserver | null = null
  // Trend 为按需拉取的图表数据：feature 级缓存，随 fetchDeviceTrend 结果覆盖。
  const seriesCache = new Map<string, Array<[Date, number]>>()
  const legendSelected = ref<Record<string, boolean>>({})
  const pickerOpen = ref(false)
  const search = ref('')
  const range = ref('1 min')
  const autoRefresh = ref(false)
  const lastRefreshAt = ref('')
  const signals = ref<TrendSignal[]>([])
  const recording = ref(false)

  function rawDataOf(signal: TrendSignal): Array<[Date, number]> {
    if (!device.value) return []
    return seriesCache.get(signal.id) || []
  }

  async function recordRawData() {
    if (!device.value || !signals.value.length) {
      ElMessage.warning('Select at least one trend signal first')
      return
    }
    recording.value = true
    try {
      const rows: string[] = ['timestamp,device_id,point_id,variable_name,value,unit']
      signals.value.forEach((signal) => {
        for (const [ts, value] of rawDataOf(signal)) {
          rows.push(
            [ts.toISOString(), device.value!.device_id, signal.id, signal.label, value, signal.unit]
              .map(csvCell)
              .join(','),
          )
        }
      })
      const blob = new Blob([rows.join('\n')], { type: 'text/csv;charset=utf-8' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `${device.value.device_id}_waveform_${range.value.replace(/\s+/g, '_')}.csv`
      a.click()
      URL.revokeObjectURL(url)
      ElMessage.success('Raw waveform data exported ')
    } finally {
      recording.value = false
    }
  }

  const candidates = computed(() =>
    dataRows.value.filter((r) => {
      const q = search.value.trim().toLowerCase()
      return (
        !q || [r.point_id, r.variable_name, r.description].some((x) => x.toLowerCase().includes(q))
      )
    }),
  )

  function toggleSelection(r: DataRow) {
    const existing = signals.value.find((x) => x.id === r.point_id)
    if (existing) {
      signals.value = signals.value.filter((x) => x.id !== r.point_id)
      delete legendSelected.value[existing.label]
    } else {
      const signal = {
        id: r.point_id,
        label: r.variable_name || r.point_id,
        unit: r.unit,
        pointIndex: r.index,
      }
      signals.value.push(signal)
      legendSelected.value[signal.label] = true
    }
    void render()
  }

  function seedSignals() {
    if (signals.value.length || !dataRows.value.length) return
    signals.value = dataRows.value.slice(0, 4).map((r) => ({
      id: r.point_id,
      label: r.variable_name || r.point_id,
      unit: r.unit,
      pointIndex: r.index,
    }))
    for (const s of signals.value) legendSelected.value[s.label] = true
  }

  async function render() {
    if (device.value && signals.value.length) {
      const rows = await fetchDeviceTrend(
        device.value.device_id,
        signals.value.map((signal) => signal.id),
        Math.max(1, Math.ceil(trendRangeMs(range.value) / 1000)),
      )
      for (const row of rows) {
        seriesCache.set(
          row.point_id,
          row.samples
            .filter((sample) => typeof sample.value === 'number')
            .map((sample) => [new Date(sample.timestamp), Number(sample.value)]),
        )
      }
    }
    lastRefreshAt.value = formatTimestamp(new Date())
    nextTick(() => {
      if (!chartEl.value) return
      if (!chart) {
        chart = echarts.init(chartEl.value)
        resizeObserver = new ResizeObserver(() => chart?.resize())
        resizeObserver.observe(chartEl.value)
        chart.on('legendselectchanged', (params: unknown) => {
          const p = params as { selected?: Record<string, boolean> }
          legendSelected.value = { ...(p.selected || {}) }
        })
      }

      const series = signals.value.map((s) => ({
        name: s.label,
        type: 'line',
        showSymbol: false,
        smooth: true,
        data: downsampleForChart(rawDataOf(s)),
      }))

      chart.setOption(
        {
          ...baseChartOption(),
          legend: {
            type: 'scroll',
            top: 10,
            left: 18,
            right: 18,
            selected: legendSelected.value,
          },
          grid: { top: 58, left: 56, right: 24, bottom: 42 },
          xAxis: {
            type: 'time',
            boundaryGap: false,
            axisLabel: baseAxisLabel(),
            axisLine: baseAxisLine(),
          },
          yAxis: {
            type: 'value',
            scale: true,
            axisLabel: baseAxisLabel(),
            splitLine: baseSplitLine(),
          },
          series,
        },
        true,
      )
      chart.resize()
    })
  }

  const refreshTimer = useIntervalFn(() => void render(), 1000, { immediate: false })
  function syncRefreshTimer() {
    if (active.value && autoRefresh.value) refreshTimer.resume()
    else refreshTimer.pause()
  }

  watch(active, (value) => {
    if (value) {
      seedSignals()
      void render()
    }
    syncRefreshTimer()
  })
  watch([range, autoRefresh], () => {
    if (active.value) void render()
    syncRefreshTimer()
  })

  useEventListener(window, 'resize', () => chart?.resize())
  onBeforeUnmount(() => {
    refreshTimer.pause()
    resizeObserver?.disconnect()
    resizeObserver = null
    chart?.dispose()
    chart = null
  })

  return {
    chartEl,
    signals,
    legendSelected,
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
  }
}

export type DeviceTrendFeature = ReturnType<typeof useDeviceTrend>
