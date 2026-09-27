<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { modelOf, pointsOfTable, store, tableOfDevice, unitSymbol } from '../mock/data'
import type { DeviceInst } from '../mock/types'

// ---- Type → Group → Instance 浏览 ----
const activeType = ref('turbine')
const activeGroup = ref('turbine_modbus')
const search = ref('')
const groupsOfType = computed(() => store.deviceGroups.filter(g => g.device_type === activeType.value))
const current = computed(() => store.devices.filter(d =>
  modelOf(d)?.device_type === activeType.value &&
  d.device_group === activeGroup.value &&
  (!search.value || `${d.device_id} ${d.host}`.includes(search.value))))
function selectType(t: string) { activeType.value = t; activeGroup.value = store.deviceGroups.find(g => g.device_type === t)?.id || '' }
function countOfType(t: string) { return store.devices.filter(d => modelOf(d)?.device_type === t).length }
function countOfGroup(g: string) { return store.devices.filter(d => d.device_group === g).length }
function typeName(t: string) { return store.deviceTypes.find(x => x.id === t)?.name || t }

// ---- Add Device ----
const addOpen = ref(false)
const newDev = reactive({ id: '', type: 'turbine', group: 'turbine_modbus', model: 'modbus_wtg', host: '' })
// 表单内 Type 切换只影响表单自身，不联动页面 activeType/activeGroup
function onNewDevType() {
  newDev.group = store.deviceGroups.find(g => g.device_type === newDev.type)?.id || ''
  newDev.model = store.deviceModels.find(m => m.device_type === newDev.type)?.id || ''
}
function openAdd() { newDev.id = ''; newDev.type = 'turbine'; onNewDevType(); newDev.host = ''; addOpen.value = true }
function add() {
  const id = newDev.id.trim(), host = newDev.host.trim()
  if (!id) { ElMessage.error('Device ID is required'); return }
  if (store.devices.some(d => d.device_id === id)) { ElMessage.error(`Device ID ${id} already exists`); return }
  if (!host) { ElMessage.error('Host is required'); return }
  if (!store.deviceGroups.some(g => g.id === newDev.group && g.device_type === newDev.type)) { ElMessage.error('Invalid device group'); return }
  const m = store.deviceModels.find(x => x.id === newDev.model && x.device_type === newDev.type)
  if (!m) { ElMessage.error('No model available for this type'); return }
  store.devices.push({ device_id: id, model: m.id, device_group: newDev.group, host, port: undefined, enabled: true, online: false })
  addOpen.value = false
  ElMessage.success('Device created (mock)')
}

// ---- 详情 Drawer ----
const drawer = ref(false)
const tab = ref('Overview')
const selected = ref<DeviceInst | null>(null)
function openDev(d: DeviceInst) { selected.value = d; drawer.value = true; tab.value = 'Overview' }
const selModel = computed(() => selected.value ? modelOf(selected.value) : undefined)

async function del() {
  if (!selected.value) return
  const id = selected.value.device_id
  const direct = store.tasks.filter(t => t.device === id).length
  await ElMessageBox.confirm(
    `删除 ${id}？\n\n将停止该设备的任务实例；直接引用该设备的任务定义（${direct} 个）会同时删除；Group 任务保留。`,
    'Delete Device', { type: 'warning', confirmButtonText: 'Delete Device & Related Tasks' })
  store.tasks = store.tasks.filter(t => t.device !== id)
  store.devices = store.devices.filter(x => x !== selected.value)
  drawer.value = false
  ElMessage.success('Device deleted (mock)')
}

// ---- Data 标签页：点集来自设备绑定点表，运行值 / 更新时间为 mock ----
const pq = ref(''), pgroup = ref('All'), pmode = ref('All')
interface Row { point_id: string, variable_name: string, description: string, value: string, unit: string, group: string, age: string, writable: boolean, updated: boolean }
const dataRows = computed<Row[]>(() => {
  if (!selected.value) return []
  return pointsOfTable(tableOfDevice(selected.value)).map((p, i) => ({
    point_id: p.point_id,
    variable_name: p.variable_name,
    description: p.description,
    value: String(((i * 7) % 1000) / 10),
    unit: unitSymbol(p.unit),
    group: p.point_groups[0] || 'all',
    age: i % 19 === 0 ? '12 s ago' : `${80 + (i % 8) * 37} ms ago`,
    writable: p.point_groups.includes('control'), // 运行态 mock：control 组视为可写命令点
    updated: i % 3 === 0,
  }))
})
const visibleData = computed(() => dataRows.value.filter(r =>
  (pgroup.value === 'All' || r.group === pgroup.value) &&
  (pmode.value === 'All' || (pmode.value === 'Updated' ? r.updated : r.writable)) &&
  (!pq.value || r.point_id.includes(pq.value) || r.variable_name.includes(pq.value) || r.description.includes(pq.value))).slice(0, 120))

// ---- Trend ----
const trendFields = ref(['active_power', 'wind_speed'])
const fieldOpen = ref(false)

// ---- Control：命令点 = control 分组的点（运行态 mock）----
const cmdPoint = ref('')
const cmdValue = ref(1600)
const cmdPoints = computed(() => dataRows.value.filter(r => r.writable).map(r => r.point_id))
const feedback = computed(() => cmdPoints.value.slice(0, 3))
function sendCmd() {
  if (!cmdPoint.value) { ElMessage.warning('Select a writable point first'); return }
  ElMessage.success(`Command sent: ${selected.value?.device_id}/${cmdPoint.value} = ${cmdValue.value} (mock)`)
}
</script>

<template><div><div class="head"><div><h1>Devices</h1><p>设备实例、运行状态、数据与控制</p></div><el-button type="primary" @click="openAdd">+ Add Device</el-button></div><el-card shadow="never"><div class="tabs"><button v-for="t in store.deviceTypes" :class="{on:activeType===t.id}" @click="selectType(t.id)">{{t.name}} <small>{{countOfType(t.id)}}</small></button></div><div class="chips"><button v-for="g in groupsOfType" :class="{on:activeGroup===g.id}" @click="activeGroup=g.id">{{g.id}} ({{countOfGroup(g.id)}})</button></div><div class="toolbar"><b>{{activeGroup}}</b><el-input v-model="search" placeholder="Search device ID / IP..." clearable style="width:300px"/></div><el-table :data="current" height="560" @row-click="openDev"><el-table-column prop="device_id" label="Device"/><el-table-column prop="model" label="Model"/><el-table-column label="Protocol"><template #default="s">{{modelOf(s.row)?.protocol}}</template></el-table-column><el-table-column label="Endpoint"><template #default="s">{{s.row.host}}<template v-if="s.row.port">:{{s.row.port}}</template></template></el-table-column><el-table-column label="Status"><template #default="s"><el-tag :type="s.row.online?'success':'danger'">{{s.row.online?'Online':'Offline'}}</el-tag></template></el-table-column></el-table></el-card>
<el-dialog v-model="addOpen" title="Add Device" width="680"><el-form label-position="top"><div class="grid"><el-form-item label="Device ID"><el-input v-model="newDev.id"/></el-form-item><el-form-item label="Type"><el-select v-model="newDev.type" @change="onNewDevType"><el-option v-for="t in store.deviceTypes" :label="t.name" :value="t.id"/></el-select></el-form-item><el-form-item label="Group"><el-select v-model="newDev.group"><el-option v-for="g in store.deviceGroups.filter(x=>x.device_type===newDev.type)" :label="g.id" :value="g.id"/></el-select></el-form-item><el-form-item label="Model"><el-select v-model="newDev.model"><el-option v-for="m in store.deviceModels.filter(x=>x.device_type===newDev.type)" :label="m.id" :value="m.id"/></el-select></el-form-item><el-form-item label="Host"><el-input v-model="newDev.host"/></el-form-item></div></el-form><template #footer><el-button @click="addOpen=false">Cancel</el-button><el-button type="primary" @click="add">Create</el-button></template></el-dialog>
<el-drawer v-model="drawer" size="68%" :title="selected?.device_id"><template v-if="selected"><div class="devhead"><div><h2>{{selected.device_id}} <el-tag :type="selected.online?'success':'danger'">{{selected.online?'Online':'Offline'}}</el-tag></h2><p>{{selected.model}} · {{selModel?.protocol}} · {{selected.host}}<template v-if="selected.port">:{{selected.port}}</template></p></div><el-tag type="success">Applied</el-tag></div><el-tabs v-model="tab"><el-tab-pane label="Overview" name="Overview"><div class="detail"><div v-for="x in [['Type',selModel?.device_type||''],['Group',selected.device_group],['Model',selected.model],['Protocol',selModel?.protocol||''],['Point Table',selModel?.point_table||''],['Host',selected.host],['Status',selected.online?'Connected':'Disconnected']]"><small>{{x[0]}}</small><b>{{x[1]}}</b></div></div></el-tab-pane><el-tab-pane label="Config" name="Config"><div class="right"><el-button>Edit</el-button><el-button type="danger" plain @click="del">Delete Device</el-button></div><pre>device_id: {{selected.device_id}}\nmodel: {{selected.model}}\ndevice_group: {{selected.device_group}}\nendpoint:\n  host: {{selected.host}}<template v-if="selected.port">\n  port: {{selected.port}}</template>\nenabled: {{selected.enabled}}</pre></el-tab-pane>
<el-tab-pane label="Data" name="Data"><div class="toolbar"><div><b>{{dataRows.length}} points</b> <span>· Last update 15:52:31.428</span></div><div class="row"><el-input v-model="pq" placeholder="Search point / variable / description..." style="width:290px"/><el-segmented v-model="pmode" :options="['All','Updated','Writable']"/></div></div><div class="chips"><button v-for="g in ['All',...store.pointGroups.map(x=>x.id)]" :class="{on:pgroup===g}" @click="pgroup=g">{{g}}</button></div><el-table :data="visibleData" height="500"><el-table-column prop="point_id" label="Point"/><el-table-column prop="value" label="Value"/><el-table-column prop="unit" label="Unit"/><el-table-column prop="age" label="Last Update"/></el-table><p>原型按 600+ 点设计；正式实现使用虚拟表格。</p></el-tab-pane>
<el-tab-pane label="Trend" name="Trend"><div class="toolbar"><div><el-button @click="fieldOpen=true">Select Fields</el-button> <span>{{trendFields.length}} selected · Last update 15:52:31.428</span></div><el-segmented :model-value="'Real-time'" :options="['Real-time','5 min','15 min','1 h']"/></div><div class="tags"><el-tag v-for="f in trendFields" closable @close="trendFields=trendFields.filter(x=>x!==f)">{{f}}</el-tag></div><div class="chart"><div v-for="(f,i) in trendFields" class="fake-line" :style="{top:(40+i*42)+'px'}">{{f}} ─────╱───╲────╱────</div></div></el-tab-pane>
<el-tab-pane label="Control" name="Control"><div class="control"><section><h3>Command</h3><el-form label-position="top"><el-form-item label="Writable Point"><el-select v-model="cmdPoint" style="width:100%"><el-option v-for="p in cmdPoints" :label="p" :value="p"/></el-select></el-form-item><el-form-item label="Current Value"><el-input model-value="180.0" disabled/></el-form-item><p>Last update: 15:52:31.428</p><el-form-item label="Target Value"><el-input-number v-model="cmdValue" style="width:100%"/></el-form-item><el-button type="primary" style="width:100%" @click="sendCmd">Send Command</el-button></el-form></section><section><h3>Feedback</h3><div v-for="f in feedback" class="feedback"><span>{{f}}</span><b>179.8</b><span>120 ms ago</span></div></section></div></el-tab-pane></el-tabs></template></el-drawer>
<el-dialog v-model="fieldOpen" title="Select Trend Fields" width="700"><el-checkbox-group v-model="trendFields" class="picker"><el-checkbox v-for="p in dataRows.slice(0,80)" :value="p.point_id" :disabled="trendFields.length>=10&&!trendFields.includes(p.point_id)">{{p.point_id}} <small>· {{p.group}}</small></el-checkbox></el-checkbox-group><template #footer>最多 10 个字段 <el-button type="primary" @click="fieldOpen=false">Done</el-button></template></el-dialog></div></template>
