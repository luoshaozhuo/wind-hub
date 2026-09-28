<script setup lang="ts">
import { computed, onBeforeUnmount, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { store } from '../mock/data'
import type { TaskDef } from '../mock/types'

// 采集任务定义（tasks.yaml）+ mock 运行控制；字段与 CollectionTaskConfig 对齐：
// device / device_group 二选一，point_group 单值，targets 引用 sink 名
const dlg = ref(false)
const editing = ref('')
const viewportWidth = ref(window.innerWidth)
const isMobile = computed(() => viewportWidth.value < 768)
const isTablet = computed(() => viewportWidth.value < 1200)
function updateViewport() { viewportWidth.value = window.innerWidth }
window.addEventListener('resize', updateViewport)
onBeforeUnmount(() => window.removeEventListener('resize', updateViewport))
const form = reactive({ task_id: '', scope: 'device_group' as 'device' | 'device_group', device: '', device_group: '', point_group: '', interval: 1 as number | null, sinks: [] as string[], enabled: true })

function openNew() {
  editing.value = ''
  form.task_id = ''; form.scope = 'device_group'; form.device = ''; form.device_group = ''
  form.point_group = store.pointGroups[0]?.id || ''; form.interval = 1; form.sinks = ['file_archive']; form.enabled = true
  dlg.value = true
}
function openEdit(t: TaskDef) {
  editing.value = t.task_id
  form.task_id = t.task_id; form.scope = t.device ? 'device' : 'device_group'
  form.device = t.device; form.device_group = t.device_group
  form.point_group = t.point_group; form.interval = t.interval; form.sinks = [...t.sinks]; form.enabled = t.enabled
  dlg.value = true
}
function save() {
  const id = form.task_id.trim()
  if (!id) { ElMessage.error('Task ID is required'); return }
  if (!editing.value && store.tasks.some(t => t.task_id === id)) { ElMessage.error(`Task ${id} already exists`); return }
  const device = form.scope === 'device' ? form.device : ''
  const device_group = form.scope === 'device_group' ? form.device_group : ''
  if (!device && !device_group) { ElMessage.error("Exactly one of 'device' / 'device_group' must be configured"); return }
  if (!form.point_group) { ElMessage.error('point_group is required'); return }
  if (form.interval === null || form.interval <= 0) { ElMessage.error('interval must be > 0'); return }
  if (!form.sinks.length) { ElMessage.error('At least one target sink is required'); return }
  if (editing.value) {
    const t = store.tasks.find(x => x.task_id === editing.value)
    if (t) Object.assign(t, { device, device_group, point_group: form.point_group, interval: form.interval, sinks: [...form.sinks], enabled: form.enabled })
    if (t && !t.enabled) t.runtime = 'STOPPED'
  } else {
    store.tasks.push({ task_id: id, device, device_group, point_group: form.point_group, interval: form.interval, sinks: [...form.sinks], enabled: form.enabled, runtime: 'STOPPED' })
  }
  dlg.value = false
  ElMessage.success(editing.value ? 'Task updated (mock)' : 'Task created (mock)')
}
function toggle(t: TaskDef) {
  if (t.runtime !== 'RUNNING' && !t.enabled) { ElMessage.warning(`Task ${t.task_id} is disabled`); return }
  t.runtime = t.runtime === 'RUNNING' ? 'STOPPED' : 'RUNNING'
  ElMessage.success(`Task ${t.task_id} ${t.runtime === 'RUNNING' ? 'started' : 'stopped'} (mock)`)
}
async function del(t: TaskDef) {
  await ElMessageBox.confirm(`删除任务 ${t.task_id}？`, 'Delete Task', { type: 'warning' })
  store.tasks = store.tasks.filter(x => x !== t)
  ElMessage.success('Task deleted (mock)')
}
function targetText(t: TaskDef) { return t.device ? `device: ${t.device}` : `group: ${t.device_group}` }
</script>

<template><div class="standard-page"><div class="head"><div><h1>Tasks</h1><p>采集任务定义与运行控制</p></div><el-button type="primary" @click="openNew">+ New Task</el-button></div><el-card shadow="never"><el-table :data="store.tasks"><el-table-column prop="task_id" label="Task"/><el-table-column label="Target"><template #default="s">{{targetText(s.row)}}</template></el-table-column><el-table-column v-if="!isMobile" prop="point_group" label="Point Group"/><el-table-column v-if="!isTablet" prop="interval" label="Interval(s)"/><el-table-column v-if="!isTablet" label="Sinks"><template #default="s">{{s.row.sinks.join(', ')}}</template></el-table-column><el-table-column v-if="!isMobile" label="Enabled"><template #default="s"><el-switch v-model="s.row.enabled" @change="!s.row.enabled&&(s.row.runtime='STOPPED')"/></template></el-table-column><el-table-column label="Runtime"><template #default="s"><el-tag :type="s.row.runtime==='RUNNING'?'success':'info'">{{s.row.runtime}}</el-tag></template></el-table-column><el-table-column label="Actions" :width="isMobile ? 176 : 220"><template #default="s"><el-button size="small" @click="toggle(s.row)">{{s.row.runtime==='RUNNING'?'Stop':'Start'}}</el-button><el-button size="small" @click="openEdit(s.row)">Edit</el-button><el-button size="small" type="danger" plain @click="del(s.row)">Delete</el-button></template></el-table-column></el-table></el-card>
<el-dialog v-model="dlg" :title="editing?'Edit Task':'New Task'" width="680"><el-form label-position="top"><div class="grid"><el-form-item label="Task ID"><el-input v-model="form.task_id" :disabled="!!editing"/></el-form-item><el-form-item label="Scope"><el-select v-model="form.scope"><el-option label="Device Group" value="device_group"/><el-option label="Single Device" value="device"/></el-select></el-form-item><el-form-item v-if="form.scope==='device_group'" label="Device Group"><el-select v-model="form.device_group"><el-option v-for="g in store.deviceGroups" :label="g.id" :value="g.id"/></el-select></el-form-item><el-form-item v-else label="Device"><el-select v-model="form.device" filterable><el-option v-for="d in store.devices" :label="d.device_id" :value="d.device_id"/></el-select></el-form-item><el-form-item label="Point Group"><el-select v-model="form.point_group"><el-option v-for="g in store.pointGroups" :label="g.id" :value="g.id"/></el-select></el-form-item><el-form-item label="Interval (s)"><el-input-number v-model="form.interval" :min="0.1" :step="0.5" style="width:100%"/></el-form-item><el-form-item label="Target Sinks"><el-select v-model="form.sinks" multiple><el-option v-for="s in store.sinks" :label="s.name" :value="s.name"/></el-select></el-form-item><el-form-item label="Enabled"><el-switch v-model="form.enabled"/></el-form-item></div></el-form><template #footer><el-button @click="dlg=false">Cancel</el-button><el-button type="primary" @click="save">Save</el-button></template></el-dialog></div></template>
