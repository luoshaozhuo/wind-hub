<script setup lang="ts">
import { onMounted, ref } from 'vue'
import AdminPageHeading from '../components/AdminPageHeading.vue'
import DeviceDetailDrawer from '../components/devices/DeviceDetailDrawer.vue'
import { runtimeApi, type RuntimeDevice } from '../api/runtime'

const devices = ref<RuntimeDevice[]>([])
const selected = ref<RuntimeDevice | null>(null)
const drawerOpen = ref(false)
const error = ref('')
const loading = ref(false)

async function refresh() {
  if (loading.value) return
  loading.value = true
  error.value = ''
  try {
    devices.value = await runtimeApi.devices()
    if (selected.value) {
      selected.value = devices.value.find(d => d.device_id === selected.value?.device_id) || null
      if (!selected.value) drawerOpen.value = false
    }
  } catch (cause) {
    error.value = String(cause)
  } finally {
    loading.value = false
  }
}
function openDevice(device: RuntimeDevice) {
  selected.value = device
  drawerOpen.value = true
}
onMounted(() => void refresh())
</script>

<template>
  <section class="runtime-page">
    <AdminPageHeading title="设备管理" description="设备在线状态、通信异常及实时采集数据">
      <template #actions><el-button :loading="loading" @click="refresh">刷新</el-button></template>
    </AdminPageHeading>
    <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" />
    <el-table :data="devices" v-loading="loading" style="width: 100%" @row-click="openDevice">
      <el-table-column prop="device_id" label="设备" min-width="160" />
      <el-table-column prop="protocol" label="协议" min-width="110" />
      <el-table-column label="通信" min-width="100">
        <template #default="{ row }">
          <el-tag :type="row.connected ? 'success' : 'danger'">{{ row.connected ? '正常' : '异常' }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="consecutive_failures" label="连续失败" min-width="100" />
      <el-table-column prop="last_seen" label="最后通信" min-width="160" />
      <el-table-column prop="last_error" label="错误信息" min-width="180" show-overflow-tooltip />
      <el-table-column label="操作" width="90">
        <template #default="{ row }"><el-button link type="primary" @click.stop="openDevice(row)">详情</el-button></template>
      </el-table-column>
    </el-table>
    <DeviceDetailDrawer v-if="selected" :key="selected.device_id" v-model="drawerOpen" :device="selected" />
  </section>
</template>
