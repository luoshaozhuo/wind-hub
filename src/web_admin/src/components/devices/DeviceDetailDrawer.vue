<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { runtimeApi, type LatestPointValue, type RuntimeDevice } from '../../api/runtime'
import DataSourceBadge from '../DataSourceBadge.vue'
import DeviceCommandPanel from './DeviceCommandPanel.vue'
import DeviceTrendPanel from './DeviceTrendPanel.vue'
import { dataSourceState } from '../../domain/dataSourceState'

const props = defineProps<{ device: RuntimeDevice }>()
const opened = defineModel<boolean>({ required: true })
const tab = ref('data')
const points = ref<LatestPointValue[]>([])
const loading = ref(false)
const error = ref('')
const loaded = ref(false)
const search = ref('')
const page = ref(1)
const pageSize = ref(50)
const autoRefresh = ref(true)
const selectedPoint = ref('')
const result = ref<Record<string, unknown> | null>(null)
const operationError = ref('')
const operation = ref('')
let timer: ReturnType<typeof setInterval> | undefined
let active = true

const state = computed(() => dataSourceState({
  isPending: loading.value && !loaded.value,
  isError: !!error.value,
  hasData: loaded.value,
}))
const filtered = computed(() => points.value.filter(p =>
  !search.value || p.point_id.toLowerCase().includes(search.value.toLowerCase()),
))
const paged = computed(() => filtered.value.slice((page.value - 1) * pageSize.value, page.value * pageSize.value))
watch(search, () => { page.value = 1 })

async function refresh() {
  if (!opened.value || loading.value) return
  loading.value = true
  try {
    const values = await runtimeApi.latestValues(props.device.device_id)
    if (!active || !opened.value) return
    points.value = values
    loaded.value = true
    error.value = ''
  } catch (cause) {
    if (active) error.value = String(cause)
  } finally {
    loading.value = false
  }
}
async function inspect(kind: 'device' | 'read' | 'verify' | 'resolve') {
  operation.value = kind
  operationError.value = ''
  result.value = null
  try {
    if (kind === 'device') result.value = await runtimeApi.verifyDevice(props.device.device_id)
    if (kind === 'read') result.value = await runtimeApi.readPoint(props.device.device_id, selectedPoint.value)
    if (kind === 'verify') result.value = await runtimeApi.verifyPoint(props.device.device_id, selectedPoint.value)
    if (kind === 'resolve') result.value = await runtimeApi.resolvePoint(props.device.device_id, selectedPoint.value)
  } catch (cause) {
    operationError.value = String(cause)
  } finally {
    operation.value = ''
  }
}
watch([opened, () => props.device.device_id], () => {
  points.value = []
  loaded.value = false
  error.value = ''
  result.value = null
  selectedPoint.value = ''
  if (opened.value) void refresh()
})
onMounted(() => { active = true; timer = setInterval(() => { if (autoRefresh.value && tab.value === 'data') void refresh() }, 1000) })
onUnmounted(() => { active = false; if (timer) clearInterval(timer) })
</script>

<template>
  <el-drawer v-model="opened" :title="device.device_id" size="min(900px, 100%)">
    <el-descriptions :column="2" border>
      <el-descriptions-item label="协议">{{ device.protocol }}</el-descriptions-item>
      <el-descriptions-item label="通信">{{ device.connected ? '正常' : '异常' }}</el-descriptions-item>
      <el-descriptions-item label="连续失败">{{ device.consecutive_failures }}</el-descriptions-item>
      <el-descriptions-item label="最后通信">{{ device.last_seen || '—' }}</el-descriptions-item>
      <el-descriptions-item label="错误" :span="2">{{ device.last_error || '—' }}</el-descriptions-item>
    </el-descriptions>
    <el-tabs v-model="tab">
      <el-tab-pane label="实时数据" name="data">
        <el-space wrap>
          <DataSourceBadge :state="state" :reason="error" />
          <el-input v-model="search" clearable placeholder="按测点 ID 筛选" style="width: 220px" />
          <el-switch v-model="autoRefresh" active-text="自动刷新（1秒）" />
          <el-button :loading="loading" @click="refresh">刷新</el-button>
        </el-space>
        <el-table :data="state === 'valid' ? paged : []" style="width: 100%; margin-top: 12px">
          <el-table-column prop="point_id" label="测点" min-width="150" />
          <el-table-column label="值" min-width="160"><template #default="{ row }">{{ JSON.stringify(row.value ?? null) }}</template></el-table-column>
          <el-table-column prop="quality" label="质量" />
          <el-table-column prop="timestamp" label="时间戳" min-width="170" />
        </el-table>
        <el-pagination v-if="filtered.length > pageSize && state === 'valid'"
          v-model:current-page="page" v-model:page-size="pageSize" :total="filtered.length"
          :page-sizes="[50, 100, 200]" layout="total, sizes, prev, pager, next" />
      </el-tab-pane>
      <el-tab-pane label="读取与校验" name="diagnostics">
        <el-space wrap>
          <el-input v-model="selectedPoint" placeholder="测点 ID" style="width: 220px" />
          <el-button :loading="operation === 'device'" :disabled="!!operation" @click="inspect('device')">验证设备</el-button>
          <el-button :loading="operation === 'read'" :disabled="!!operation || !selectedPoint.trim()" @click="inspect('read')">读取测点</el-button>
          <el-button :loading="operation === 'verify'" :disabled="!!operation || !selectedPoint.trim()" @click="inspect('verify')">校验测点</el-button>
          <el-button :loading="operation === 'resolve'" :disabled="!!operation || !selectedPoint.trim()" @click="inspect('resolve')">解析地址</el-button>
        </el-space>
        <el-alert v-if="operationError" type="error" :title="operationError" :closable="false" show-icon />
        <pre v-if="result" style="white-space: pre-wrap; overflow-wrap: anywhere">{{ JSON.stringify(result, null, 2) }}</pre>
      </el-tab-pane>
      <el-tab-pane label="实时趋势" name="trend">
        <DeviceTrendPanel :device-id="device.device_id" :active="opened && tab === 'trend'" />
      </el-tab-pane>
      <el-tab-pane label="控制下发" name="command">
        <DeviceCommandPanel :device-id="device.device_id" />
      </el-tab-pane>
    </el-tabs>
  </el-drawer>
</template>
