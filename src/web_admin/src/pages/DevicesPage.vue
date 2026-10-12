<script setup lang="ts">
import { ref } from 'vue'
import { ElMessage } from 'element-plus'
import { runtimeApi, type RuntimeDevice, type LatestPointValue } from '../api/runtime'
import { usePolling } from '../api/polling'
const devices = ref<RuntimeDevice[]>([])
const values = ref<LatestPointValue[]>([])
const selected = ref('')
const error = ref('')
const apiKey = ref('')
const pointId = ref('')
const writeValue = ref('')
const busy = ref(false)
async function refresh() {
  try {
    error.value = ''
    devices.value = await runtimeApi.devices()
    if (selected.value) values.value = await runtimeApi.latestValues(selected.value)
  } catch (cause) {
    error.value = String(cause)
  }
}
async function selectDevice(id: string) {
  selected.value = id
  await refresh()
}
async function verify() {
  try {
    const result = await runtimeApi.verifyDevice(selected.value)
    ElMessage({
      message:
        result.stages.map((s) => s.name + ': ' + s.message).join('; ') ||
        (result.ok ? 'Healthy' : 'Failed'),
      type: result.ok ? 'success' : 'warning',
    })
  } catch (cause) {
    ElMessage.error(String(cause))
  }
}
async function write() {
  busy.value = true
  try {
    if (!pointId.value) throw new Error('Point ID required')
    if (!writeValue.value.trim()) throw new Error('Value required')
    const raw = writeValue.value.trim()
    const value: boolean | number | string =
      raw === 'true'
        ? true
        : raw === 'false'
          ? false
          : /^[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?$/.test(raw)
            ? Number(raw)
            : raw
    const result = await runtimeApi.writePoint(
      {
        command_id: crypto.randomUUID(),
        device_id: selected.value,
        point_id: pointId.value,
        value,
        timeout: 1,
      },
      apiKey.value,
    )
    const suffix = ` (command_id: ${result.command_id})`
    if (result.outcome === 'COMMAND_OUTCOME_CONFIRMED_SUCCESS') {
      ElMessage({ message: 'Command succeeded' + suffix, type: 'success' })
    } else if (result.outcome === 'COMMAND_OUTCOME_UNKNOWN') {
      // UNKNOWN：命令可能已送达或已执行但无确认证据，禁止盲目重试。
      ElMessage({ message: '结果未知，命令可能已执行，请勿盲目重试' + suffix, type: 'warning' })
    } else {
      ElMessage({ message: (result.error || 'Command failed') + suffix, type: 'error' })
    }
    await refresh()
  } catch (cause) {
    ElMessage.error(String(cause))
  } finally {
    busy.value = false
  }
}
usePolling(refresh)
</script>

<template>
  <section class="runtime-page">
    <h2>Devices</h2>
    <el-alert v-if="error" :title="error" type="error" show-icon />
    <el-button @click="refresh">Refresh</el-button>
    <el-table :data="devices" @row-click="(row: RuntimeDevice) => selectDevice(row.device_id)">
      <el-table-column prop="device_id" label="Device" />
      <el-table-column prop="protocol" label="Protocol" />
      <el-table-column label="Connected"
        ><template #default="{ row }">{{ row.connected ? 'Yes' : 'No' }}</template></el-table-column
      >
      <el-table-column prop="last_error" label="Error" />
    </el-table>
    <template v-if="selected">
      <h3>Latest Values — {{ selected }}</h3>
      <el-button @click="verify">Verify Device</el-button>
      <el-table :data="values">
        <el-table-column prop="point_id" label="Point" />
        <el-table-column label="Value"
          ><template #default="{ row }">{{
            JSON.stringify(row.value ?? null)
          }}</template></el-table-column
        >
        <el-table-column prop="quality" label="Quality" />
        <el-table-column prop="timestamp" label="Timestamp" />
      </el-table>
      <h3>Command</h3>
      <el-space wrap>
        <el-input v-model="pointId" placeholder="Point ID" />
        <el-input v-model="writeValue" placeholder="Value" />
        <el-input
          v-model="apiKey"
          type="password"
          show-password
          placeholder="Control API Key (not saved)"
        />
        <el-button type="primary" :disabled="!apiKey || !pointId" :loading="busy" @click="write"
          >Write</el-button
        >
      </el-space>
    </template>
  </section>
</template>
