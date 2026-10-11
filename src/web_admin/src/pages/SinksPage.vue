<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { runtimeApi, type RuntimeSink } from '../api/runtime'
const sinks = ref<RuntimeSink[]>([])
const busy = ref('')
const apiKey = ref('')
const error = ref('')
async function refresh() {
  try {
    error.value = ''
    sinks.value = await runtimeApi.sinks()
  } catch (cause) { error.value = String(cause) }
}
async function verify(name: string) {
  busy.value = name
  try {
    const result = await runtimeApi.verifySink(name)
    ElMessage({ message: result.message || (result.success ? 'Healthy' : 'Failed'), type: result.success ? 'success' : 'warning' })
    await refresh()
  } catch (cause) { ElMessage.error(String(cause)) }
  finally { busy.value = '' }
}
async function writeTest(name: string) {
  busy.value = name
  try {
    const result = await runtimeApi.testSink(name, apiKey.value)
    ElMessage({ message: result.message || 'Completed', type: result.success ? 'success' : 'warning' })
    await refresh()
  } catch (cause) { ElMessage.error(String(cause)) }
  finally { busy.value = '' }
}
onMounted(() => void refresh())
</script>

<template>
  <section class="runtime-page">
    <h2>Collector Sinks</h2>
    <el-alert v-if="error" :title="error" type="error" show-icon />
    <el-space wrap>
      <el-input v-model="apiKey" type="password" show-password placeholder="Control API Key (not saved)" />
      <el-button @click="refresh">Refresh</el-button>
    </el-space>
    <el-table :data="sinks">
      <el-table-column prop="name" label="Name" />
      <el-table-column label="Healthy"><template #default="{row}">{{ row.healthy ? 'Yes' : 'No' }}</template></el-table-column>
      <el-table-column prop="message" label="Status" />
      <el-table-column prop="queue_depth" label="Queue" />
      <el-table-column label="Diagnostics" width="220">
        <template #default="{row}">
          <el-button size="small" :loading="busy === row.name" @click="verify(row.name)">Verify</el-button>
          <el-button size="small" :loading="busy === row.name" :disabled="!apiKey" @click="writeTest(row.name)">Write Test</el-button>
        </template>
      </el-table-column>
    </el-table>
  </section>
</template>
