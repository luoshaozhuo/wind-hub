<script setup lang="ts">
// 即时命令 panel：控制点选择、目标值输入、显式确认写入与结果/回读展示。
// 命令编排在 useDeviceCommand；写入后经 onSent 通知宿主刷新趋势。
import { computed, toRef } from 'vue'
import { useDeviceCommand } from '../../composables/useDeviceCommand'
import type { DeviceDataFeature } from '../../composables/useDeviceData'
import type { DeviceInst } from '../../domain/types'

const props = defineProps<{
  device: DeviceInst
  data: DeviceDataFeature
}>()

const emit = defineEmits<{ sent: [] }>()

const deviceRef = toRef(props, 'device')
const dataRows = computed(() => props.data.dataRows.value)
const resolvedPoints = computed(() => props.data.resolvedPoints.value)

const {
  cmdPoint,
  cmdValue,
  cmdBool,
  sending,
  commandResult,
  controlCandidates,
  currentControlRow,
  cmdIsBool,
  sendCommand,
  formatCommandValue,
} = useDeviceCommand(deviceRef, dataRows, resolvedPoints, { onSent: () => emit('sent') })
</script>

<template>
  <div class="control-column">
    <section class="panel-card">
      <div class="panel-head">
        <div>
          <h3>Command</h3>
          <p>选择控制 Point，确认定义后写入目标值并执行回读。</p>
        </div>
      </div>

      <el-form label-position="top">
        <el-form-item label="Command Point">
          <el-select v-model="cmdPoint" filterable class="app-full-width" :disabled="sending">
            <el-option
              v-for="r in controlCandidates"
              :key="r.point_id"
              :label="`${r.point_id} · ${r.variable_name}`"
              :value="r.point_id"
            />
          </el-select>
        </el-form-item>

        <el-descriptions v-if="currentControlRow" :column="1" border class="control-definition">
          <el-descriptions-item label="Point ID">{{
            currentControlRow.point_id
          }}</el-descriptions-item>
          <el-descriptions-item label="Variable / Address"
            >{{ currentControlRow.variable_name || '—' }} ·
            {{ currentControlRow.address }}</el-descriptions-item
          >
          <el-descriptions-item label="Data Type">{{
            currentControlRow.data_type
          }}</el-descriptions-item>
          <el-descriptions-item label="Scale / Offset"
            >{{ currentControlRow.scale }} / {{ currentControlRow.offset }}</el-descriptions-item
          >
          <el-descriptions-item label="Unit">{{
            currentControlRow.unit || '—'
          }}</el-descriptions-item>
          <el-descriptions-item label="Current"
            >{{ currentControlRow.value }} {{ currentControlRow.unit }}</el-descriptions-item
          >
          <el-descriptions-item label="Updated">{{
            currentControlRow.updated_at
          }}</el-descriptions-item>
        </el-descriptions>

        <el-form-item label="Target Value" class="control-target-field">
          <el-switch
            v-if="cmdIsBool"
            v-model="cmdBool"
            :disabled="sending"
            active-text="true"
            inactive-text="false"
          />
          <el-input-number
            v-else
            v-model="cmdValue"
            :step="1"
            controls-position="right"
            class="app-full-width"
            :disabled="sending"
          />
        </el-form-item>
        <el-button
          type="primary"
          :loading="sending"
          :disabled="!currentControlRow || sending"
          @click="sendCommand"
          class="app-full-width"
          >Send Command</el-button
        >
      </el-form>
    </section>

    <section class="panel-card command-result-panel">
      <div class="panel-head">
        <div>
          <h3>Command Result</h3>
          <p>写入结果与回读值</p>
        </div>
      </div>
      <div v-if="!commandResult" class="empty-state compact">
        No command executed in this session.
      </div>
      <div v-else class="command-result">
        <div>
          <span>Requested</span><b>{{ formatCommandValue(commandResult.requested) }}</b>
        </div>
        <div>
          <span>Sent</span><b>{{ commandResult.sentAt }}</b>
        </div>
        <div>
          <span>Write</span
          ><b :class="commandResult.success ? 'step-success' : 'app-text-fault'">{{
            commandResult.success ? '✓ Success' : '✕ Failed'
          }}</b>
        </div>
        <div v-if="commandResult.error">
          <span>Error</span><b class="app-text-fault">{{ commandResult.error }}</b>
        </div>
        <div>
          <span>Read back</span><b>{{ formatCommandValue(commandResult.readback) }}</b>
        </div>
        <div v-if="commandResult.success && typeof commandResult.requested === 'number'">
          <span>Difference</span
          ><b>{{ (Number(commandResult.readback) - commandResult.requested).toFixed(2) }}</b>
        </div>
        <div>
          <span>Latency</span><b>{{ commandResult.latency }} ms</b>
        </div>
      </div>
    </section>
  </div>
</template>

<style scoped>
.control-column {
  display: grid;
  gap: var(--app-space-4);
  min-width: 0;
}
.control-definition {
  margin-bottom: var(--app-space-4);
}
.control-target-field {
  margin-top: var(--app-space-4);
}
.command-result-panel {
  min-height: 0;
}
</style>
