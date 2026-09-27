<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { pointsOfTable, store } from '../mock/data'
import { ADS_READ_MODES, PROTOCOLS } from '../mock/types'

// Meta Definition 管理：DeviceType / DeviceGroup / DeviceModel / PointTable / PointGroup
const defTab = ref('Device Types')
const dlg = ref('')
const form = reactive({ id: '', name: '', device_type: '', protocol: 'modbus' as string, point_table: '', manufacturer: '', read_mode: 'sum', extends: '' })

const tablesOfProtocol = computed(() => store.pointTables.filter(t => t.protocol === form.protocol && t.id !== form.id))

function open(kind: string) {
  form.id = ''; form.name = ''; form.device_type = ''; form.protocol = 'modbus'
  form.point_table = ''; form.manufacturer = ''; form.read_mode = 'sum'; form.extends = ''
  dlg.value = kind
}
function onModelProtocol() {
  // Point Table 必须与 Protocol 匹配：协议变化后清空已选点表
  form.point_table = ''
}
function checkId(list: string[], label: string): boolean {
  if (!form.id.trim()) { ElMessage.error(`${label} ID is required`); return false }
  if (list.includes(form.id.trim())) { ElMessage.error(`${label} ID ${form.id} already exists`); return false }
  return true
}
function createType() {
  if (!checkId(store.deviceTypes.map(x => x.id), 'Type')) return
  store.deviceTypes.push({ id: form.id.trim(), name: form.name.trim() || form.id.trim() })
  done('Type')
}
function createGroup() {
  if (!checkId(store.deviceGroups.map(x => x.id), 'Group')) return
  if (!form.device_type) { ElMessage.error('Device type is required'); return }
  store.deviceGroups.push({ id: form.id.trim(), device_type: form.device_type })
  done('Group')
}
function createModel() {
  if (!checkId(store.deviceModels.map(x => x.id), 'Model')) return
  if (!form.device_type) { ElMessage.error('Device type is required'); return }
  const t = store.pointTables.find(x => x.id === form.point_table)
  if (!t) { ElMessage.error('Point table is required'); return }
  if (t.protocol !== form.protocol) { ElMessage.error(`Point table protocol (${t.protocol}) must match model protocol (${form.protocol})`); return }
  store.deviceModels.push({
    id: form.id.trim(), device_type: form.device_type, manufacturer: form.manufacturer.trim(),
    protocol: form.protocol as 'ads' | 'modbus' | 'iec104', point_table: t.id,
    read_mode: form.protocol === 'ads' ? form.read_mode : '',
  })
  done('Model')
}
function createTable() {
  if (!checkId(store.pointTables.map(x => x.id), 'Table')) return
  if (form.extends) {
    const parent = store.pointTables.find(x => x.id === form.extends)
    if (!parent || parent.protocol !== form.protocol) { ElMessage.error('Parent table must use the same protocol'); return }
  }
  store.pointTables.push({ id: form.id.trim(), protocol: form.protocol as 'ads' | 'modbus' | 'iec104', extends: form.extends })
  // 子表以父表当前点集为初始内容（原型对继承展开做一次性快照）
  store.points[form.id.trim()] = form.extends ? pointsOfTable(form.extends).map(p => ({ ...p, address: { ...p.address }, point_groups: [...p.point_groups] })) : []
  done('Point table')
}
function createPgroup() {
  if (!checkId(store.pointGroups.map(x => x.id), 'Point group')) return
  store.pointGroups.push({ id: form.id.trim(), name: form.name.trim() || form.id.trim() })
  done('Point group')
}
function done(label: string) { dlg.value = ''; ElMessage.success(`${label} created (mock)`) }

const typeRows = computed(() => store.deviceTypes.map(t => ({
  ...t,
  refs: `${store.deviceModels.filter(m => m.device_type === t.id).length} models · ${store.deviceGroups.filter(g => g.device_type === t.id).length} groups`,
})))
const groupRows = computed(() => store.deviceGroups.map(g => ({
  ...g,
  refs: `${store.devices.filter(d => d.device_group === g.id).length} devices · ${store.tasks.filter(t => t.device_group === g.id).length} tasks`,
})))
const tableRows = computed(() => store.pointTables.map(t => ({ ...t, points: pointsOfTable(t.id).length })))
const pgroupRows = computed(() => store.pointGroups.map(g => ({
  ...g,
  refs: `${store.tasks.filter(t => t.point_group === g.id).length} tasks`,
})))
</script>

<template><div><div class="head"><div><h1>Definitions</h1><p>设备与点表相关的 Meta / Prototype 定义</p></div></div><el-card shadow="never"><el-tabs v-model="defTab"><el-tab-pane label="Device Types" name="Device Types"><div class="right"><el-button type="primary" @click="open('type')">+ New Type</el-button></div><el-table :data="typeRows"><el-table-column prop="id" label="ID"/><el-table-column prop="name" label="Name"/><el-table-column prop="refs" label="References"/></el-table></el-tab-pane><el-tab-pane label="Device Groups" name="Device Groups"><div class="right"><el-button type="primary" @click="open('group')">+ New Group</el-button></div><el-table :data="groupRows"><el-table-column prop="id" label="Group ID"/><el-table-column prop="device_type" label="Device Type"/><el-table-column prop="refs" label="References"/></el-table></el-tab-pane><el-tab-pane label="Device Models" name="Device Models"><div class="right"><el-button type="primary" @click="open('model')">+ New Model</el-button></div><el-table :data="store.deviceModels"><el-table-column prop="id" label="Model ID"/><el-table-column prop="device_type" label="Device Type"/><el-table-column prop="protocol" label="Protocol"/><el-table-column prop="point_table" label="Point Table"/></el-table></el-tab-pane><el-tab-pane label="Point Tables" name="Point Tables"><div class="right"><el-button type="primary" @click="open('table')">+ New Point Table</el-button></div><el-table :data="tableRows"><el-table-column prop="id" label="Table ID"/><el-table-column prop="protocol" label="Protocol"/><el-table-column prop="extends" label="Extends"><template #default="s">{{s.row.extends||'—'}}</template></el-table-column><el-table-column prop="points" label="Points"/></el-table></el-tab-pane><el-tab-pane label="Point Groups" name="Point Groups"><div class="right"><el-button type="primary" @click="open('pgroup')">+ New Point Group</el-button></div><el-table :data="pgroupRows"><el-table-column prop="id" label="Group ID"/><el-table-column prop="name" label="Name"/><el-table-column prop="refs" label="References"/></el-table></el-tab-pane></el-tabs></el-card>
<el-dialog :model-value="dlg==='type'" title="New Device Type" width="480" @close="dlg=''"><el-form label-position="top"><el-form-item label="ID"><el-input v-model="form.id"/></el-form-item><el-form-item label="Name"><el-input v-model="form.name"/></el-form-item></el-form><template #footer><el-button @click="dlg=''">Cancel</el-button><el-button type="primary" @click="createType">Create</el-button></template></el-dialog>
<el-dialog :model-value="dlg==='group'" title="New Device Group" width="480" @close="dlg=''"><el-form label-position="top"><el-form-item label="ID"><el-input v-model="form.id"/></el-form-item><el-form-item label="Device Type"><el-select v-model="form.device_type" style="width:100%"><el-option v-for="t in store.deviceTypes" :label="t.name" :value="t.id"/></el-select></el-form-item></el-form><template #footer><el-button @click="dlg=''">Cancel</el-button><el-button type="primary" @click="createGroup">Create</el-button></template></el-dialog>
<el-dialog :model-value="dlg==='model'" title="New Device Model" width="680" @close="dlg=''"><el-form label-position="top"><div class="grid"><el-form-item label="Model ID"><el-input v-model="form.id"/></el-form-item><el-form-item label="Device Type"><el-select v-model="form.device_type"><el-option v-for="t in store.deviceTypes" :label="t.name" :value="t.id"/></el-select></el-form-item><el-form-item label="Protocol"><el-select v-model="form.protocol" @change="onModelProtocol"><el-option v-for="p in PROTOCOLS" :label="p" :value="p"/></el-select></el-form-item><el-form-item label="Point Table"><el-select v-model="form.point_table"><el-option v-for="t in tablesOfProtocol" :label="t.id" :value="t.id"/></el-select></el-form-item><el-form-item label="Manufacturer"><el-input v-model="form.manufacturer"/></el-form-item><el-form-item v-if="form.protocol==='ads'" label="Read Mode"><el-select v-model="form.read_mode"><el-option v-for="r in ADS_READ_MODES" :label="r" :value="r"/></el-select></el-form-item></div></el-form><template #footer><el-button @click="dlg=''">Cancel</el-button><el-button type="primary" @click="createModel">Create</el-button></template></el-dialog>
<el-dialog :model-value="dlg==='table'" title="New Point Table" width="480" @close="dlg=''"><el-form label-position="top"><el-form-item label="Table ID"><el-input v-model="form.id"/></el-form-item><el-form-item label="Protocol"><el-select v-model="form.protocol" style="width:100%" @change="form.extends=''"><el-option v-for="p in PROTOCOLS" :label="p" :value="p"/></el-select></el-form-item><el-form-item label="Extends (optional base table)"><el-select v-model="form.extends" style="width:100%" clearable><el-option v-for="t in tablesOfProtocol" :label="t.id" :value="t.id"/></el-select></el-form-item></el-form><template #footer><el-button @click="dlg=''">Cancel</el-button><el-button type="primary" @click="createTable">Create</el-button></template></el-dialog>
<el-dialog :model-value="dlg==='pgroup'" title="New Point Group" width="480" @close="dlg=''"><el-form label-position="top"><el-form-item label="ID"><el-input v-model="form.id"/></el-form-item><el-form-item label="Name"><el-input v-model="form.name"/></el-form-item></el-form><template #footer><el-button @click="dlg=''">Cancel</el-button><el-button type="primary" @click="createPgroup">Create</el-button></template></el-dialog></div></template>
