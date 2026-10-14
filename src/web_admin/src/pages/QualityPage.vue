<script setup lang="ts">
import AdminPageHeading from '../components/AdminPageHeading.vue'
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { runtimeApi, type LatestPointValue } from '../api/runtime'

const points = ref<LatestPointValue[]>([])
const filter = ref('')
const error = ref('')
const loading = ref(false)
let timer: ReturnType<typeof setInterval> | undefined
let active = true
const groups = computed(() => {
  const counts = new Map<string, number>()
  for (const point of points.value) {
    const status = point.quality || '(未提供)'
    counts.set(status, (counts.get(status) || 0) + 1)
  }
  return [...counts.entries()].map(([quality, count]) => ({ quality, count }))
})
const visible = computed(() => points.value.filter(p =>
  !filter.value || [p.device_id, p.point_id, p.quality].join(' ').toLowerCase().includes(filter.value.toLowerCase()),
))
async function refresh() {
  if (loading.value) return
  loading.value = true
  try {
    const values = await runtimeApi.latestValues()
    if (!active) return
    points.value = values
    error.value = ''
  } catch (cause) {
    if (active) error.value = String(cause)
  } finally {
    loading.value = false
  }
}
onMounted(() => {
  active = true
  void refresh()
  timer = setInterval(() => void refresh(), 5000)
})
onUnmounted(() => { active = false; if (timer) clearInterval(timer) })
</script>

<template>
  <section class="runtime-page">
    <AdminPageHeading title="采集质量" description="基于 Collector 实际返回数据的质量分布" />
    <p>按 Collector 返回的 quality 原始值统计，仅覆盖当前最新值记录；未上报的测点不计入分母，不能据此推断全场测点健康率。</p>
    <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" />
    <el-row :gutter="16">
      <el-col :xs="24" :sm="12" :lg="8">
        <el-card shadow="never"><el-statistic title="最新值记录数" :value="points.length" /></el-card>
      </el-col>
      <el-col :xs="24" :sm="12" :lg="16">
        <el-card shadow="never">
          <template #header>质量字段分布</template>
          <el-space wrap>
            <el-tag v-for="item in groups" :key="item.quality" type="info">{{ item.quality }}：{{ item.count }}</el-tag>
            <span v-if="!groups.length">没有最新值记录</span>
          </el-space>
        </el-card>
      </el-col>
    </el-row>
    <el-input v-model="filter" clearable placeholder="筛选设备 / 测点 / 质量" style="max-width: 340px; margin: 16px 0" />
    <el-table :data="visible" v-loading="loading" style="width: 100%">
      <el-table-column prop="device_id" label="设备" min-width="150" />
      <el-table-column prop="point_id" label="测点" min-width="150" />
      <el-table-column prop="quality" label="质量" min-width="110" />
      <el-table-column prop="source" label="来源" min-width="100" />
      <el-table-column prop="timestamp" label="时间戳" min-width="180" />
      <el-table-column prop="timestamp_source" label="时间戳来源" min-width="130" />
    </el-table>
  </section>
</template>
