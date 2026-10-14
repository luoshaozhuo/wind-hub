<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { runtimeApi, type RuntimeDevice } from '../api/runtime'

const devices = ref<RuntimeDevice[]>([])
const deviceId = ref('')
const pointId = ref('')
const pointGroup = ref('')
const pointIds = ref('')
const action = ref('')
const error = ref('')
const result = ref<Record<string, unknown> | null>(null)
const loadingDevices = ref(false)

async function loadDevices() {
  loadingDevices.value = true
  try {
    devices.value = await runtimeApi.devices()
    if (!deviceId.value && devices.value.length) deviceId.value = devices.value[0].device_id
  } catch (cause) {
    error.value = String(cause)
  } finally {
    loadingDevices.value = false
  }
}
async function execute(operation: 'device' | 'point' | 'resolve' | 'group' | 'read') {
  if (!deviceId.value) { error.value = '请选择设备'; return }
  if (['point', 'resolve'].includes(operation) && !pointId.value.trim()) {
    error.value = '请输入测点 ID'
    return
  }
  const ids = [...new Set(pointIds.value.split(/[\s,，;；]+/).map(s => s.trim()).filter(Boolean))]
  if (operation === 'read' && !ids.length) { error.value = '请输入至少一个测点 ID'; return }
  action.value = operation
  error.value = ''
  result.value = null
  try {
    if (operation === 'device') result.value = await runtimeApi.verifyDevice(deviceId.value)
    if (operation === 'point') result.value = await runtimeApi.verifyPoint(deviceId.value, pointId.value.trim())
    if (operation === 'resolve') result.value = await runtimeApi.resolvePoint(deviceId.value, pointId.value.trim())
    if (operation === 'group') result.value = await runtimeApi.verifyPoints(deviceId.value, pointGroup.value.trim())
    if (operation === 'read') result.value = await runtimeApi.readMany(deviceId.value, ids)
  } catch (cause) {
    error.value = String(cause)
  } finally {
    action.value = ''
  }
}
onMounted(() => void loadDevices())
</script>

<template>
  <section class="runtime-page">
    <h2>设备与测点诊断</h2>
    <p>调用 Commander 正式接口执行只读验证、地址解析和批量读取，不执行控制写入。</p>
    <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" />
    <el-form label-position="top">
      <el-form-item label="设备">
        <el-select v-model="deviceId" filterable placeholder="选择设备" style="width: 300px" :loading="loadingDevices">
          <el-option v-for="item in devices" :key="item.device_id" :label="item.device_id" :value="item.device_id" />
        </el-select>
        <el-button style="margin-left: 12px" :loading="loadingDevices" @click="loadDevices">重新获取设备</el-button>
      </el-form-item>
      <el-form-item label="设备整体校验">
        <el-button :loading="action === 'device'" :disabled="!!action || !deviceId" @click="execute('device')">验证设备</el-button>
      </el-form-item>
      <el-form-item label="单个测点 ID">
        <el-input v-model="pointId" placeholder="输入点表中的测点 ID" style="max-width: 360px" />
        <el-button :disabled="!!action || !pointId.trim()" :loading="action === 'point'" @click="execute('point')">校验测点</el-button>
        <el-button :disabled="!!action || !pointId.trim()" :loading="action === 'resolve'" @click="execute('resolve')">解析测点</el-button>
      </el-form-item>
      <el-form-item label="测点组（可为空，交由服务端定义范围）">
        <el-input v-model="pointGroup" placeholder="Point Group" style="max-width: 360px" />
        <el-button :disabled="!!action" :loading="action === 'group'" @click="execute('group')">批量校验</el-button>
      </el-form-item>
      <el-form-item label="批量读取的测点 ID（逗号或换行分隔）">
        <el-input v-model="pointIds" type="textarea" :rows="3" placeholder="point_1, point_2" style="max-width: 560px" />
        <el-button :disabled="!!action || !pointIds.trim()" :loading="action === 'read'" @click="execute('read')">批量读取</el-button>
      </el-form-item>
    </el-form>
    <el-card v-if="result" shadow="never">
      <template #header>服务端返回结果</template>
      <pre style="white-space: pre-wrap; overflow-wrap: anywhere">{{ JSON.stringify(result, null, 2) }}</pre>
    </el-card>
  </section>
</template>
