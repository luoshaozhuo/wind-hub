<script setup lang="ts">
import { ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { runtimeApi } from '../../api/runtime'

const props = defineProps<{ deviceId: string }>()
const pointId = ref('')
const valueType = ref<'number' | 'boolean' | 'string'>('number')
const rawValue = ref('')
const apiKey = ref('')
const timeout = ref(1)
const busy = ref(false)

async function submit() {
  if (busy.value) return
  const point = pointId.value.trim()
  if (!point || !apiKey.value || !rawValue.value.trim()) return
  const raw = rawValue.value.trim()
  let value: number | boolean | string
  if (valueType.value === 'number') {
    value = Number(raw)
    if (!Number.isFinite(value)) { ElMessage.error('数值必须是有限数字'); return }
  } else if (valueType.value === 'boolean') {
    if (raw !== 'true' && raw !== 'false') { ElMessage.error('布尔值只能是 true 或 false'); return }
    value = raw === 'true'
  } else value = rawValue.value
  try {
    await ElMessageBox.confirm(
      `确认向设备 ${props.deviceId} 的测点 ${point} 下发值 ${String(value)}？命令不会自动重试。`,
      '确认设备写入',
      { type: 'warning', confirmButtonText: '确认下发', cancelButtonText: '取消' },
    )
  } catch { return }
  busy.value = true
  try {
    const result = await runtimeApi.writePoint({
      command_id: crypto.randomUUID(),
      device_id: props.deviceId,
      point_id: point,
      value,
      timeout: timeout.value,
    }, apiKey.value)
    ElMessage({ type: result.success ? 'success' : 'error', message: result.error || (result.success ? '写入成功' : '写入失败') })
  } catch (cause) {
    ElMessage.error(String(cause))
  } finally { busy.value = false }
}
</script>

<template>
  <el-alert type="warning" :closable="false" show-icon title="设备写入为独立操作。网络超时或结果不明时，不要直接重复下发相同命令。" />
  <el-form label-position="top" style="margin-top: 16px; max-width: 540px">
    <el-form-item label="测点 ID"><el-input v-model="pointId" /></el-form-item>
    <el-form-item label="数据类型">
      <el-radio-group v-model="valueType">
        <el-radio value="number">Number</el-radio>
        <el-radio value="boolean">Boolean</el-radio>
        <el-radio value="string">String</el-radio>
      </el-radio-group>
    </el-form-item>
    <el-form-item label="写入值"><el-input v-model="rawValue" placeholder="输入要写入的值" /></el-form-item>
    <el-form-item label="操作超时（秒）"><el-input-number v-model="timeout" :min="0.1" :max="30" :step="0.5" /></el-form-item>
    <el-form-item label="控制 API Key"><el-input v-model="apiKey" type="password" show-password autocomplete="off" /></el-form-item>
    <el-button type="danger" :loading="busy" :disabled="busy || !pointId.trim() || !rawValue.trim() || !apiKey" @click="submit">确认并下发</el-button>
  </el-form>
</template>
