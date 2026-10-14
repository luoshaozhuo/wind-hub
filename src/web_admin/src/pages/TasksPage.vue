<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import AdminPageHeading from '../components/AdminPageHeading.vue'
import DataSourceBadge from '../components/DataSourceBadge.vue'
import { dataSourceState } from '../domain/dataSourceState'
import { runtimeApi, type RuntimeTask, type RuntimeInstance } from '../api/runtime'

const tasks = ref<RuntimeTask[]>([])
const instances = ref<RuntimeInstance[]>([])
const busy = ref('')
const error = ref('')
const apiKey = ref('')
const search = ref('')
const loaded = ref(false)
const loading = ref(false)
const selectedTask = ref('')
const drawerOpen = ref(false)
let active = true
let timer: ReturnType<typeof setInterval> | undefined

const filtered = computed(() => tasks.value.filter(t =>
  (t.task_id + ' ' + t.point_group + ' ' + t.runtime_state).toLowerCase().includes(search.value.toLowerCase()),
))
const selectedInstances = computed(() => instances.value.filter(i => i.task_id === selectedTask.value))
const availability = computed(() => dataSourceState({
  isPending: loading.value && !loaded.value,
  isError: !!error.value,
  hasData: loaded.value,
}))

async function refresh() {
  if (loading.value) return
  loading.value = true
  try {
    const [nextTasks, nextInstances] = await Promise.all([runtimeApi.tasks(), runtimeApi.instances()])
    if (!active) return
    tasks.value = nextTasks
    instances.value = nextInstances
    loaded.value = true
    error.value = ''
  } catch (cause) {
    if (active) error.value = String(cause)
  } finally { loading.value = false }
}
function openTask(task: RuntimeTask) {
  selectedTask.value = task.task_id
  drawerOpen.value = true
}
async function control(id: string, scope: 'task' | 'instance', start: boolean) {
  if (busy.value || !apiKey.value) return
  const verb = start ? '启动' : '停止'
  try {
    await ElMessageBox.confirm(`确定${verb}${scope === 'task' ? '任务' : '实例'} ${id}？`, '操作确认', {
      type: 'warning', confirmButtonText: verb, cancelButtonText: '取消',
    })
  } catch { return }
  busy.value = scope + ':' + id
  try {
    const result = scope === 'task'
      ? (start ? await runtimeApi.startTask(id, apiKey.value) : await runtimeApi.stopTask(id, apiKey.value))
      : (start ? await runtimeApi.startInstance(id, apiKey.value) : await runtimeApi.stopInstance(id, apiKey.value))
    ElMessage.success(`请求已完成：${String((result as {runtime_state?: string; state?: string}).runtime_state || (result as {state?: string}).state || id)}`)
    await refresh()
  } catch (cause) { ElMessage.error(String(cause)) }
  finally { busy.value = '' }
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
    <AdminPageHeading title="采集任务" description="Collector 任务与实例运行状态；每 5 秒更新">
      <template #actions><DataSourceBadge :state="availability" :reason="error" /><el-button :loading="loading" @click="refresh">刷新</el-button></template>
    </AdminPageHeading>
    <el-alert v-if="error" type="error" :title="error" show-icon :closable="false" />
    <el-space wrap>
      <el-input v-model="search" clearable placeholder="筛选任务、点组或状态" style="width: 260px" />
      <el-input v-model="apiKey" type="password" show-password autocomplete="off" placeholder="控制 API Key（不持久化）" style="width: 270px" />
    </el-space>
    <el-table :data="availability === 'valid' ? filtered : []" v-loading="loading" style="width: 100%; margin-top: 12px" @row-click="openTask">
      <el-table-column prop="task_id" label="任务" min-width="160" />
      <el-table-column prop="point_group" label="测点组" min-width="120" />
      <el-table-column prop="runtime_state" label="运行状态" min-width="120" />
      <el-table-column prop="instance_count" label="实例数" min-width="100" />
      <el-table-column label="操作" width="220">
        <template #default="{row}">
          <el-button link @click.stop="openTask(row)">详情</el-button>
          <el-button size="small" :disabled="!apiKey || !!busy" :loading="busy === 'task:' + row.task_id" @click.stop="control(row.task_id, 'task', true)">启动</el-button>
          <el-button size="small" :disabled="!apiKey || !!busy" @click.stop="control(row.task_id, 'task', false)">停止</el-button>
        </template>
      </el-table-column>
    </el-table>
  </section>
  <el-drawer v-model="drawerOpen" :title="'任务详情 · ' + selectedTask" size="min(760px, 100%)">
    <el-descriptions :column="1" border>
      <el-descriptions-item label="任务 ID">{{ selectedTask }}</el-descriptions-item>
      <el-descriptions-item label="实例数">{{ selectedInstances.length }}</el-descriptions-item>
    </el-descriptions>
    <h3>任务实例</h3>
    <el-table :data="selectedInstances" style="width: 100%">
      <el-table-column prop="instance_id" label="实例" min-width="150" />
      <el-table-column prop="device_id" label="设备" min-width="120" />
      <el-table-column prop="point_group" label="测点组" min-width="120" />
      <el-table-column prop="state" label="状态" min-width="100" />
      <el-table-column label="操作" width="145">
        <template #default="{row}">
          <el-button link type="primary" :disabled="!apiKey || !!busy" @click="control(row.instance_id, 'instance', true)">启动</el-button>
          <el-button link type="danger" :disabled="!apiKey || !!busy" @click="control(row.instance_id, 'instance', false)">停止</el-button>
        </template>
      </el-table-column>
    </el-table>
  </el-drawer>
</template>
