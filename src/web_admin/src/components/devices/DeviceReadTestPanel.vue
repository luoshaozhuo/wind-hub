<script setup lang="ts">
// 点读测试 panel：对当前设备 Point Table 中已配置的 Point 做单次诊断读。
// 成败与错误码由 backend service 按场景注册表决定（§5）。
import { computed, ref, watch } from 'vue'
import { protocolRead } from '../../api/diagnostics'
import { pointAddressText } from '../../domain/deviceData'
import { unitSymbol } from '../../domain/devices'
import { useConfigStore } from '../../stores/config'
import { useViewport } from '../../composables/useViewport'
import { EMPTY } from '../../utils/format'
import type { DeviceInst, PointDef } from '../../domain/types'

interface PointReadResult {
  state: 'success' | 'failed'
  timestamp: string
  latency_ms: number
  raw_data: string
  decoded_value: string
  engineering_value: string
  error_category: string
  error_code: string
  error_message: string
}

const props = defineProps<{
  device: DeviceInst
  resolvedPoints: PointDef[]
}>()

const configStore = useConfigStore()
const { isMobile } = useViewport()

const readPointId = ref('')
const readLoading = ref(false)
const readResult = ref<PointReadResult | null>(null)
const readPoint = computed(() => props.resolvedPoints.find((p) => p.point_id === readPointId.value))

watch(
  () => props.resolvedPoints,
  (rows) => {
    if (!rows.some((p) => p.point_id === readPointId.value))
      readPointId.value = rows[0]?.point_id || ''
  },
  { immediate: true },
)
watch(readPointId, () => {
  readResult.value = null
})

async function readSelectedPoint() {
  if (!readPoint.value || readLoading.value) return
  readLoading.value = true
  readResult.value = null
  const started = performance.now()
  try {
    const row = await protocolRead(props.device.device_id, readPoint.value.point_id)
    const value = String(row.value ?? EMPTY)
    readResult.value = {
      state: row.quality === 'bad' ? 'failed' : 'success',
      timestamp: row.timestamp,
      latency_ms: Math.round(performance.now() - started),
      raw_data: 'Not exposed by protocol adapter',
      decoded_value: value,
      engineering_value: value,
      error_category: row.quality === 'bad' ? 'quality' : '',
      error_code: row.quality === 'bad' ? 'BAD_QUALITY' : '',
      error_message: '',
    }
  } catch (error) {
    readResult.value = {
      state: 'failed',
      timestamp: new Date().toISOString(),
      latency_ms: Math.round(performance.now() - started),
      raw_data: EMPTY,
      decoded_value: EMPTY,
      engineering_value: EMPTY,
      error_category: 'protocol',
      error_code: 'READ_FAILED',
      error_message: error instanceof Error ? error.message : String(error),
    }
  } finally {
    readLoading.value = false
  }
}
</script>

<template>
  <div class="read-test-layout">
    <section class="panel-card">
      <div class="panel-head">
        <div>
          <h3>Point Read Test</h3>
          <p>只测试当前设备 Point Table 中已经配置的 Point。</p>
        </div>
      </div>
      <el-form label-position="top">
        <el-form-item label="Point">
          <el-select v-model="readPointId" filterable class="app-full-width">
            <el-option
              v-for="p in resolvedPoints"
              :key="p.point_id"
              :label="`${p.point_id} · ${p.variable_name || pointAddressText(p)}`"
              :value="p.point_id"
            />
          </el-select>
        </el-form-item>
      </el-form>

      <el-descriptions v-if="readPoint" :column="isMobile ? 1 : 2" border>
        <el-descriptions-item label="Point ID">{{ readPoint.point_id }}</el-descriptions-item>
        <el-descriptions-item label="Variable">{{
          readPoint.variable_name || '—'
        }}</el-descriptions-item>
        <el-descriptions-item label="Address">{{
          pointAddressText(readPoint)
        }}</el-descriptions-item>
        <el-descriptions-item label="Data Type">{{ readPoint.data_type }}</el-descriptions-item>
        <el-descriptions-item label="Scale / Offset"
          >{{ readPoint.scale }} / {{ readPoint.offset }}</el-descriptions-item
        >
        <el-descriptions-item label="Unit">{{
          unitSymbol(configStore, readPoint.unit) || '—'
        }}</el-descriptions-item>
        <el-descriptions-item label="Groups" :span="2">{{
          readPoint.point_groups.join(', ') || '—'
        }}</el-descriptions-item>
        <el-descriptions-item label="Description" :span="2">{{
          readPoint.description || '—'
        }}</el-descriptions-item>
      </el-descriptions>

      <div class="read-test-actions">
        <el-button
          type="primary"
          :loading="readLoading"
          :disabled="!readPoint"
          @click="readSelectedPoint"
          >Read Once</el-button
        >
      </div>
    </section>

    <section class="panel-card">
      <div class="panel-head">
        <div>
          <h3>Read Result</h3>
          <p>保留 Raw Data、解码结果和协议错误。</p>
        </div>
      </div>
      <el-empty
        v-if="!readResult"
        description="No read executed in this session"
        :image-size="64"
      />
      <template v-else>
        <el-alert
          :type="readResult.state === 'success' ? 'success' : 'error'"
          :closable="false"
          :title="readResult.state === 'success' ? 'Read succeeded' : 'Read failed'"
        />
        <el-descriptions :column="1" border class="read-result-details">
          <el-descriptions-item label="Timestamp">{{ readResult.timestamp }}</el-descriptions-item>
          <el-descriptions-item label="Latency"
            >{{ readResult.latency_ms }} ms</el-descriptions-item
          >
          <el-descriptions-item label="Raw Data"
            ><code>{{ readResult.raw_data }}</code></el-descriptions-item
          >
          <el-descriptions-item label="Decoded">{{
            readResult.decoded_value
          }}</el-descriptions-item>
          <el-descriptions-item label="Engineering">{{
            readResult.engineering_value
          }}</el-descriptions-item>
          <template v-if="readResult.state === 'failed'">
            <el-descriptions-item label="Category">{{
              readResult.error_category
            }}</el-descriptions-item>
            <el-descriptions-item label="Error Code">{{
              readResult.error_code
            }}</el-descriptions-item>
            <el-descriptions-item label="Message">{{
              readResult.error_message
            }}</el-descriptions-item>
          </template>
        </el-descriptions>
      </template>
    </section>
  </div>
</template>

<style scoped>
.read-test-layout {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: var(--app-space-4);
}
.read-test-actions {
  display: flex;
  justify-content: flex-end;
  margin-top: var(--app-space-3);
}
.read-result-details {
  margin-top: var(--app-space-3);
}
.read-result-details code {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: var(--app-font-label);
}
@media (max-width: 1199px) {
  .read-test-layout {
    grid-template-columns: 1fr;
  }
}
</style>
