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
const dirty = ref(false)
const viewportWidth = ref(window.innerWidth)
const isMobile = computed(() => viewportWidth.value < 768)
const isTablet = computed(() => viewportWidth.value < 1200)
function updateViewport() { viewportWidth.value = window.innerWidth }
window.addEventListener('resize', updateViewport)
onBeforeUnmount(() => window.removeEventListener('resize', updateViewport))

const addrLabel = computed(() =>
  protocol.value === 'ads'
    ? 'Symbol / Index'
    : protocol.value === 'modbus'
      ? 'Type / Address'
      : 'IOA',
)

const addressOf = (p: PointDef) => addressText(protocol.value, p)

// ---- Point Table management ----
const tableEdit = ref(false)
const tableEditing = ref(false)
const tableDraft = reactive({
  id: '',
  protocol: 'modbus' as Protocol,
  extends: '',
})

const parentTables = computed(() =>
  store.pointTables.filter(
    t => t.protocol === tableDraft.protocol && t.id !== tableDraft.id,
  ),
)

function openNewTable() {
  tableEditing.value = false
  tableDraft.id = ''
  tableDraft.protocol = 'modbus'
  tableDraft.extends = ''
  tableEdit.value = true
}

function openEditTable() {
  if (!tableDef.value) return
  tableEditing.value = true
  tableDraft.id = tableDef.value.id
  tableDraft.protocol = tableDef.value.protocol
  tableDraft.extends = tableDef.value.extends || ''
  tableEdit.value = true
}

function saveTable() {
  const id = tableDraft.id.trim()
  if (!id) {
    ElMessage.error('Point Table ID is required')
    return
  }

  if (!tableEditing.value && store.pointTables.some(t => t.id === id)) {
    ElMessage.error(`Point table "${id}" already exists`)
    return
  }

  if (tableDraft.extends) {
    const parent = store.pointTables.find(t => t.id === tableDraft.extends)
    if (!parent || parent.protocol !== tableDraft.protocol) {
      ElMessage.error('Parent table must use the same protocol')
      return
    }
  }

  if (tableEditing.value) {
    const target = store.pointTables.find(t => t.id === id)
    if (!target) return

    if (target.protocol !== tableDraft.protocol && pointsOfTable(id).length > 0) {
      ElMessage.error('Protocol cannot be changed while the point table contains points')
      return
    }

    target.protocol = tableDraft.protocol
    target.extends = tableDraft.extends
    dirty.value = true
    tableEdit.value = false
    ElMessage.success('Point table updated (mock)')
    return
  }

  store.pointTables.push({
    id,
    protocol: tableDraft.protocol,
    extends: tableDraft.extends,
  })

  store.points[id] = tableDraft.extends
    ? pointsOfTable(tableDraft.extends).map(p => ({
        ...p,
        address: { ...p.address },
        point_groups: [...p.point_groups],
      }))
    : []

  pointTable.value = id
  dirty.value = true
  tableEdit.value = false
  ElMessage.success('Point table created (mock)')
}

// ---- Point Group management ----
const groupManage = ref(false)
const groupEdit = ref(false)
const groupEditingId = ref('')
const groupDraft = reactive({ id: '', name: '' })

const groupRows = computed(() =>
  store.pointGroups.map(g => ({
    ...g,
    points: Object.values(store.points).reduce(
      (sum, list) => sum + list.filter(p => p.point_groups.includes(g.id)).length,
      0,
    ),
    tasks: store.tasks.filter(t => t.point_group === g.id).length,
  })),
)

function openNewGroup() {
  groupEditingId.value = ''
  groupDraft.id = ''
  groupDraft.name = ''
  groupEdit.value = true
}

function openEditGroup(row: { id: string; name: string }) {
  groupEditingId.value = row.id
  groupDraft.id = row.id
  groupDraft.name = row.name
  groupEdit.value = true
}

function saveGroup() {
  const id = groupDraft.id.trim()
  if (!id) {
    ElMessage.error('Point Group ID is required')
    return
  }

  if (!groupEditingId.value && store.pointGroups.some(g => g.id === id)) {
    ElMessage.error(`Point group "${id}" already exists`)
    return
  }

  if (groupEditingId.value) {
    const target = store.pointGroups.find(g => g.id === groupEditingId.value)
    if (target) target.name = groupDraft.name.trim() || target.id
  } else {
    store.pointGroups.push({ id, name: groupDraft.name.trim() || id })
  }

  groupEdit.value = false
  dirty.value = true
  ElMessage.success(groupEditingId.value ? 'Point group updated (mock)' : 'Point group created (mock)')
}

async function deleteGroup(row: { id: string; points: number; tasks: number }) {
  if (row.points || row.tasks) {
    ElMessage.warning(`Cannot delete: referenced by ${row.points} point(s) and ${row.tasks} task(s)`)
    return
  }

  await ElMessageBox.confirm(`Delete point group "${row.id}"?`, 'Delete Point Group', { type: 'warning' })
  store.pointGroups.splice(store.pointGroups.findIndex(g => g.id === row.id), 1)
  dirty.value = true
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

  if (protocol.value === 'modbus') {
    return { type: draft.register_type, address: draft.address }
  }

  return {
    ioa: draft.ioa,
    ...(draft.ioa_type.trim() ? { type: draft.ioa_type.trim() } : {}),
  }
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
    ElMessage.error(`Point ${id} already exists`)
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

function save() {
  dirty.value = false
  ElMessage.success(`${pointTable.value} saved (mock)`)
}

function saveApply() {
  dirty.value = false
  ElMessage.success(`${pointTable.value} saved & reload applied (mock)`)
}
</script>

<template>
  <div>
    <div class="head">
      <div>
        <h1>Points</h1>
        <p>Point Table、Point Group 与具体点定义</p>
      </div>
      <div>
        <el-button @click="save">Save</el-button>
        <el-button type="primary" @click="saveApply">Save & Apply</el-button>
      </div>
    </div>

    <el-card shadow="never">
      <div class="point-table-toolbar">
        <div class="table-select-block">
          <div class="table-label">Point Table</div>
          <div class="table-line">
            <el-select v-model="pointTable" style="width: 260px">
              <el-option v-for="t in store.pointTables" :key="t.id" :label="t.id" :value="t.id" />
            </el-select>
            <el-tag>{{ protocol.toUpperCase() }}</el-tag>
            <el-tag v-if="tableDef?.extends" type="info">extends {{ tableDef.extends }}</el-tag>
            <span class="muted">{{ rows.length }} points</span>
            <el-tag v-if="dirty" type="warning">Pending Apply</el-tag>
          </div>
        </div>

        <div class="table-actions">
          <el-button @click="openNewTable">New Table</el-button>
          <el-button @click="openEditTable">Edit Table</el-button>
          <el-button @click="groupManage = true">Manage Groups</el-button>
          <el-button type="primary" @click="openAdd">+ Add Point</el-button>
        </div>
      </div>

      <el-table :data="rows" height="590">
        <el-table-column prop="point_id" label="Point" />
        <el-table-column prop="variable_name" label="Variable" />
        <el-table-column :label="addrLabel">
          <template #default="s">{{ addressOf(s.row) }}</template>
        </el-table-column>
        <el-table-column v-if="!isMobile" prop="data_type" label="Data Type" />
        <el-table-column v-if="!isMobile" label="Groups">
          <template #default="s">
            <el-tag v-for="g in s.row.point_groups" :key="g" class="group-tag">{{ g }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column v-if="!isTablet" prop="scale" label="Scale" />
        <el-table-column v-if="!isTablet" prop="offset" label="Offset" />
        <el-table-column v-if="!isMobile" label="Unit">
          <template #default="s">{{ unitSymbol(s.row.unit) || s.row.unit }}</template>
        </el-table-column>
        <el-table-column label="Actions" :width="isMobile ? 128 : 150">
          <template #default="s">
            <el-button size="small" @click="openEdit(s.row)">Edit</el-button>
            <el-button size="small" type="danger" plain @click="delPoint(s.row)">Delete</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-dialog
      v-model="tableEdit"
      :title="tableEditing ? 'Edit Point Table' : 'New Point Table'"
      width="500"
    >
      <el-form label-position="top">
        <el-form-item label="Table ID">
          <el-input v-model="tableDraft.id" :disabled="tableEditing" />
        </el-form-item>

        <el-form-item label="Protocol">
          <el-select
            v-model="tableDraft.protocol"
            style="width: 100%"
            :disabled="tableEditing && rows.length > 0"
            @change="tableDraft.extends = ''"
          >
            <el-option v-for="p in PROTOCOLS" :key="p" :label="p.toUpperCase()" :value="p" />
          </el-select>
          <div v-if="tableEditing && rows.length > 0" class="field-note">
            Protocol cannot be changed while this table contains points.
          </div>
        </el-form-item>

        <el-form-item label="Extends">
          <el-select v-model="tableDraft.extends" clearable style="width: 100%">
            <el-option v-for="t in parentTables" :key="t.id" :label="t.id" :value="t.id" />
          </el-select>
        </el-form-item>
      </el-form>

      <template #footer>
        <el-button @click="tableEdit = false">Cancel</el-button>
        <el-button type="primary" @click="saveTable">{{ tableEditing ? 'Update' : 'Create' }}</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="groupManage" title="Point Groups" width="720">
      <div class="group-manage-head">
        <span class="muted">Point Group 用于组织点并被采集任务引用。</span>
        <el-button type="primary" @click="openNewGroup">+ Add Group</el-button>
      </div>

      <el-table :data="groupRows">
        <el-table-column prop="id" label="Group ID" min-width="160" />
        <el-table-column prop="name" label="Name" min-width="210" />
        <el-table-column prop="points" label="Points" width="90" />
        <el-table-column prop="tasks" label="Tasks" width="90" />
        <el-table-column label="Actions" width="130" align="right">
          <template #default="{ row }">
            <el-button link type="primary" @click="openEditGroup(row)">Edit</el-button>
            <el-button link type="danger" @click="deleteGroup(row)">Delete</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-dialog>

    <el-dialog
      v-model="groupEdit"
      :title="groupEditingId ? 'Edit Point Group' : 'Add Point Group'"
      width="460"
      append-to-body
    >
      <el-form label-position="top">
        <el-form-item label="Group ID">
          <el-input v-model="groupDraft.id" :disabled="!!groupEditingId" />
        </el-form-item>
        <el-form-item label="Name">
          <el-input v-model="groupDraft.name" />
        </el-form-item>
      </el-form>

      <template #footer>
        <el-button @click="groupEdit = false">Cancel</el-button>
        <el-button type="primary" @click="saveGroup">{{ groupEditingId ? 'Update' : 'Create' }}</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="pointEdit" :title="editing ? 'Edit Point' : 'Add Point'" width="760">
      <el-form label-position="top">
        <div class="grid">
          <el-form-item label="Point ID">
            <el-input v-model="draft.point_id" :disabled="!!editing" />
          </el-form-item>
          <el-form-item label="Variable Name">
            <el-input v-model="draft.variable_name" />
          </el-form-item>

          <template v-if="protocol === 'ads'">
            <el-form-item label="Symbol">
              <el-input v-model="draft.symbol" placeholder="MAIN.rotorSpeed" />
            </el-form-item>
            <el-form-item label="Index Group">
              <el-input v-model="draft.index_group" placeholder="0x4020" />
            </el-form-item>
            <el-form-item label="Index Offset">
              <el-input v-model="draft.index_offset" placeholder="0x1234" />
            </el-form-item>
          </template>

          <template v-else-if="protocol === 'modbus'">
            <el-form-item label="Register Type">
              <el-select v-model="draft.register_type">
                <el-option v-for="r in MODBUS_REGISTER_TYPES" :key="r" :label="r" :value="r" />
              </el-select>
            </el-form-item>
            <el-form-item label="Address (0-based)">
              <el-input-number v-model="draft.address" :min="0" :controls="false" style="width: 100%" />
            </el-form-item>
          </template>

          <template v-else>
            <el-form-item label="IOA">
              <el-input-number
                v-model="draft.ioa"
                :min="0"
                :max="16777215"
                :controls="false"
                style="width: 100%"
              />
            </el-form-item>
            <el-form-item label="Type (optional)">
              <el-input v-model="draft.ioa_type" placeholder="measured_value" />
            </el-form-item>
          </template>

          <el-form-item label="Data Type">
            <el-select v-model="draft.data_type">
              <el-option v-for="x in DATA_TYPES" :key="x" :label="x" :value="x" />
            </el-select>
          </el-form-item>

          <el-form-item label="Point Groups">
            <el-select v-model="draft.point_groups" multiple>
              <el-option v-for="g in store.pointGroups" :key="g.id" :label="g.id" :value="g.id" />
            </el-select>
          </el-form-item>

          <el-form-item label="Scale">
            <el-input-number v-model="draft.scale" />
          </el-form-item>

          <el-form-item label="Offset">
            <el-input-number v-model="draft.offset" />
          </el-form-item>

          <el-form-item label="Unit">
            <el-select v-model="draft.unit">
              <el-option
                v-for="(u, id) in store.units"
                :key="id"
                :label="id + (u.symbol ? ` (${u.symbol})` : '')"
                :value="id"
              />
            </el-select>
          </el-form-item>

          <el-form-item label="Description">
            <el-input v-model="draft.description" />
          </el-form-item>
        </div>
      </el-form>

      <template #footer>
        <el-button @click="pointEdit = false">Cancel</el-button>
        <el-button type="primary" @click="savePoint">Save</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.point-table-toolbar {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 18px;
  margin-bottom: 14px;
}
.table-label {
  margin-bottom: 7px;
  color: #6f7a8a;
  font-size: 12px;
  font-weight: 600;
}
.table-line,
.table-actions,
.group-manage-head {
  display: flex;
  align-items: center;
  gap: 8px;
}
.table-actions {
  flex-wrap: wrap;
  justify-content: flex-end;
}
.muted {
  color: #8a94a3;
  font-size: 12px;
}
.group-tag {
  margin-right: 4px;
  margin-bottom: 2px;
}
.group-manage-head {
  justify-content: space-between;
  margin-bottom: 12px;
}
.field-note {
  margin-top: 6px;
  color: #929cab;
  font-size: 11px;
}
@media (max-width: 1050px) {
  .point-table-toolbar {
    align-items: flex-start;
    flex-direction: column;
  }
  .table-actions {
    justify-content: flex-start;
  }
}
</style>
