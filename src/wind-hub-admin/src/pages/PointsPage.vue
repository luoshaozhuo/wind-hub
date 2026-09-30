<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  DEFAULT_POINT_GROUP_ID,
  addressText,
  affectedByPointTables,
  descendantTableIds,
  isDefaultPointGroup,
  isDefaultPointTable,
  pointOrigin,
  pointsOfTable,
  refreshTaskValidity,
  store,
  tableProtocol,
  unitSymbol,
  validateAddress,
} from '../mock/data'
import { useViewport } from '../composables/useViewport'
import { DATA_TYPES, MODBUS_REGISTER_TYPES, PROTOCOLS } from '../mock/types'
import type { PointAddress, PointDef, Protocol } from '../mock/types'

const pointTable = ref('beckhoff_wtg_v1')
const protocol = computed(() => tableProtocol(pointTable.value) || 'ads')
const tableDef = computed(() => store.pointTables.find(t => t.id === pointTable.value))
const rows = computed<PointDef[]>(() => pointsOfTable(pointTable.value))
const pointPage = ref(1)
const pointPageSize = ref(50)
const pagedRows = computed(() => {
  const start = (pointPage.value - 1) * pointPageSize.value
  return rows.value.slice(start, start + pointPageSize.value)
})
watch(pointTable, () => {
  pointPage.value = 1
})
const { isMobile, isTablet } = useViewport()
const addrLabel = computed(() =>
  protocol.value === 'ads' ? 'Symbol / Index' : protocol.value === 'modbus' ? 'Type / Address' : 'IOA',
)
const addressOf = (p: PointDef) => addressText(protocol.value, p)
const originOf = (p: PointDef) => pointOrigin(pointTable.value, p.point_id)

function tableImpact(tableId: string) {
  const tables = [tableId, ...descendantTableIds(tableId)]
  return affectedByPointTables(tables)
}

async function confirmTableImpact(tableId: string, title: string, action: string) {
  const impact = tableImpact(tableId)
  const running = impact.tasks.filter(t => t.runtime === 'RUNNING')
  if (!impact.devices.length && !impact.tasks.length && impact.tables.length === 1) return true
  await ElMessageBox.confirm(
    '<b>' + action + '</b><br><br>' +
    impact.tables.length + ' Point Table(s) affected.<br>' +
    impact.devices.length + ' Device(s) affected.<br>' +
    impact.tasks.length + ' Task(s) affected; ' + running.length + ' currently running.<br><br>' +
    'Affected running tasks will be stopped while the change is applied and restored if they remain valid.',
    title,
    { type: 'warning', confirmButtonText: 'Apply Changes', dangerouslyUseHTMLString: true },
  )
  return true
}

function withAffectedTasksStopped(tableId: string, apply: () => void) {
  const impact = tableImpact(tableId)
  const runningIds = new Set(impact.tasks.filter(t => t.runtime === 'RUNNING').map(t => t.task_id))
  for (const t of impact.tasks) if (runningIds.has(t.task_id)) t.runtime = 'STOPPED'
  apply()
  refreshTaskValidity()
  for (const t of impact.tasks) {
    if (runningIds.has(t.task_id) && t.valid !== false && t.enabled) t.runtime = 'RUNNING'
  }
}

// ---- Point metadata management ----
type ManageSection = 'table' | 'group'
const manageOpen = ref(false)
const manageSection = ref<ManageSection>('table')
const tableEditingId = ref('')
const groupEditingId = ref('')
const tableSnapshot = ref('')
const groupSnapshot = ref('')

const tableDraft = reactive({ id: '', protocol: 'modbus' as Protocol, extends: '' })
const groupDraft = reactive({ id: '', name: '' })

const parentTables = computed(() => {
  const blocked = new Set(tableEditingId.value ? [tableEditingId.value, ...descendantTableIds(tableEditingId.value)] : [])
  return store.pointTables.filter(t =>
    !t.system &&
    t.protocol === tableDraft.protocol &&
    !blocked.has(t.id),
  )
})

const editingTable = computed(() => store.pointTables.find(t => t.id === tableEditingId.value))
const tableDirty = computed(() => !!tableEditingId.value && JSON.stringify(tableDraft) !== tableSnapshot.value)
const groupDirty = computed(() => !!groupEditingId.value && JSON.stringify(groupDraft) !== groupSnapshot.value)
const manageDirty = computed(() => manageSection.value === 'table' ? tableDirty.value : groupDirty.value)

async function beforeManageClose(done:()=>void){
  if(!manageDirty.value){done();return}
  try{
    await ElMessageBox.confirm('Discard unsaved metadata changes?','Unsaved Changes',{type:'warning',confirmButtonText:'Discard'})
    done()
  }catch{}
}
const editingGroup = computed(() => store.pointGroups.find(g => g.id === groupEditingId.value))
const currentTableIsSystem = computed(() => !!tableDef.value?.system)

const testDeviceId = ref('')
const testLoading = ref(false)
const testError = ref('')
const TEST_CANDIDATE_TYPES = ['bool', 'int8', 'uint8', 'int16', 'uint16', 'int32', 'uint32', 'float32', 'float64']
const testCandidates = ref(TEST_CANDIDATE_TYPES.map(type => ({ type, value: '—' })))
const testLatency = ref(0)

const testDevices = computed(() => store.devices.filter(d => {
  const model = store.deviceModels.find(m => m.id === d.model)
  return model?.protocol === protocol.value
}))

const selectedTestDevice = computed(() => store.devices.find(d => d.device_id === testDeviceId.value))

const testRequest = computed(() => {
  if (protocol.value === 'ads') {
    if (draft.symbol.trim()) return `Symbol · ${draft.symbol.trim()}`
    return `Index · ${draft.index_group || '—'} / ${draft.index_offset || '—'}`
  }
  if (protocol.value === 'modbus') return `${draft.register_type} · ${draft.address ?? '—'}`
  return `IOA · ${draft.ioa ?? '—'}${draft.ioa_type ? ` · ${draft.ioa_type}` : ''}`
})

function resetPointTest() {
  testLoading.value = false
  testError.value = ''
  testCandidates.value = TEST_CANDIDATE_TYPES.map(type => ({ type, value: '—' }))
  testLatency.value = 0
  const preferred = testDevices.value.find(d => d.online && d.enabled) || testDevices.value[0]
  testDeviceId.value = preferred?.device_id || ''
}

function mockRawBytes(seed: string): Uint8Array {
  let hash = 2166136261
  for (let i = 0; i < seed.length; i++) {
    hash ^= seed.charCodeAt(i)
    hash = Math.imul(hash, 16777619)
  }
  const bytes = new Uint8Array(8)
  for (let i = 0; i < bytes.length; i++) {
    hash ^= hash << 13
    hash ^= hash >>> 17
    hash ^= hash << 5
    bytes[i] = hash & 0xff
  }
  return bytes
}

function decodeCandidates(bytes: Uint8Array) {
  const view = new DataView(bytes.buffer)
  const format = (v: number) => Number.isFinite(v) ? String(Math.abs(v) >= 1e6 ? v.toExponential(6) : Number(v.toFixed(6))) : String(v)
  return [
    { type: 'bool', value: bytes[0] ? 'true' : 'false' },
    { type: 'int8', value: String(view.getInt8(0)) },
    { type: 'uint8', value: String(view.getUint8(0)) },
    { type: 'int16', value: String(view.getInt16(0, true)) },
    { type: 'uint16', value: String(view.getUint16(0, true)) },
    { type: 'int32', value: String(view.getInt32(0, true)) },
    { type: 'uint32', value: String(view.getUint32(0, true)) },
    { type: 'float32', value: format(view.getFloat32(0, true)) },
    { type: 'float64', value: format(view.getFloat64(0, true)) },
  ]
}

async function runPointTest() {
  if (testLoading.value) return
  const device = selectedTestDevice.value
  if (!device) {
    testError.value = 'Select a device first'
    return
  }
  const address = draftAddress()
  const err = validateAddress(protocol.value, address)
  if (err) {
    testError.value = err
    return
  }

  testLoading.value = true
  testError.value = ''
  testCandidates.value = TEST_CANDIDATE_TYPES.map(type => ({ type, value: '—' }))
  const started = performance.now()
  await new Promise(resolve => setTimeout(resolve, 700))

  if (!device.enabled || !device.online) {
    testLatency.value = Math.round(performance.now() - started)
    testError.value = device.enabled ? 'Device is offline' : 'Device is disabled'
    testLoading.value = false
    return
  }

  const seed = [device.device_id, protocol.value, testRequest.value, draft.data_type].join('|')
  const bytes = mockRawBytes(seed)
  testCandidates.value = decodeCandidates(bytes)
  testLatency.value = Math.round(performance.now() - started)
  testLoading.value = false
}

const tableRows = computed(() => store.pointTables.map(t => {
  const modelIds = store.deviceModels.filter(m => m.point_table === t.id).map(m => m.id)
  const childTableIds = store.pointTables.filter(x => x.extends === t.id).map(x => x.id)
  const devices = store.devices.filter(d => modelIds.includes(d.model)).length
  return {
    ...t,
    points: pointsOfTable(t.id).length,
    localPoints: (store.points[t.id] || []).length,
    inheritedPoints: t.extends ? pointsOfTable(t.extends).filter(p => !(store.points[t.id] || []).some(x => x.point_id === p.point_id) && !(t.remove_points || []).includes(p.point_id)).length : 0,
    models: modelIds.length,
    modelIds,
    devices,
    childTables: childTableIds.length,
    childTableIds,
  }
}))

type TableRow = (typeof tableRows.value)[number]

function tableDeleteBlocked(row: TableRow) {
  return !!row.system || isDefaultPointTable(row.id) || row.childTables > 0 || row.models > 0
}

function tableDeleteBlockerText(row: TableRow) {
  if (row.system || isDefaultPointTable(row.id)) return 'System default Point Tables cannot be deleted'
  const reasons: string[] = []
  if (row.childTableIds.length) reasons.push('Child Table: ' + row.childTableIds.join(', '))
  if (row.modelIds.length) reasons.push('Device Model: ' + row.modelIds.join(', '))
  return reasons.length ? 'Referenced by ' + reasons.join(' · ') : ''
}

const editingTableReferences = computed(() => {
  if (!tableEditingId.value) return { childTables: [] as string[], models: [] as string[] }
  return {
    childTables: store.pointTables.filter(t => t.extends === tableEditingId.value).map(t => t.id),
    models: store.deviceModels.filter(m => m.point_table === tableEditingId.value).map(m => m.id),
  }
})

const groupRows = computed(() =>
  store.pointGroups.map(g => ({
    ...g,
    points: Object.values(store.points).reduce((sum, list) => sum + list.filter(p => p.point_groups.includes(g.id)).length, 0),
    tasks: store.tasks.filter(t => t.point_group === g.id).length,
  })),
)

function openManage() {
  manageOpen.value = true
  manageSection.value = 'table'
  newTable()
}

function selectManage(next: ManageSection) {
  manageSection.value = next
  if (next === 'table') newTable()
  else newGroup()
}

function onManageTabChange(name: string | number) {
  selectManage(String(name) as ManageSection)
}

function onManageRowClick(row: { id: string }) {
  if (manageSection.value === 'table') editTable(row.id)
  else editGroup(row.id)
}

function newTable() {
  tableEditingId.value = ''
  tableDraft.id = ''
  tableDraft.protocol = 'modbus'
  tableDraft.extends = ''
}

function editTable(id: string) {
  const t = store.pointTables.find(x => x.id === id)
  if (!t) return
  tableEditingId.value = t.id
  tableDraft.id = t.id
  tableDraft.protocol = t.protocol
  tableDraft.extends = t.extends || ''
  tableSnapshot.value = JSON.stringify(tableDraft)
}

async function saveTable() {
  const id = tableDraft.id.trim()
  if (!id) {
    ElMessage.error('Point Table ID is required')
    return
  }

  const duplicate = store.pointTables.some(t => t.id === id && t.id !== tableEditingId.value)
  if (duplicate) {
    ElMessage.error('Point table "' + id + '" already exists')
    return
  }

  if (tableDraft.extends) {
    const parent = store.pointTables.find(t => t.id === tableDraft.extends)
    if (!parent || parent.protocol !== tableDraft.protocol) {
      ElMessage.error('Parent table must use the same protocol')
      return
    }
    if (descendantTableIds(id).includes(tableDraft.extends)) {
      ElMessage.error('Point table inheritance cycle is not allowed')
      return
    }
  }

  if (tableEditingId.value) {
    const target = store.pointTables.find(t => t.id === tableEditingId.value)
    if (!target) return
    if (target.system) {
      ElMessage.warning('System default Point Tables cannot be modified')
      return
    }
    if (id !== target.id) {
      ElMessage.warning('Point Table ID is stable after creation')
      return
    }

    const protocolChanged = target.protocol !== tableDraft.protocol
    const parentChanged = target.extends !== tableDraft.extends

    if (protocolChanged) {
      const childCount = store.pointTables.filter(t => t.extends === target.id).length
      const modelCount = store.deviceModels.filter(m => m.point_table === target.id).length
      if (childCount || modelCount || pointsOfTable(target.id).length) {
        ElMessage.warning('Protocol cannot be changed while the table has points, child tables, or Device Model references')
        return
      }
    }

    if (parentChanged && tableDraft.extends) {
      const parentIds = new Set(pointsOfTable(tableDraft.extends).map(p => p.point_id))
      const invalidRemoved = (target.remove_points || []).filter(pid => !parentIds.has(pid))
      if (invalidRemoved.length) {
        ElMessage.error('New parent does not contain removed point(s): ' + invalidRemoved.join(', '))
        return
      }
    }

    if (protocolChanged || parentChanged) {
      await confirmTableImpact(target.id, 'Point Table Change Impact',
        protocolChanged ? 'Change Point Table protocol?' : 'Change parent Point Table?')
    }

    withAffectedTasksStopped(target.id, () => {
      target.protocol = tableDraft.protocol
      target.extends = tableDraft.extends
    })
    tableSnapshot.value = JSON.stringify(tableDraft)
    ElMessage.success('Point table updated (mock)')
  } else {
    store.pointTables.push({
      id,
      protocol: tableDraft.protocol,
      extends: tableDraft.extends,
      remove_points: [],
    })
    store.points[id] = []
    pointTable.value = id
    ElMessage.success('Point table created (mock)')
    newTable()
  }
}

async function deleteTable(row: { id: string; protocol: Protocol; points: number; models: number; devices: number; childTables: number; system?: boolean }) {
  if (row.system || isDefaultPointTable(row.id)) {
    ElMessage.warning('System default Point Tables cannot be deleted')
    return
  }

  const children = store.pointTables.filter(t => t.extends === row.id)
  const models = store.deviceModels.filter(m => m.point_table === row.id)
  if (children.length || models.length) {
    ElMessage.warning(
      'Cannot delete: referenced by ' + children.length + ' child table(s) and ' +
      models.length + ' Device Model(s)',
    )
    return
  }

  await ElMessageBox.confirm(
    '<b>Delete Point Table "' + row.id + '"?</b><br><br>' +
    row.points + ' effective point(s) will no longer be available.<br>' +
    'No references will be migrated automatically.',
    'Delete Point Table',
    { type: 'warning', confirmButtonText: 'Delete', dangerouslyUseHTMLString: true },
  )

  store.pointTables.splice(store.pointTables.findIndex(t => t.id === row.id), 1)
  delete store.points[row.id]
  if (pointTable.value === row.id) {
    pointTable.value = store.pointTables.find(t => !t.system)?.id || store.pointTables[0]?.id || ''
  }
  if (tableEditingId.value === row.id) newTable()
  refreshTaskValidity()
  ElMessage.success('Point table deleted (mock)')
}

function newGroup() {
  groupEditingId.value = ''
  groupDraft.id = ''
  groupDraft.name = ''
}

function editGroup(id: string) {
  const g = store.pointGroups.find(x => x.id === id)
  if (!g) return
  groupEditingId.value = g.id
  groupDraft.id = g.id
  groupDraft.name = g.name
  groupSnapshot.value = JSON.stringify(groupDraft)
}

function saveGroup() {
  const id = groupDraft.id.trim()
  if (!id) {
    ElMessage.error('Point Group ID is required')
    return
  }

  const duplicate = store.pointGroups.some(g => g.id === id && g.id !== groupEditingId.value)
  if (duplicate) {
    ElMessage.error('Point group "' + id + '" already exists')
    return
  }

  if (groupEditingId.value) {
    const target = store.pointGroups.find(g => g.id === groupEditingId.value)
    if (!target) return
    if (target.system) {
      ElMessage.warning('System default Point Group cannot be modified')
      return
    }
    if (id !== target.id) {
      ElMessage.warning('Point Group ID is stable after creation')
      return
    }
    target.name = groupDraft.name.trim() || target.id
    groupSnapshot.value = JSON.stringify(groupDraft)
  } else {
    store.pointGroups.push({ id, name: groupDraft.name.trim() || id })
    newGroup()
  }

  ElMessage.success(groupEditingId.value ? 'Point group updated (mock)' : 'Point group created (mock)')
}

async function deleteGroup(row: { id: string; points: number; tasks: number; system?: boolean }) {
  if (row.system || isDefaultPointGroup(row.id)) {
    ElMessage.warning('System default Point Group cannot be deleted')
    return
  }

  const referencedPoints = Object.values(store.points).reduce(
    (sum, list) => sum + list.filter(p => p.point_groups.includes(row.id)).length, 0,
  )
  const tasks = store.tasks.filter(t => t.point_group === row.id)
  if (referencedPoints || tasks.length) {
    ElMessage.warning(
      'Cannot delete: referenced by ' + referencedPoints + ' local point definition(s) and ' +
      tasks.length + ' Task(s)',
    )
    return
  }

  await ElMessageBox.confirm(
    'Delete Point Group "' + row.id + '"? References will not be migrated automatically.',
    'Delete Point Group',
    { type: 'warning', confirmButtonText: 'Delete' },
  )
  store.pointGroups.splice(store.pointGroups.findIndex(g => g.id === row.id), 1)
  if (groupEditingId.value === row.id) newGroup()
  ElMessage.success('Point group deleted (mock)')
}

// ---- Add / Edit Point ----
const pointEdit = ref(false)
const editing = ref('')
const pointSnapshot = ref('')
const draft = reactive({
  point_id: '',
  variable_name: '',
  point_groups: ['all'] as string[],
  symbol: '',
  index_group: '',
  index_offset: '',
  register_type: 'holding',
  address: undefined as number | undefined,
  ioa: undefined as number | undefined,
  ioa_type: '',
  data_type: 'float32',
  scale: 1,
  offset: 0,
  unit: 'none',
  description: '',
})

function resetDraft() {
  draft.point_id = ''
  draft.variable_name = ''
  draft.point_groups = ['all']
  draft.symbol = ''
  draft.index_group = ''
  draft.index_offset = ''
  draft.register_type = 'holding'
  draft.address = undefined
  draft.ioa = undefined
  draft.ioa_type = ''
  draft.data_type = 'float32'
  draft.scale = 1
  draft.offset = 0
  draft.unit = 'none'
  draft.description = ''
}

function openAdd() {
  if (currentTableIsSystem.value) {
    ElMessage.warning('Default Point Tables are placeholders and cannot contain points')
    return
  }
  editing.value = ''
  resetDraft()
  resetPointTest()
  pointEdit.value = true
}

const pointDraftState = computed(() => JSON.stringify(draft))
const pointDirty = computed(() => !!editing.value && pointDraftState.value !== pointSnapshot.value)

async function beforePointClose(done:()=>void){
  if(!pointDirty.value){done();return}
  try{
    await ElMessageBox.confirm('Discard unsaved Point changes?','Unsaved Changes',{type:'warning',confirmButtonText:'Discard'})
    done()
  }catch{}
}
async function closePointEditor(){
  if(!pointDirty.value){pointEdit.value=false;return}
  try{
    await ElMessageBox.confirm('Discard unsaved Point changes?','Unsaved Changes',{type:'warning',confirmButtonText:'Discard'})
    pointEdit.value=false
  }catch{}
}

function openEdit(p: PointDef) {
  editing.value = p.point_id
  resetDraft()
  draft.point_id = p.point_id
  draft.variable_name = p.variable_name
  draft.point_groups = [...p.point_groups]
  draft.symbol = p.address.symbol || ''
  draft.index_group = p.address.index_group || ''
  draft.index_offset = p.address.index_offset || ''
  draft.register_type = p.address.type || 'holding'
  draft.address = p.address.address
  draft.ioa = p.address.ioa
  draft.ioa_type = protocol.value === 'iec104' ? (p.address.type || '') : ''
  draft.data_type = p.data_type
  draft.scale = p.scale
  draft.offset = p.offset
  draft.unit = p.unit
  draft.description = p.description
  resetPointTest()
  pointSnapshot.value = JSON.stringify(draft)
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

async function savePoint() {
  const id = draft.point_id.trim()
  const list = store.points[pointTable.value]
  if (!list) return
  if (!id) {
    ElMessage.error('Point ID is required')
    return
  }
  if (editing.value && id !== editing.value) {
    ElMessage.warning('Point ID is stable after creation')
    return
  }
  if (!editing.value && pointsOfTable(pointTable.value).some(p => p.point_id === id)) {
    ElMessage.error('Point ID "' + id + '" already exists in this Point Table')
    return
  }
  if (!draft.point_groups.length) {
    ElMessage.error('point_groups must be non-empty')
    return
  }
  const address = draftAddress()
  const err = validateAddress(protocol.value, address)
  if (err) {
    ElMessage.error(err)
    return
  }
  if (!DATA_TYPES.includes(draft.data_type)) {
    ElMessage.error('Invalid data_type')
    return
  }
  if (!store.units[draft.unit]) {
    ElMessage.error('Invalid unit')
    return
  }

  const row: PointDef = {
    point_id: id,
    variable_name: draft.variable_name.trim(),
    point_groups: [...draft.point_groups],
    address,
    data_type: draft.data_type,
    scale: draft.scale,
    offset: draft.offset,
    unit: draft.unit,
    description: draft.description.trim(),
  }

  if (editing.value) {
    await confirmTableImpact(pointTable.value, 'Point Change Impact',
      originOf({ ...row, point_id: editing.value }) === 'inherited'
        ? 'Create an override for this inherited point?'
        : 'Update this point definition?')
  }

  withAffectedTasksStopped(pointTable.value, () => {
    const i = list.findIndex(p => p.point_id === editing.value)
    if (i >= 0) list.splice(i, 1, row)
    else list.push(row)
    const table = store.pointTables.find(t => t.id === pointTable.value)
    if (table) table.remove_points = (table.remove_points || []).filter(pid => pid !== id)
  })

  pointEdit.value = false
  ElMessage.success(editing.value ? 'Point updated and applied (mock)' : 'Point added and applied (mock)')
}

async function delPoint(p: PointDef) {
  const origin = originOf(p)
  await confirmTableImpact(pointTable.value, 'Point Delete Impact',
    origin === 'inherited' ? 'Exclude this inherited point from the child table?' : 'Delete this point from the effective table?')

  withAffectedTasksStopped(pointTable.value, () => {
    const list = store.points[pointTable.value]
    const localIndex = list.findIndex(x => x.point_id === p.point_id)
    if (localIndex >= 0) list.splice(localIndex, 1)

    const table = store.pointTables.find(t => t.id === pointTable.value)
    if (table?.extends) {
      const parentHasPoint = pointsOfTable(table.extends).some(x => x.point_id === p.point_id)
      if (parentHasPoint && !table.remove_points.includes(p.point_id)) table.remove_points.push(p.point_id)
    }
  })
  ElMessage.success(origin === 'inherited' ? 'Inherited point excluded (mock)' : 'Point deleted (mock)')
}

async function resetOverride(p: PointDef) {
  if (originOf(p) !== 'override') return
  await confirmTableImpact(pointTable.value, 'Reset Override Impact', 'Restore the parent definition for this point?')
  withAffectedTasksStopped(pointTable.value, () => {
    const list = store.points[pointTable.value]
    const i = list.findIndex(x => x.point_id === p.point_id)
    if (i >= 0) list.splice(i, 1)
    const table = store.pointTables.find(t => t.id === pointTable.value)
    if (table) table.remove_points = table.remove_points.filter(id => id !== p.point_id)
  })
  ElMessage.success('Override reset to parent definition (mock)')
}

</script>

<template>
  <div class="points-page">
    <div class="head">
      <div><h1>Points</h1><p>Point Table、Point Group 与具体点定义</p></div>
    </div>

    <el-card shadow="never">
      <div class="point-table-toolbar">
        <div class="table-select-block">
          <div class="table-label">Point Table</div>
          <div class="table-line">
            <el-select v-model="pointTable" style="width:260px">
              <el-option v-for="t in store.pointTables" :key="t.id" :label="t.id" :value="t.id" />
            </el-select>
            <span class="table-meta">{{ protocol.toUpperCase() }}</span>
            <span v-if="tableDef?.extends" class="table-meta">extends {{ tableDef.extends }}</span>
            <span class="muted">{{ rows.length }} points</span>
          </div>
        </div>

        <div class="table-actions">
          <el-button type="primary" :disabled="currentTableIsSystem" @click="openAdd">+ Add Point</el-button>
          <el-dropdown trigger="click">
            <el-button>Actions</el-button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item @click="openManage">Manage Metadata</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
      </div>

      <el-table :data="pagedRows" height="590">
        <el-table-column label="Point"><template #default="s"><el-link :underline="false" @click="openEdit(s.row)"><b>{{ s.row.point_id }}</b></el-link></template></el-table-column>
        <el-table-column prop="variable_name" label="Variable" />
        <el-table-column :label="addrLabel"><template #default="s">{{ addressOf(s.row) }}</template></el-table-column>
        <el-table-column v-if="!isMobile" label="Source" width="105"><template #default="s"><el-tag size="small" :type="originOf(s.row)==='inherited'?'info':originOf(s.row)==='override'?'warning':''">{{ originOf(s.row) }}</el-tag></template></el-table-column>
        <el-table-column v-if="!isMobile" prop="data_type" label="Data Type" />
        <el-table-column v-if="!isMobile" label="Groups"><template #default="s"><el-tag v-for="g in s.row.point_groups" :key="g" class="group-tag">{{ g }}</el-tag></template></el-table-column>
        <el-table-column v-if="!isTablet" prop="scale" label="Scale" />
        <el-table-column v-if="!isTablet" prop="offset" label="Offset" />
        <el-table-column v-if="!isMobile" label="Unit"><template #default="s">{{ unitSymbol(s.row.unit) || s.row.unit }}</template></el-table-column>
        <el-table-column label="Operation" :width="isMobile ? 108 : 170">
          <template #default="s">
            <el-button v-if="originOf(s.row)==='override'" size="small" @click="resetOverride(s.row)">Reset to Parent</el-button>
            <el-button size="small" type="danger" plain @click="delPoint(s.row)">Delete</el-button>
          </template>
        </el-table-column>
      </el-table>
      <div class="pagination">
        <el-pagination
          v-model:current-page="pointPage"
          v-model:page-size="pointPageSize"
          :page-sizes="[20, 50, 100]"
          :total="rows.length"
          :layout="isMobile ? 'prev, pager, next' : 'total, sizes, prev, pager, next'"
        />
      </div>
    </el-card>

    <el-drawer
      v-model="manageOpen"
      title="Manage Point Metadata"
      direction="rtl"
      :size="isMobile ? '100%' : 'min(900px, 94vw)'"
      class="point-metadata-drawer"
      :before-close="beforeManageClose"
      append-to-body
      destroy-on-close
    >
      <el-tabs v-model="manageSection" class="metadata-tabs" @tab-change="onManageTabChange">
        <el-tab-pane label="Point Tables" name="table" />
        <el-tab-pane label="Point Groups" name="group" />
      </el-tabs>

      <div class="metadata-layout">
        <div class="metadata-list-pane">
          <el-table
            :data="manageSection === 'table' ? tableRows : groupRows"
            row-key="id"
            :show-header="false"
            highlight-current-row
            :current-row-key="manageSection === 'table' ? tableEditingId : groupEditingId"
            class="metadata-object-table"
            @row-click="onManageRowClick"
          >
            <el-table-column min-width="240">
              <template #default="{ row }">
                <div class="metadata-object-info">
                  <template v-if="manageSection === 'table'">
                    <b>{{ row.id }}</b>
                    <small>{{ row.protocol.toUpperCase() }}<template v-if="row.extends"> · extends {{ row.extends }}</template></small>
                    <small>{{ row.points }} effective · {{ row.localPoints }} local · {{ row.inheritedPoints }} inherited</small>
                    <small>{{ row.models }} models · {{ row.devices }} devices · {{ row.childTables }} child tables</small>
                  </template>
                  <template v-else>
                    <b>{{ row.name }}</b>
                    <small>{{ row.id }}</small>
                    <small>{{ row.points }} points · {{ row.tasks }} tasks</small>
                  </template>
                </div>
              </template>
            </el-table-column>
            <el-table-column width="82" align="right">
              <template #default="{ row }">
                <el-tag v-if="row.system" type="info" size="small">System</el-tag>
                <template v-else-if="manageSection === 'table'">
                  <el-tooltip
                    :disabled="!tableDeleteBlocked(row)"
                    :content="tableDeleteBlockerText(row)"
                    placement="left"
                  >
                    <span>
                      <el-button
                        link
                        type="danger"
                        :disabled="tableDeleteBlocked(row)"
                        @click.stop="deleteTable(row)"
                      >Delete</el-button>
                    </span>
                  </el-tooltip>
                </template>
                <el-button
                  v-else
                  link
                  type="danger"
                  @click.stop="deleteGroup(row)"
                >Delete</el-button>
              </template>
            </el-table-column>
          </el-table>
        </div>

        <div class="metadata-editor-main">
          <div class="metadata-editor-title">
            <h3>{{ manageSection === 'table' ? (tableEditingId ? 'Edit Table' : 'New Table') : (groupEditingId ? 'Edit Group' : 'New Group') }}</h3>
            <p>{{ manageSection === 'table' ? tableEditingId : groupEditingId }}</p>
          </div>

          <el-form v-if="manageSection === 'table'" label-position="top">
            <el-form-item label="Table ID"><el-input v-model="tableDraft.id" :disabled="!!tableEditingId" /></el-form-item>
            <el-form-item label="Protocol">
              <el-select v-model="tableDraft.protocol" style="width:100%" :disabled="!!editingTable?.system" @change="tableDraft.extends = ''">
                <el-option v-for="p in PROTOCOLS" :key="p" :label="p.toUpperCase()" :value="p" />
              </el-select>
              <div v-if="editingTable?.system" class="field-note">System default Point Tables are fixed placeholders.</div>
            </el-form-item>
            <el-form-item label="Extends">
              <el-select v-model="tableDraft.extends" style="width:100%">
                <el-option label="No Base Table" value="" />
                <el-option v-for="t in parentTables" :key="t.id" :label="t.id" :value="t.id" />
              </el-select>
              <div class="field-note">Only protocol-compatible parents that cannot create an inheritance cycle are shown.</div>
            </el-form-item>
            <el-alert
              v-if="tableEditingId && (editingTableReferences.childTables.length || editingTableReferences.models.length)"
              type="warning"
              :closable="false"
              class="table-reference-alert"
            >
              <template #title>Deletion blocked by references</template>
              <div v-if="editingTableReferences.childTables.length">
                Child Tables: {{ editingTableReferences.childTables.join(', ') }}
              </div>
              <div v-if="editingTableReferences.models.length">
                Device Models: {{ editingTableReferences.models.join(', ') }}
              </div>
            </el-alert>
            <div class="metadata-editor-actions">
              <el-button v-if="!tableEditingId" @click="newTable">Clear</el-button>
              <el-button type="primary" :disabled="!!tableEditingId && !tableDirty" @click="saveTable">{{ tableEditingId ? 'Save' : 'Create' }}</el-button>
            </div>
          </el-form>

          <el-form v-else label-position="top">
            <el-form-item label="Group ID"><el-input v-model="groupDraft.id" :disabled="!!groupEditingId" /></el-form-item>
            <el-form-item label="Name"><el-input v-model="groupDraft.name" /></el-form-item>
            <div class="metadata-editor-actions">
              <el-button v-if="!groupEditingId" @click="newGroup">Clear</el-button>
              <el-button type="primary" :disabled="!!groupEditingId && !groupDirty" @click="saveGroup">{{ groupEditingId ? 'Save' : 'Create' }}</el-button>
            </div>
          </el-form>
        </div>
      </div>
    </el-drawer>

    <el-drawer
      v-model="pointEdit"
      :title="editing ? 'Edit Point' : 'Add Point'"
      direction="rtl"
      :size="isMobile ? '100%' : isTablet ? '92%' : 'min(1120px, 86vw)'"
      append-to-body
      destroy-on-close
      class="point-editor-drawer"
      :before-close="beforePointClose"
    >
      <el-row :gutter="24">
        <el-col :xs="24" :sm="24" :md="24" :lg="14">
          <section class="point-editor-section">
            <div class="point-editor-heading">
              <h3>Point Definition</h3>
              <p>Edit the point definition. Unsaved address changes are used by the test.</p>
            </div>
            <el-form label-position="top">
              <div class="grid">
                <el-form-item label="Point ID"><el-input v-model="draft.point_id" :disabled="!!editing" /></el-form-item>
                <el-form-item label="Variable Name"><el-input v-model="draft.variable_name" /></el-form-item>
                <template v-if="protocol === 'ads'">
                  <el-form-item label="Symbol"><el-input v-model="draft.symbol" placeholder="MAIN.rotorSpeed" /></el-form-item>
                  <el-form-item label="Index Group"><el-input v-model="draft.index_group" placeholder="0x4020" /></el-form-item>
                  <el-form-item label="Index Offset"><el-input v-model="draft.index_offset" placeholder="0x1234" /></el-form-item>
                </template>
                <template v-else-if="protocol === 'modbus'">
                  <el-form-item label="Register Type"><el-select v-model="draft.register_type"><el-option v-for="r in MODBUS_REGISTER_TYPES" :key="r" :label="r" :value="r" /></el-select></el-form-item>
                  <el-form-item label="Address (0-based)"><el-input-number v-model="draft.address" :min="0" :controls="false" style="width:100%" /></el-form-item>
                </template>
                <template v-else>
                  <el-form-item label="IOA"><el-input-number v-model="draft.ioa" :min="0" :max="16777215" :controls="false" style="width:100%" /></el-form-item>
                  <el-form-item label="ASDU Type"><el-input v-model="draft.ioa_type" /></el-form-item>
                </template>
                <el-form-item label="Point Groups"><el-select v-model="draft.point_groups" multiple><el-option v-for="g in store.pointGroups" :key="g.id" :label="g.name + ' · ' + g.id" :value="g.id" :disabled="!!g.system && !draft.point_groups.includes(g.id)" /></el-select></el-form-item>
                <el-form-item label="Data Type"><el-select v-model="draft.data_type"><el-option v-for="t in DATA_TYPES" :key="t" :label="t" :value="t" /></el-select></el-form-item>
                <el-form-item label="Scale"><el-input-number v-model="draft.scale" /></el-form-item>
                <el-form-item label="Offset"><el-input-number v-model="draft.offset" /></el-form-item>
                <el-form-item label="Unit"><el-select v-model="draft.unit"><el-option v-for="(u, id) in store.units" :key="id" :label="id + (u.symbol ? ' (' + u.symbol + ')' : '')" :value="id" /></el-select></el-form-item>
                <el-form-item label="Description"><el-input v-model="draft.description" /></el-form-item>
              </div>
            </el-form>
          </section>
        </el-col>

        <el-col :xs="24" :sm="24" :md="24" :lg="10">
          <el-card shadow="never" class="point-test-card">
            <div class="point-editor-heading">
              <h3>Connectivity Test</h3>
              <p>Select a {{ protocol.toUpperCase() }} device and test the current unsaved definition.</p>
            </div>

            <el-form label-position="top">
              <el-form-item label="Device">
                <el-select v-model="testDeviceId" style="width:100%" :disabled="testLoading" placeholder="Select device">
                  <el-option
                    v-for="d in testDevices"
                    :key="d.device_id"
                    :label="d.device_id + ' · ' + d.host"
                    :value="d.device_id"
                  >
                    <span>{{ d.device_id }} · {{ d.host }}</span>
                    <span class="device-state">{{ d.online ? 'online' : 'offline' }}</span>
                  </el-option>
                </el-select>
              </el-form-item>
            </el-form>

            <el-descriptions :column="1" size="small" border class="test-request">
              <el-descriptions-item label="Protocol">{{ protocol.toUpperCase() }}</el-descriptions-item>
              <el-descriptions-item label="Request">{{ testRequest }}</el-descriptions-item>
              <el-descriptions-item v-if="selectedTestDevice" label="Target">
                {{ selectedTestDevice.host }}:{{ selectedTestDevice.port || '—' }}
              </el-descriptions-item>
            </el-descriptions>

            <div class="test-actions">
              <el-button type="primary" :loading="testLoading" :disabled="!testDeviceId" @click="runPointTest">
                Test Read
              </el-button>
              <span v-if="testLatency && !testLoading" class="muted">{{ testLatency }} ms</span>
            </div>

            <el-alert v-if="testError" :title="testError" type="error" :closable="false" show-icon />

            <div class="test-results-title">Interpretations</div>
            <el-table :data="testCandidates" size="small" class="test-results-table">
              <el-table-column prop="type" label="Type" width="96" />
              <el-table-column prop="value" label="Value" min-width="120" />
            </el-table>
          </el-card>
        </el-col>
      </el-row>
      <template #footer><el-button @click="closePointEditor">Cancel</el-button><el-button type="primary" :disabled="!!editing && !pointDirty" @click="savePoint">Save</el-button></template>
    </el-drawer>
  </div>
</template>

<style scoped>
.point-table-toolbar{display:flex;align-items:flex-end;justify-content:space-between;gap:var(--app-toolbar-gap);margin-bottom:14px}
.table-label{margin-bottom:7px;color:var(--app-text-secondary);font-size:var(--app-font-body);font-weight:var(--app-font-weight-semibold)}
.table-line,.table-actions,.metadata-editor-actions{display:flex;align-items:center;gap:var(--app-space-2)}
.table-actions{flex-wrap:wrap;justify-content:flex-end}
.table-meta{color:var(--app-text-regular);font-size:var(--app-font-label)}
.muted,.field-note{color:var(--app-text-muted);font-size:var(--app-font-label)}
.group-tag{margin-right:4px;margin-bottom:2px}
.metadata-tabs{margin-top:-8px}
.metadata-layout{display:grid;grid-template-columns:340px minmax(0,1fr);min-height:440px}
.metadata-list-pane{min-width:0;padding-right:12px;border-right:1px solid var(--app-border-soft)}
.metadata-object-table{width:100%;cursor:pointer}
.metadata-object-table :deep(.el-table__inner-wrapper::before){display:none}
.metadata-object-table :deep(.el-table__cell){padding:9px 0!important}
.metadata-object-table :deep(.el-table__row.current-row>td.el-table__cell){background:#f4f7fb}
.metadata-object-info{min-width:0;padding-left:4px}
.metadata-object-info b,.metadata-object-info small{display:block}
.metadata-object-info b{font-size:var(--app-font-body);font-weight:var(--app-font-weight-semibold);color:var(--app-text-primary)}
.metadata-object-info small{margin-top:4px;color:var(--app-text-muted);font-size:var(--app-font-caption);white-space:normal;line-height:var(--app-line-height-compact)}
.metadata-editor-main{min-width:0;padding:4px 8px 4px 24px}
.metadata-editor-title{margin-bottom:16px}.metadata-editor-title h3{margin:0;font-size:var(--app-font-section-title);font-weight:var(--app-font-weight-semibold)}
.metadata-editor-title p{margin:4px 0 0;color:var(--app-text-muted);font-size:var(--app-font-caption)}
.metadata-editor-actions{justify-content:flex-end;margin-top:8px}
.point-editor-section,.point-test-card{min-width:0}
.point-editor-heading{margin-bottom:var(--app-space-4)}
.point-editor-heading h3{margin:0;color:var(--app-text-primary);font-size:var(--app-font-section-title);font-weight:var(--app-font-weight-semibold)}
.point-editor-heading p{margin:4px 0 0;color:var(--app-text-muted);font-size:var(--app-font-caption);line-height:var(--app-line-height-compact)}
.point-test-card{height:auto}
.test-request{margin-top:var(--app-space-2)}
.test-actions{display:flex;align-items:center;gap:var(--app-space-2);margin-top:var(--app-space-3);margin-bottom:var(--app-space-3)}
.test-results-title{margin:var(--app-space-3) 0 var(--app-space-2);color:var(--app-text-primary);font-size:var(--app-font-body);font-weight:var(--app-font-weight-semibold)}
.test-results-table{width:100%}
.device-state{float:right;margin-left:var(--app-space-3);color:var(--app-text-muted);font-size:var(--app-font-caption)}
@media(max-width:1199px){.point-test-card{margin-top:var(--app-space-4)}}
@media(max-width:1199px){.point-table-toolbar{align-items:flex-start;flex-direction:column}.table-actions{justify-content:flex-start}.metadata-layout{grid-template-columns:1fr}.metadata-list-pane{border-right:0;border-bottom:1px solid var(--app-border-soft);padding:0 0 12px}.metadata-editor-main{padding:16px 0 0}}
</style>
