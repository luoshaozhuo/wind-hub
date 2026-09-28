<script setup lang="ts">
import { computed, onBeforeUnmount, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { addressText, pointsOfTable, store, tableProtocol, unitSymbol, validateAddress } from '../mock/data'
import { DATA_TYPES, MODBUS_REGISTER_TYPES, PROTOCOLS } from '../mock/types'
import type { PointAddress, PointDef, Protocol } from '../mock/types'

const pointTable = ref('beckhoff_wtg_v1')
const protocol = computed(() => tableProtocol(pointTable.value) || 'ads')
const tableDef = computed(() => store.pointTables.find(t => t.id === pointTable.value))
const rows = computed<PointDef[]>(() => pointsOfTable(pointTable.value))
const viewportWidth = ref(window.innerWidth)
const isMobile = computed(() => viewportWidth.value < 768)
const isTablet = computed(() => viewportWidth.value < 1200)
function updateViewport() { viewportWidth.value = window.innerWidth }
window.addEventListener('resize', updateViewport)
onBeforeUnmount(() => window.removeEventListener('resize', updateViewport))

const addrLabel = computed(() =>
  protocol.value === 'ads' ? 'Symbol / Index' : protocol.value === 'modbus' ? 'Type / Address' : 'IOA',
)
const addressOf = (p: PointDef) => addressText(protocol.value, p)

// ---- Point metadata management ----
type ManageSection = 'table' | 'group'
const manageOpen = ref(false)
const manageSection = ref<ManageSection>('table')
const tableEditingId = ref('')
const groupEditingId = ref('')

const tableDraft = reactive({ id: '', protocol: 'modbus' as Protocol, extends: '' })
const groupDraft = reactive({ id: '', name: '' })

const parentTables = computed(() =>
  store.pointTables.filter(t => t.protocol === tableDraft.protocol && t.id !== tableDraft.id),
)

const tableRows = computed(() => store.pointTables.map(t => ({
  ...t,
  points: pointsOfTable(t.id).length,
  models: store.deviceModels.filter(m => m.point_table === t.id).length,
})))

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
}

function saveTable() {
  const id = tableDraft.id.trim()
  if (!id) {
    ElMessage.error('Point Table ID is required')
    return
  }
  if (!tableEditingId.value && store.pointTables.some(t => t.id === id)) {
    ElMessage.error('Point table "' + id + '" already exists')
    return
  }
  if (tableDraft.extends) {
    const parent = store.pointTables.find(t => t.id === tableDraft.extends)
    if (!parent || parent.protocol !== tableDraft.protocol) {
      ElMessage.error('Parent table must use the same protocol')
      return
    }
  }
  if (tableEditingId.value) {
    const target = store.pointTables.find(t => t.id === tableEditingId.value)
    if (!target) return
    if (target.protocol !== tableDraft.protocol && pointsOfTable(target.id).length > 0) {
      ElMessage.error('Protocol cannot be changed while the point table contains points')
      return
    }
    target.protocol = tableDraft.protocol
    target.extends = tableDraft.extends
    ElMessage.success('Point table updated (mock)')
  } else {
    store.pointTables.push({ id, protocol: tableDraft.protocol, extends: tableDraft.extends })
    store.points[id] = tableDraft.extends
      ? pointsOfTable(tableDraft.extends).map(p => ({ ...p, address: { ...p.address }, point_groups: [...p.point_groups] }))
      : []
    pointTable.value = id
    ElMessage.success('Point table created (mock)')
  }
  newTable()
}

async function deleteTable(row: { id: string; points: number; models: number }) {
  if (row.models) {
    ElMessage.warning('Cannot delete: referenced by ' + row.models + ' device model(s)')
    return
  }
  if (row.points) {
    ElMessage.warning('Cannot delete: table still contains ' + row.points + ' point(s)')
    return
  }
  await ElMessageBox.confirm('Delete point table "' + row.id + '"?', 'Delete Point Table', { type: 'warning' })
  store.pointTables.splice(store.pointTables.findIndex(t => t.id === row.id), 1)
  delete store.points[row.id]
  if (pointTable.value === row.id) pointTable.value = store.pointTables[0]?.id || ''
  if (tableEditingId.value === row.id) newTable()
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
}

function saveGroup() {
  const id = groupDraft.id.trim()
  if (!id) {
    ElMessage.error('Point Group ID is required')
    return
  }
  if (!groupEditingId.value && store.pointGroups.some(g => g.id === id)) {
    ElMessage.error('Point group "' + id + '" already exists')
    return
  }
  if (groupEditingId.value) {
    const target = store.pointGroups.find(g => g.id === groupEditingId.value)
    if (target) target.name = groupDraft.name.trim() || target.id
  } else {
    store.pointGroups.push({ id, name: groupDraft.name.trim() || id })
  }
  ElMessage.success(groupEditingId.value ? 'Point group updated (mock)' : 'Point group created (mock)')
  newGroup()
}

async function deleteGroup(row: { id: string; points: number; tasks: number }) {
  if (row.points || row.tasks) {
    ElMessage.warning('Cannot delete: referenced by ' + row.points + ' point(s) and ' + row.tasks + ' task(s)')
    return
  }
  await ElMessageBox.confirm('Delete point group "' + row.id + '"?', 'Delete Point Group', { type: 'warning' })
  store.pointGroups.splice(store.pointGroups.findIndex(g => g.id === row.id), 1)
  if (groupEditingId.value === row.id) newGroup()
}

// ---- Add / Edit Point ----
const pointEdit = ref(false)
const editing = ref('')
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
  editing.value = ''
  resetDraft()
  pointEdit.value = true
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
  const id = draft.point_id.trim()
  const list = store.points[pointTable.value]
  if (!list) return
  if (!id) {
    ElMessage.error('Point ID is required')
    return
  }
  if (!editing.value && list.some(p => p.point_id === id)) {
    ElMessage.error('Point ' + id + ' already exists')
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
    const i = list.findIndex(p => p.point_id === editing.value)
    if (i >= 0) list.splice(i, 1, row)
  } else {
    list.push(row)
  }
  pointEdit.value = false
  ElMessage.success(editing.value ? 'Point updated and applied (mock)' : 'Point added and applied (mock)')
}

async function delPoint(p: PointDef) {
  await ElMessageBox.confirm('删除点 ' + p.point_id + '？', 'Delete Point', { type: 'warning' })
  const list = store.points[pointTable.value]
  list.splice(list.indexOf(p), 1)
  ElMessage.success('Point deleted and applied (mock)')
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
          <el-button @click="openManage">Manage</el-button>
          <el-button type="primary" @click="openAdd">+ Add Point</el-button>
        </div>
      </div>

      <el-table :data="rows" height="590">
        <el-table-column prop="point_id" label="Point" />
        <el-table-column prop="variable_name" label="Variable" />
        <el-table-column :label="addrLabel"><template #default="s">{{ addressOf(s.row) }}</template></el-table-column>
        <el-table-column v-if="!isMobile" prop="data_type" label="Data Type" />
        <el-table-column v-if="!isMobile" label="Groups"><template #default="s"><el-tag v-for="g in s.row.point_groups" :key="g" class="group-tag">{{ g }}</el-tag></template></el-table-column>
        <el-table-column v-if="!isTablet" prop="scale" label="Scale" />
        <el-table-column v-if="!isTablet" prop="offset" label="Offset" />
        <el-table-column v-if="!isMobile" label="Unit"><template #default="s">{{ unitSymbol(s.row.unit) || s.row.unit }}</template></el-table-column>
        <el-table-column label="Actions" :width="isMobile ? 128 : 150">
          <template #default="s">
            <el-button size="small" @click="openEdit(s.row)">Edit</el-button>
            <el-button size="small" type="danger" plain @click="delPoint(s.row)">Delete</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-dialog v-model="manageOpen" title="Manage Point Metadata" :width="isMobile ? '100%' : 'min(1000px, 94vw)'" :fullscreen="isMobile">
      <div class="point-manager">
        <aside class="point-manager-nav">
          <button :class="{ active: manageSection === 'table' }" @click="selectManage('table')">
            <span><b>Point Tables</b><small>协议与继承关系</small></span><em>{{ store.pointTables.length }}</em>
          </button>
          <button :class="{ active: manageSection === 'group' }" @click="selectManage('group')">
            <span><b>Point Groups</b><small>点与任务分组</small></span><em>{{ store.pointGroups.length }}</em>
          </button>
        </aside>

        <section class="point-manager-content">
          <div class="point-manager-list">
            <div class="manager-head">
              <div><h3>{{ manageSection === 'table' ? 'Point Tables' : 'Point Groups' }}</h3><p>选择条目后在右侧编辑</p></div>
              <el-button @click="manageSection === 'table' ? newTable() : newGroup()">+ New</el-button>
            </div>

            <template v-if="manageSection === 'table'">
              <button v-for="row in tableRows" :key="row.id" :class="['manager-row', { selected: tableEditingId === row.id }]" @click="editTable(row.id)">
                <span><b>{{ row.id }}</b><small>{{ row.protocol.toUpperCase() }}<template v-if="row.extends"> · extends {{ row.extends }}</template></small><small>{{ row.points }} points · {{ row.models }} models</small></span>
                <el-button link type="danger" @click.stop="deleteTable(row)">Delete</el-button>
              </button>
            </template>

            <template v-else>
              <button v-for="row in groupRows" :key="row.id" :class="['manager-row', { selected: groupEditingId === row.id }]" @click="editGroup(row.id)">
                <span><b>{{ row.name }}</b><small>{{ row.id }}</small><small>{{ row.points }} points · {{ row.tasks }} tasks</small></span>
                <el-button link type="danger" @click.stop="deleteGroup(row)">Delete</el-button>
              </button>
            </template>
          </div>

          <div class="point-manager-editor">
            <div class="manager-head">
              <div><h3>{{ manageSection === 'table' ? (tableEditingId ? 'Edit Table' : 'New Table') : (groupEditingId ? 'Edit Group' : 'New Group') }}</h3><p>{{ manageSection === 'table' ? tableEditingId : groupEditingId }}</p></div>
            </div>

            <el-form label-position="top" v-if="manageSection === 'table'">
              <el-form-item label="Table ID"><el-input v-model="tableDraft.id" :disabled="!!tableEditingId" /></el-form-item>
              <el-form-item label="Protocol">
                <el-select v-model="tableDraft.protocol" style="width:100%" :disabled="!!tableEditingId && pointsOfTable(tableEditingId).length > 0" @change="tableDraft.extends = ''">
                  <el-option v-for="p in PROTOCOLS" :key="p" :label="p.toUpperCase()" :value="p" />
                </el-select>
                <div v-if="tableEditingId && pointsOfTable(tableEditingId).length > 0" class="field-note">Protocol cannot change while the table contains points.</div>
              </el-form-item>
              <el-form-item label="Extends">
                <el-select v-model="tableDraft.extends" clearable style="width:100%"><el-option v-for="t in parentTables" :key="t.id" :label="t.id" :value="t.id" /></el-select>
              </el-form-item>
              <div class="manager-actions"><el-button @click="newTable">Clear</el-button><el-button type="primary" @click="saveTable">{{ tableEditingId ? 'Update' : 'Create' }}</el-button></div>
            </el-form>

            <el-form label-position="top" v-else>
              <el-form-item label="Group ID"><el-input v-model="groupDraft.id" :disabled="!!groupEditingId" /></el-form-item>
              <el-form-item label="Name"><el-input v-model="groupDraft.name" /></el-form-item>
              <div class="manager-actions"><el-button @click="newGroup">Clear</el-button><el-button type="primary" @click="saveGroup">{{ groupEditingId ? 'Update' : 'Create' }}</el-button></div>
            </el-form>
          </div>
        </section>
      </div>
    </el-dialog>

    <el-dialog v-model="pointEdit" :title="editing ? 'Edit Point' : 'Add Point'" width="760">
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
          <el-form-item label="Point Groups"><el-select v-model="draft.point_groups" multiple><el-option v-for="g in store.pointGroups" :key="g.id" :label="g.name + ' · ' + g.id" :value="g.id" /></el-select></el-form-item>
          <el-form-item label="Data Type"><el-select v-model="draft.data_type"><el-option v-for="t in DATA_TYPES" :key="t" :label="t" :value="t" /></el-select></el-form-item>
          <el-form-item label="Scale"><el-input-number v-model="draft.scale" /></el-form-item>
          <el-form-item label="Offset"><el-input-number v-model="draft.offset" /></el-form-item>
          <el-form-item label="Unit"><el-select v-model="draft.unit"><el-option v-for="(u, id) in store.units" :key="id" :label="id + (u.symbol ? ' (' + u.symbol + ')' : '')" :value="id" /></el-select></el-form-item>
          <el-form-item label="Description"><el-input v-model="draft.description" /></el-form-item>
        </div>
      </el-form>
      <template #footer><el-button @click="pointEdit = false">Cancel</el-button><el-button type="primary" @click="savePoint">Save</el-button></template>
    </el-dialog>
  </div>
</template>

<style scoped>
.point-table-toolbar{display:flex;align-items:flex-end;justify-content:space-between;gap:18px;margin-bottom:14px}.table-label{margin-bottom:7px;color:#6f7a8a;font-size:12px;font-weight:600}.table-line,.table-actions,.manager-head,.manager-actions{display:flex;align-items:center;gap:8px}.table-actions{flex-wrap:wrap;justify-content:flex-end}.table-meta{color:#526071;font-size:11px}.muted{color:#8a94a3;font-size:12px}.group-tag{margin-right:4px;margin-bottom:2px}.field-note{margin-top:6px;color:#929cab;font-size:11px}
.point-manager{display:grid;grid-template-columns:190px minmax(0,1fr);min-height:500px}.point-manager-nav{padding-right:14px;border-right:1px solid #edf0f4}.point-manager-nav button{width:100%;display:flex;justify-content:space-between;gap:10px;border:0;background:transparent;padding:11px;border-radius:8px;text-align:left;cursor:pointer}.point-manager-nav button:hover,.point-manager-nav button.active{background:#f5f7fa}.point-manager-nav span b,.point-manager-nav span small{display:block}.point-manager-nav span b{font-size:12px;color:#344054}.point-manager-nav span small{margin-top:3px;color:#98a2b3;font-size:10px}.point-manager-nav em{font-style:normal;color:#98a2b3;font-size:11px}
.point-manager-content{display:grid;grid-template-columns:minmax(260px,.9fr) minmax(320px,1.1fr);min-width:0}.point-manager-list,.point-manager-editor{padding:0 18px;min-width:0}.point-manager-list{border-right:1px solid #edf0f4}.manager-head{justify-content:space-between;align-items:flex-start;margin-bottom:12px}.manager-head h3{margin:0;font-size:14px;color:#344054}.manager-head p{margin:4px 0 0;color:#98a2b3;font-size:10px}.manager-row{width:100%;display:flex;justify-content:space-between;align-items:center;gap:10px;border:0;border-bottom:1px solid #f0f2f5;background:transparent;padding:10px 4px;text-align:left;cursor:pointer}.manager-row:hover,.manager-row.selected{background:#f7f9fb}.manager-row span b,.manager-row span small{display:block}.manager-row span b{font-size:12px;color:#344054}.manager-row span small{margin-top:3px;color:#98a2b3;font-size:10px}.manager-actions{justify-content:flex-end;margin-top:8px}
@media(max-width:900px){.point-table-toolbar{align-items:flex-start;flex-direction:column}.table-actions{justify-content:flex-start}.point-manager{grid-template-columns:1fr}.point-manager-nav{display:flex;gap:6px;overflow:auto;padding:0 0 12px;border-right:0;border-bottom:1px solid #edf0f4}.point-manager-nav button{min-width:180px}.point-manager-content{grid-template-columns:1fr}.point-manager-list{border-right:0;border-bottom:1px solid #edf0f4;padding:14px 0}.point-manager-editor{padding:16px 0}}
</style>
