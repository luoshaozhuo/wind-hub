<script setup lang="ts">
// Point 连通性测试 panel：设备选择、请求预览、Test Read 与候选解码。
// 状态编排在 usePointReadTest；本组件只是它的宿主。
import { computed, watch } from 'vue'
import { usePointReadTest } from '../../composables/usePointReadTest'
import type { PointAddress, Protocol } from '../../domain/types'

const props = defineProps<{
  protocol: Protocol
  address: PointAddress
  requestText: string
  /** 打开新草稿时递增：触发设备候选与结果重置。 */
  resetKey: number
}>()

const protocolRef = computed(() => props.protocol)
const addressRef = computed(() => props.address)
const requestTextRef = computed(() => props.requestText)

const {
  testDeviceId,
  testLoading,
  testError,
  testCandidates,
  testLatency,
  testDevices,
  selectedTestDevice,
  reset,
  run,
} = usePointReadTest(protocolRef, addressRef, requestTextRef)

watch(
  () => props.resetKey,
  () => reset(),
  { immediate: true },
)
</script>

<template>
  <el-card shadow="never" class="point-test-card">
    <div class="point-editor-heading">
      <h3>Connectivity Test</h3>
      <p>Select a {{ protocol.toUpperCase() }} device and test the current unsaved definition.</p>
    </div>

    <el-form label-position="top">
      <el-form-item label="Device">
        <el-select
          v-model="testDeviceId"
          :disabled="testLoading"
          placeholder="Select device"
          class="app-full-width"
        >
          <el-option
            v-for="d in testDevices"
            :key="d.device_id"
            :label="d.device_id + ' · ' + d.host"
            :value="d.device_id"
          >
            <span>{{ d.device_id }} · {{ d.host }}</span>
            <span class="device-state">{{ d.online ? 'online' : 'offline' }}</span>
          </el-option>
        </el-select>
      </el-form-item>
    </el-form>

    <el-descriptions :column="1" size="small" border class="test-request">
      <el-descriptions-item label="Protocol">{{ protocol.toUpperCase() }}</el-descriptions-item>
      <el-descriptions-item label="Request">{{ requestText }}</el-descriptions-item>
      <el-descriptions-item v-if="selectedTestDevice" label="Target">
        {{ selectedTestDevice.host }}:{{ selectedTestDevice.port || '—' }}
      </el-descriptions-item>
    </el-descriptions>

    <div class="test-actions">
      <el-button type="primary" :loading="testLoading" :disabled="!testDeviceId" @click="run">
        Test Read
      </el-button>
      <span v-if="testLatency && !testLoading" class="muted">{{ testLatency }} ms</span>
    </div>

    <el-alert v-if="testError" :title="testError" type="error" :closable="false" show-icon />

    <div class="test-results-title">Interpretations</div>
    <el-table :data="testCandidates" size="small" class="test-results-table">
      <el-table-column prop="type" label="Type" width="96" />
      <el-table-column prop="value" label="Value" min-width="120" />
    </el-table>
  </el-card>
</template>

<style scoped>
.point-test-card {
  min-width: 0;
  height: auto;
}
.point-editor-heading {
  margin-bottom: var(--app-space-4);
}
.point-editor-heading h3 {
  margin: 0;
  color: var(--app-text-primary);
  font-size: var(--app-font-section-title);
  font-weight: var(--app-font-weight-semibold);
}
.point-editor-heading p {
  margin: var(--app-space-1) 0 0;
  color: var(--app-text-muted);
  font-size: var(--app-font-caption);
  line-height: var(--app-line-height-compact);
}
.test-request {
  margin-top: var(--app-space-2);
}
.test-actions {
  display: flex;
  align-items: center;
  gap: var(--app-space-2);
  margin-top: var(--app-space-3);
  margin-bottom: var(--app-space-3);
}
.test-results-title {
  margin: var(--app-space-3) 0 var(--app-space-2);
  color: var(--app-text-primary);
  font-size: var(--app-font-body);
  font-weight: var(--app-font-weight-semibold);
}
.test-results-table {
  width: 100%;
}
.device-state {
  float: right;
  margin-left: var(--app-space-3);
  color: var(--app-text-muted);
  font-size: var(--app-font-caption);
}
.muted {
  color: var(--app-text-muted);
  font-size: var(--app-font-label);
}
@media (max-width: 1199px) {
  .point-test-card {
    margin-top: var(--app-space-4);
  }
}
</style>
