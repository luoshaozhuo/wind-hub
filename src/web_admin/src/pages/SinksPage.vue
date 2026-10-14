<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import AdminPageHeading from '../components/AdminPageHeading.vue'
import DataSourceBadge from '../components/DataSourceBadge.vue'
import { dataSourceState } from '../domain/dataSourceState'
import { runtimeApi, type RuntimeSink } from '../api/runtime'

const sinks = ref<RuntimeSink[]>([])
const busy = ref('')
const apiKey = ref('')
const error = ref('')
const loading = ref(false)
const loaded = ref(false)
const filter = ref('')
const selected = ref<RuntimeSink | null>(null)
const drawerOpen = ref(false)
const lastCheck = ref<Record<string, { success: boolean; message: string }>>({})
let active = true
let timer: ReturnType<typeof setInterval> | undefined
const state = computed(() => dataSourceState({
  isPending: loading.value && !loaded.value,
  isError: !!error.value,
  hasData: loaded.value,
}))
const filtered = computed(() => sinks.value.filter(s =>
  (s.name + ' ' + s.message).toLowerCase().includes(filter.value.toLowerCase()),
))
async function refresh() {
  if (loading.value) return
  loading.value = true
  try {
    const values = await runtimeApi.sinks()
    if (!active) return
    sinks.value = values
    loaded.value = true
    error.value = ''
    if (selected.value) selected.value = values.find(s => s.name === selected.value?.name) || null
  } catch (cause) {
    if (active) error.value = String(cause)
  } finally { loading.value = false }
}
function openSink(sink: RuntimeSink) {
  selected.value = sink
  drawerOpen.value = true
}
async function verify(name: string) {
  if (busy.value) return
  busy.value = name
  try {
    const result = await runtimeApi.verifySink(name)
    lastCheck.value[name] = result
    ElMessage({ message: result.message || (result.success ? '校验通过' : '校验失败'), type: result.success ? 'success' : 'warning' })
    await refresh()
  } catch (cause) { ElMessage.error(String(cause)) }
  finally { busy.value = '' }
}
async function writeTest(name: string) {
  if (busy.value || !apiKey.value) return
  try {
    await ElMessageBox.confirm(`确认对 Sink ${name} 执行测试写入？测试可能产生实际下游数据。`, '写入确认', {
      type: 'warning', confirmButtonText: '确认写入', cancelButtonText: '取消',
    })
  } catch { return }
  busy.value = name
  try {
    const result = await runtimeApi.testSink(name, apiKey.value)
    ElMessage({ message: result.message || '请求完成', type: result.success ? 'success' : 'warning' })
    await refresh()
  } catch (cause) { ElMessage.error(String(cause)) }
  finally { busy.value = '' }
}
onMounted(() => { active = true; void refresh(); timer = setInterval(() => void refresh(), 5000) })
onUnmounted(() => { active = false; if (timer) clearInterval(timer) })
</script>

<template>
  <section class="runtime-page">
    <AdminPageHeading title="数据输出" description="Collector Sink 健康、队列与下游写入诊断；每 5 秒自动更新">
      <template #actions><DataSourceBadge :state="state" :reason="error" /><el-button :loading="loading" @click="refresh">刷新</el-button></template>
    </AdminPageHeading>
    <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" />
    <el-space wrap>
      <el-input v-model="filter" clearable placeholder="筛选 Sink 或状态" style="width: 260px" />
      <el-input v-model="apiKey" type="password" show-password autocomplete="off" placeholder="控制 API Key（不持久化）" style="width: 270px" />
    </el-space>
    <el-table :data="state === 'valid' ? filtered : []" v-loading="loading" style="width: 100%; margin-top: 12px" @row-click="openSink">
      <el-table-column prop="name" label="Sink" min-width="160" />
      <el-table-column label="健康" min-width="100"><template #default="{row}"><el-tag :type="row.healthy ? 'success' : 'danger'">{{ row.healthy ? '正常' : '异常' }}</el-tag></template></el-table-column>
      <el-table-column prop="queue_depth" label="队列深度" min-width="120" />
      <el-table-column prop="message" label="状态描述" min-width="200" show-overflow-tooltip />
      <el-table-column label="操作" width="220">
        <template #default="{row}">
          <el-button link type="primary" @click.stop="openSink(row)">详情</el-button>
          <el-button size="small" :disabled="!!busy" :loading="busy === row.name" @click.stop="verify(row.name)">校验</el-button>
          <el-button size="small" :disabled="!apiKey || !!busy" @click.stop="writeTest(row.name)">测试写入</el-button>
        </template>
      </el-table-column>
    </el-table>
  </section>
  <el-drawer v-model="drawerOpen" :title="'Sink 详情 · ' + (selected?.name || '')" size="min(680px, 100%)">
    <el-descriptions v-if="selected" :column="1" border>
      <el-descriptions-item label="名称">{{ selected.name }}</el-descriptions-item>
      <el-descriptions-item label="健康">{{ selected.healthy ? '正常' : '异常' }}</el-descriptions-item>
      <el-descriptions-item label="队列深度">{{ selected.queue_depth }}</el-descriptions-item>
      <el-descriptions-item label="状态">{{ selected.message }}</el-descriptions-item>
    </el-descriptions>
    <el-alert v-if="selected && lastCheck[selected.name]" style="margin-top: 12px" :type="lastCheck[selected.name].success ? 'success' : 'warning'"
      :title="lastCheck[selected.name].message || '校验已完成'" :closable="false" />
  </el-drawer>
</template>
