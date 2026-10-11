<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { runtimeApi, type RuntimeTask, type RuntimeInstance } from '../api/runtime'

const tasks = ref<RuntimeTask[]>([])
const instances = ref<RuntimeInstance[]>([])
const busy = ref('')
const error = ref('')
const apiKey = ref('')

async function refresh() {
  try {
    error.value = ''
    ;[tasks.value, instances.value] = await Promise.all([runtimeApi.tasks(), runtimeApi.instances()])
  } catch (cause) {
    error.value = String(cause)
  }
}
async function toggle(id: string, start: boolean) {
  busy.value = id
  try {
    if (start) await runtimeApi.startTask(id, apiKey.value)
    else await runtimeApi.stopTask(id, apiKey.value)
    ElMessage.success('Operation completed')
    await refresh()
  } catch (cause) {
    ElMessage.error(String(cause))
  } finally {
    busy.value = ''
  }
}
onMounted(() => void refresh())
</script>

<template>
  <section class="runtime-page">
    <h2>Collector Tasks</h2>
    <el-alert v-if="error" :title="error" type="error" show-icon />
    <el-space wrap>
      <el-input v-model="apiKey" type="password" show-password placeholder="Control API Key (not saved)" />
      <el-button @click="refresh">Refresh</el-button>
    </el-space>
    <el-table :data="tasks" style="width: 100%">
      <el-table-column prop="task_id" label="Task" />
      <el-table-column prop="point_group" label="Point Group" />
      <el-table-column prop="runtime_state" label="State" />
      <el-table-column prop="instance_count" label="Instances" />
      <el-table-column label="Control" width="180">
        <template #default="{ row }">
          <el-button size="small" :loading="busy === row.task_id" :disabled="!apiKey" @click="toggle(row.task_id, true)">Start</el-button>
          <el-button size="small" :loading="busy === row.task_id" :disabled="!apiKey" @click="toggle(row.task_id, false)">Stop</el-button>
        </template>
      </el-table-column>
    </el-table>
    <h3>Task Instances</h3>
    <el-table :data="instances">
      <el-table-column prop="instance_id" label="Instance" />
      <el-table-column prop="task_id" label="Task" />
      <el-table-column prop="device_id" label="Device" />
      <el-table-column prop="state" label="State" />
    </el-table>
  </section>
</template>
