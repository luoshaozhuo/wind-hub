<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import { runtimeApi, type RuntimeDevice } from '../../api/runtime'

const props = defineProps<{ device: RuntimeDevice; active: boolean }>()
const result = ref<Record<string, unknown> | null>(null)
const error = ref('')
const loading = ref(false)
let generation = 0

async function load() {
  if (!props.active || loading.value) return
  const current = ++generation
  loading.value = true
  error.value = ''
  try {
    const info = await runtimeApi.verifyDevice(props.device.device_id)
    if (current !== generation || !props.active) return
    result.value = info
  } catch (cause) {
    if (current === generation) error.value = String(cause)
  } finally { loading.value = false }
}
watch([() => props.device.device_id, () => props.active], () => {
  generation++
  result.value = null
  error.value = ''
  if (props.active) void load()
})
onMounted(() => { if (props.active) void load() })
</script>

<template>
  <section>
    <el-alert type="info" :closable="false" title="此处为设备运行配置摘要和连接校验结果。当前 Web Server 不提供设备配置 CRUD，不能在这里修改持久化配置。" />
    <el-descriptions :column="1" border style="margin: 12px 0">
      <el-descriptions-item label="设备 ID">{{ device.device_id }}</el-descriptions-item>
      <el-descriptions-item label="协议">{{ device.protocol }}</el-descriptions-item>
      <el-descriptions-item label="连接状态">{{ device.connected ? '连接正常' : '连接异常' }}</el-descriptions-item>
      <el-descriptions-item label="连续失败">{{ device.consecutive_failures }}</el-descriptions-item>
      <el-descriptions-item label="最近通信">{{ device.last_seen || '—' }}</el-descriptions-item>
      <el-descriptions-item label="错误">{{ device.last_error || '—' }}</el-descriptions-item>
    </el-descriptions>
    <el-button :loading="loading" @click="load">重新校验连接</el-button>
    <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" />
    <el-card v-if="result" shadow="never" style="margin-top: 12px">
      <template #header>Commander 校验结果</template>
      <pre style="white-space: pre-wrap; overflow-wrap: anywhere">{{ JSON.stringify(result, null, 2) }}</pre>
    </el-card>
  </section>
</template>
