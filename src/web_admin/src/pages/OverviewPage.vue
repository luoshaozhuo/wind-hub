<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import AdminPageHeading from '../components/AdminPageHeading.vue'
import DataSourceBadge from '../components/DataSourceBadge.vue'
import { dataSourceState, type DataSourceState } from '../domain/dataSourceState'
import { runtimeApi } from '../api/runtime'

type SourceKey = 'devices' | 'tasks' | 'sinks' | 'values'
type SourceSnapshot = { loaded: boolean; loading: boolean; error: string }
const snapshot = ref<Record<SourceKey, SourceSnapshot>>({
  devices: { loaded: false, loading: true, error: '' },
  tasks: { loaded: false, loading: true, error: '' },
  sinks: { loaded: false, loading: true, error: '' },
  values: { loaded: false, loading: true, error: '' },
})
const deviceTotal = ref(0)
const deviceOnline = ref(0)
const taskTotal = ref(0)
const taskRunning = ref(0)
const sinkTotal = ref(0)
const sinkHealthy = ref(0)
const pointTotal = ref(0)

let timer: ReturnType<typeof setInterval> | undefined
let active = true
let refreshing = false

function sourceState(key: SourceKey): DataSourceState {
  const item = snapshot.value[key]
  return dataSourceState({
    isPending: item.loading && !item.loaded,
    isError: Boolean(item.error),
    hasData: item.loaded,
  })
}

const cards = computed(() => [
  { key: 'devices' as SourceKey, label: '通信正常设备', current: deviceOnline.value, total: deviceTotal.value },
  { key: 'tasks' as SourceKey, label: '运行任务', current: taskRunning.value, total: taskTotal.value },
  { key: 'sinks' as SourceKey, label: '健康 Sink', current: sinkHealthy.value, total: sinkTotal.value },
  { key: 'values' as SourceKey, label: '最新测点记录', current: pointTotal.value, total: null },
])

async function refresh() {
  if (refreshing) return
  refreshing = true
  try {
    const results = await Promise.allSettled([
      runtimeApi.devices(), runtimeApi.tasks(), runtimeApi.sinks(), runtimeApi.latestValues(),
    ] as const)
    if (!active) return
    const [devices, tasks, sinks, values] = results
    if (devices.status === 'fulfilled') {
      deviceTotal.value = devices.value.length
      deviceOnline.value = devices.value.filter(d => d.connected).length
    }
    if (tasks.status === 'fulfilled') {
      taskTotal.value = tasks.value.length
      taskRunning.value = tasks.value.filter(t => t.runtime_state.toLowerCase() === 'running').length
    }
    if (sinks.status === 'fulfilled') {
      sinkTotal.value = sinks.value.length
      sinkHealthy.value = sinks.value.filter(s => s.healthy).length
    }
    if (values.status === 'fulfilled') pointTotal.value = values.value.length
    const keys: SourceKey[] = ['devices', 'tasks', 'sinks', 'values']
    for (const [i, key] of keys.entries()) {
      const item = results[i]
      snapshot.value[key] = {
        loaded: snapshot.value[key].loaded || item.status === 'fulfilled',
        loading: false,
        error: item.status === 'rejected' ? String(item.reason) : '',
      }
    }
  } finally {
    refreshing = false
  }
}

onMounted(() => {
  active = true
  void refresh()
  timer = setInterval(() => void refresh(), 1000)
})
onUnmounted(() => {
  active = false
  if (timer) clearInterval(timer)
})
</script>

<template>
  <section class="runtime-page">
    <AdminPageHeading title="运行总览" description="Collector 实时设备、任务、Sink 与最新测点记录；每秒查询。" />
    <el-row :gutter="16">
      <el-col v-for="card in cards" :key="card.key" :xs="24" :sm="12" :lg="6">
        <el-card shadow="never" class="overview-metric">
          <div class="overview-metric__head">
            <span>{{ card.label }}</span>
            <DataSourceBadge :state="sourceState(card.key)" :reason="snapshot[card.key].error" />
          </div>
          <div class="overview-metric__value">
            <template v-if="sourceState(card.key) === 'valid'">
              {{ card.current }}<span v-if="card.total !== null"> / {{ card.total }}</span>
            </template>
            <template v-else>—</template>
          </div>
          <p v-if="sourceState(card.key) === 'stale'" class="overview-metric__note">数据已中断，旧值不作为实时指标展示</p>
        </el-card>
      </el-col>
    </el-row>
    <p class="overview-note">仅展示现有 REST 接口可提供的指标，不将最新值记录数解释为点表总数。</p>
  </section>
</template>

<style scoped>
.overview-metric { margin-bottom: 16px; }
.overview-metric__head { display: flex; justify-content: space-between; align-items: center; gap: 8px; color: var(--el-text-color-secondary); font-size: 13px; }
.overview-metric__value { margin-top: 14px; min-height: 38px; font-size: 28px; font-weight: 650; font-variant-numeric: tabular-nums; }
.overview-metric__value span { font-size: 18px; color: var(--el-text-color-secondary); }
.overview-metric__note, .overview-note { font-size: 12px; color: var(--el-text-color-secondary); }
</style>
