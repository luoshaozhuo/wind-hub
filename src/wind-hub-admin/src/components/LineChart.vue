<script setup lang="ts">
import * as echarts from 'echarts'
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'

const props = defineProps<{
  x: string[]
  series: Array<{ name: string; data: number[] }>
}>()

const el = ref<HTMLDivElement | null>(null)
let chart: echarts.ECharts | null = null
let observer: ResizeObserver | null = null

function render() {
  if (!chart) return
  chart.setOption({
    tooltip: { trigger: 'axis' },
    legend: { top: 0 },
    grid: { left: 48, right: 24, top: 48, bottom: 32 },
    xAxis: { type: 'category', data: props.x, boundaryGap: false },
    yAxis: { type: 'value' },
    series: props.series.map((item) => ({ ...item, type: 'line', smooth: true, showSymbol: false })),
  })
}

onMounted(() => {
  if (!el.value) return
  chart = echarts.init(el.value)
  render()
  observer = new ResizeObserver(() => chart?.resize())
  observer.observe(el.value)
})

watch(() => [props.x, props.series], render, { deep: true })

onBeforeUnmount(() => {
  observer?.disconnect()
  chart?.dispose()
})
</script>

<template><div ref="el" class="chart" /></template>
