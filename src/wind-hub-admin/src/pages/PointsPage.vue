<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { addressText, pointsOfTable, store, tableProtocol, unitSymbol, validateAddress } from '../mock/data'
import { DATA_TYPES, MODBUS_REGISTER_TYPES } from '../mock/types'
import type { PointAddress, PointDef } from '../mock/types'

// 编辑单个 Point Table 内的点定义 —— 地址字段由点表 protocol 驱动
const pointTable = ref('beckhoff_wtg_v1')
const protocol = computed(() => tableProtocol(pointTable.value) || 'ads')
const tableDef = computed(() => store.pointTables.find(t => t.id === pointTable.value))
const rows = computed<PointDef[]>(() => pointsOfTable(pointTable.value))
const dirty = ref(false)
const addrLabel = computed(() => protocol.value === 'ads' ? 'Symbol / Index' : protocol.value === 'modbus' ? 'Type / Address' : 'IOA')
const addressOf = (p: PointDef) => addressText(protocol.value, p)

// ---- Add / Edit Point ----
const pointEdit = ref(false)
const editing = ref('') // 非空表示编辑既有 point_id
const draft = reactive({
  point_id: '', variable_name: '', point_groups: ['all'] as string[],
  symbol: '', index_group: '', index_offset: '',
  register_type: 'holding', address: undefined as number | undefined,
  ioa: undefined as number | undefined, ioa_type: '',
  data_type: 'float32', scale: 1, offset: 0, unit: 'none', description: '',
})
function resetDraft() {
  draft.point_id = ''; draft.variable_name = ''; draft.point_groups = ['all']
  draft.symbol = ''; draft.index_group = ''; draft.index_offset = ''
  draft.register_type = 'holding'; draft.address = undefined
  draft.ioa = undefined; draft.ioa_type = ''
  draft.data_type = 'float32'; draft.scale = 1; draft.offset = 0; draft.unit = 'none'; draft.description = ''
}
function openAdd() { editing.value = ''; resetDraft(); pointEdit.value = true }
function openEdit(p: PointDef) {
  editing.value = p.point_id
  resetDraft()
  draft.point_id = p.point_id; draft.variable_name = p.variable_name; draft.point_groups = [...p.point_groups]
  draft.symbol = p.address.symbol || ''; draft.index_group = p.address.index_group || ''; draft.index_offset = p.address.index_offset || ''
  draft.register_type = p.address.type || 'holding'; draft.address = p.address.address
  draft.ioa = p.address.ioa; draft.ioa_type = protocol.value === 'iec104' ? (p.address.type || '') : ''
  draft.data_type = p.data_type; draft.scale = p.scale; draft.offset = p.offset; draft.unit = p.unit; draft.description = p.description
  pointEdit.value = true
}
function draftAddress(): PointAddress {
  if (protocol.value === 'ads') {
    const a: PointAddress = {}
    if (draft.symbol.trim()) a.symbol = draft.symbol.trim()
    if (draft.index_group.trim()) a.index_group = draft.index_group.trim()
    if (draft.index_offset.trim()) a.index_offset = draft.index_offset.trim()
    return a
  }
  if (protocol.value === 'modbus') return { type: draft.register_type, address: draft.address }
  return { ioa: draft.ioa, ...(draft.ioa_type.trim() ? { type: draft.ioa_type.trim() } : {}) }
}
function savePoint() {
  const id = draft.point_id.trim(), list = store.points[pointTable.value]
  if (!id) { ElMessage.error('Point ID is required'); return }
  if (!editing.value && list.some(p => p.point_id === id)) { ElMessage.error(`Point ${id} already exists`); return }
  if (!draft.point_groups.length) { ElMessage.error('point_groups must be non-empty'); return }
  const address = draftAddress()
  const err = validateAddress(protocol.value, address)
  if (err) { ElMessage.error(err); return }
  if (!DATA_TYPES.includes(draft.data_type)) { ElMessage.error('Invalid data_type'); return }
  if (!store.units[draft.unit]) { ElMessage.error('Invalid unit'); return }
  const row: PointDef = {
    point_id: id, variable_name: draft.variable_name.trim(), point_groups: [...draft.point_groups],
    address, data_type: draft.data_type, scale: draft.scale, offset: draft.offset,
    unit: draft.unit, description: draft.description.trim(),
  }
  if (editing.value) {
    const i = list.findIndex(p => p.point_id === editing.value)
    if (i >= 0) list.splice(i, 1, row)
  } else {
    list.push(row)
  }
  dirty.value = true
  pointEdit.value = false
  ElMessage.success(editing.value ? 'Point updated (mock)' : 'Point added (mock)')
}
async function delPoint(p: PointDef) {
  await ElMessageBox.confirm(`删除点 ${p.point_id}？`, 'Delete Point', { type: 'warning' })
  const list = store.points[pointTable.value]
  list.splice(list.indexOf(p), 1)
  dirty.value = true
  ElMessage.success('Point deleted (mock)')
}
function save() { dirty.value = false; ElMessage.success(`${pointTable.value} saved (mock)`) }
function saveApply() { dirty.value = false; ElMessage.success(`${pointTable.value} saved & reload applied (mock)`) }
</script>

<template><div><div class="head"><div><h1>Points</h1><p>编辑 Point Table 内的具体点定义</p></div><div><el-button @click="save">Save</el-button> <el-button type="primary" @click="saveApply">Save & Apply</el-button></div></div><el-card shadow="never"><div class="toolbar"><div class="row"><b>Point Table</b><el-select v-model="pointTable" style="width:240px"><el-option v-for="t in store.pointTables" :label="t.id" :value="t.id"/></el-select><el-tag>{{protocol}}</el-tag><el-tag v-if="tableDef?.extends" type="info">extends {{tableDef.extends}}</el-tag><span>{{rows.length}} points</span><el-tag v-if="dirty" type="warning">Pending Apply</el-tag></div><el-button type="primary" @click="openAdd">+ Add Point</el-button></div><el-table :data="rows" height="590"><el-table-column prop="point_id" label="Point"/><el-table-column prop="variable_name" label="Variable"/><el-table-column :label="addrLabel"><template #default="s">{{addressOf(s.row)}}</template></el-table-column><el-table-column prop="data_type" label="Data Type"/><el-table-column label="Groups"><template #default="s"><el-tag v-for="g in s.row.point_groups">{{g}}</el-tag></template></el-table-column><el-table-column prop="scale" label="Scale"/><el-table-column prop="offset" label="Offset"/><el-table-column label="Unit"><template #default="s">{{unitSymbol(s.row.unit)||s.row.unit}}</template></el-table-column><el-table-column label="Actions" width="150"><template #default="s"><el-button size="small" @click="openEdit(s.row)">Edit</el-button><el-button size="small" type="danger" plain @click="delPoint(s.row)">Delete</el-button></template></el-table-column></el-table></el-card>
<el-dialog v-model="pointEdit" :title="editing?'Edit Point':'Add Point'" width="760"><el-form label-position="top"><div class="grid"><el-form-item label="Point ID"><el-input v-model="draft.point_id" :disabled="!!editing"/></el-form-item><el-form-item label="Variable Name"><el-input v-model="draft.variable_name"/></el-form-item><template v-if="protocol==='ads'"><el-form-item label="Symbol"><el-input v-model="draft.symbol" placeholder="MAIN.rotorSpeed"/></el-form-item><el-form-item label="Index Group"><el-input v-model="draft.index_group" placeholder="0x4020"/></el-form-item><el-form-item label="Index Offset"><el-input v-model="draft.index_offset" placeholder="0x1234"/></el-form-item></template><template v-else-if="protocol==='modbus'"><el-form-item label="Register Type"><el-select v-model="draft.register_type"><el-option v-for="r in MODBUS_REGISTER_TYPES" :label="r" :value="r"/></el-select></el-form-item><el-form-item label="Address (0-based)"><el-input-number v-model="draft.address" :min="0" :controls="false" style="width:100%"/></el-form-item></template><template v-else><el-form-item label="IOA"><el-input-number v-model="draft.ioa" :min="0" :max="16777215" :controls="false" style="width:100%"/></el-form-item><el-form-item label="Type (optional)"><el-input v-model="draft.ioa_type" placeholder="measured_value"/></el-form-item></template><el-form-item label="Data Type"><el-select v-model="draft.data_type"><el-option v-for="x in DATA_TYPES" :label="x" :value="x"/></el-select></el-form-item><el-form-item label="Point Groups"><el-select v-model="draft.point_groups" multiple><el-option v-for="g in store.pointGroups" :label="g.id" :value="g.id"/></el-select></el-form-item><el-form-item label="Scale"><el-input-number v-model="draft.scale"/></el-form-item><el-form-item label="Offset"><el-input-number v-model="draft.offset"/></el-form-item><el-form-item label="Unit"><el-select v-model="draft.unit"><el-option v-for="(u,id) in store.units" :label="id+(u.symbol?` (${u.symbol})`:'')" :value="id"/></el-select></el-form-item><el-form-item label="Description"><el-input v-model="draft.description"/></el-form-item></div></el-form><template #footer><el-button @click="pointEdit=false">Cancel</el-button><el-button type="primary" @click="savePoint">Save</el-button></template></el-dialog></div></template>
