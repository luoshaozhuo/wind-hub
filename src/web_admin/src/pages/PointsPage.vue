<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { runtimeApi, type LatestPointValue, type RuntimeDevice } from '../api/runtime'
const devices = ref<RuntimeDevice[]>([])
const points = ref<LatestPointValue[]>([])
const selected = ref('')
const query = ref('')
const error = ref('')
const loading = ref(false)

const filtered = computed(() => points.value.filter(p =>
  (!selected.value || p.device_id === selected.value) &&
  (!query.value || (p.device_id + ' ' + p.point_id).toLowerCase().includes(query.value.toLowerCase())),
))
async function refresh() {
  loading.value = true
  error.value = ''
  try {
    const [nextDevices, nextPoints] = await Promise.all([
      runtimeApi.devices(), runtimeApi.latestValues(selected.value || undefined),
    ])
    devices.value = nextDevices
    points.value = nextPoints
  } catch (cause) {
    error.value = String(cause)
  } finally {
    loading.value = false
  }
}
onMounted(() => void refresh())
</script>

<template>
  <section class="runtime-page">
    <h2>测点浏览</h2>
    <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" />
    <el-space wrap>
      <el-select v-model="selected" placeholder="全部设备" clearable style="width: 220px" @change="refresh">
        <el-option v-for="device in devices" :key="device.device_id" :value="device.device_id" :label="device.device_id" />
      </el-select>
      <el-input v-model="query" clearable placeholder="筛选设备或测点 ID" style="width: 240px" />
      <el-button :loading="loading" @click="refresh">刷新</el-button>
    </el-space>
    <el-table :data="filtered" v-loading="loading" style="width: 100%">
      <el-table-column prop="device_id" label="设备" min-width="140" />
      <el-table-column prop="point_id" label="测点" min-width="160" />
      <el-table-column label="值" min-width="180">
        <template #default="{ row }">{{ JSON.stringify(row.value ?? null) }}</template>
      </el-table-column>
      <el-table-column prop="quality" label="质量" min-width="100" />
      <el-table-column prop="timestamp" label="时间戳" min-width="170" />
      <el-table-column prop="timestamp_source" label="时间戳来源" min-width="120" />
    </el-table>
    <p>仅显示 Collector 已发布的最新值；这里不代表完整点表配置。</p>
  </section>
</template>
