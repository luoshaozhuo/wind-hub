<script setup lang="ts">
import { computed, onBeforeUnmount, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { store } from '../mock/data'
import { ADS_READ_MODES, PROTOCOLS } from '../mock/types'
import type { DeviceModelDef, Protocol } from '../mock/types'

type ManageSection = 'model' | 'type' | 'group'

const manageOpen = ref(false)
const section = ref<ManageSection>('model')
const editingId = ref('')
const isMobile = ref(window.innerWidth < 768)

function onResize() {
  isMobile.value = window.innerWidth < 768
}
window.addEventListener('resize', onResize)
onBeforeUnmount(() => window.removeEventListener('resize', onResize))

const form = reactive({
  id: '',
  name: '',
  device_type: '',
  manufacturer: '',
  model: '',
  protocol: 'modbus' as Protocol,
  point_table: '',
  read_mode: 'sum',
  port: 502,
  timeout: 3,
  unit_id: 1,
  mode: 'tcp',
  word_order: 'little_endian',
  target_port: 801,
  twincat_version: '2',
  reconnect_max_retries: 5,
  reconnect_backoff_max: 30,
  common_addr: 1,
  k: 12,
  w: 8,
  t0: 30,
  t1: 15,
  t2: 10,
  t3: 20,
  max_reconnect_retries: 5,
})

const tablesOfProtocol = computed(() => store.pointTables.filter(t => t.protocol === form.protocol))

const modelRows = computed(() => store.deviceModels.map(m => ({
  ...m,
  type_name: store.deviceTypes.find(t => t.id === m.device_type)?.name || m.device_type,
  devices: store.devices.filter(d => d.model === m.id).length,
})))

const typeRows = computed(() => store.deviceTypes.map(t => ({
  ...t,
  models: store.deviceModels.filter(m => m.device_type === t.id).length,
  groups: store.deviceGroups.filter(g => g.device_type === t.id).length,
})))

const groupRows = computed(() => store.deviceGroups.map(g => ({
  ...g,
  type_name: store.deviceTypes.find(t => t.id === g.device_type)?.name || g.device_type,
  devices: store.devices.filter(d => d.device_group === g.id).length,
  tasks: store.tasks.filter(t => t.device_group === g.id).length,
})))

function resetForm() {
  editingId.value = ''
  form.id = ''
  form.name = ''
  form.device_type = store.deviceTypes[0]?.id || ''
  form.manufacturer = ''
  form.model = ''
  form.protocol = 'modbus'
  form.point_table = store.pointTables.find(t => t.protocol === 'modbus')?.id || ''
  form.read_mode = 'sum'
  form.port = 502
  form.timeout = 3
  form.unit_id = 1
  form.mode = 'tcp'
  form.word_order = 'little_endian'
  form.target_port = 801
  form.twincat_version = '2'
  form.reconnect_max_retries = 5
  form.reconnect_backoff_max = 30
  form.common_addr = 1
  form.k = 12
  form.w = 8
  form.t0 = 30
  form.t1 = 15
  form.t2 = 10
  form.t3 = 20
  form.max_reconnect_retries = 5
}

function openManager() {
  manageOpen.value = true
  section.value = 'model'
  resetForm()
}

function selectSection(next: ManageSection) {
  section.value = next
  resetForm()
}

function onSectionChange(name: string | number) {
  selectSection(String(name) as ManageSection)
}

function onItemSelect(id: string) {
  if (section.value === 'model') {
    const row = store.deviceModels.find(x => x.id === id)
    if (row) openEditModel(row)
  } else if (section.value === 'type') {
    const row = store.deviceTypes.find(x => x.id === id)
    if (row) openEditType(row)
  } else {
    const row = store.deviceGroups.find(x => x.id === id)
    if (row) openEditGroup(row)
  }
}

function newItem() {
  resetForm()
}

function openEditModel(row: DeviceModelDef) {
  resetForm()
  editingId.value = row.id
  form.id = row.id
  form.device_type = row.device_type
  form.manufacturer = row.manufacturer || ''
  form.model = row.model || ''
  form.protocol = row.protocol
  form.point_table = row.point_table
  form.read_mode = row.read_mode || 'sum'
  const c = row.connection_defaults || {}
  form.port = Number(c.port ?? (row.protocol === 'iec104' ? 2404 : row.protocol === 'ads' ? 48898 : 502))
  form.timeout = Number(c.timeout ?? 3)
  form.unit_id = Number(c.unit_id ?? 1)
  form.mode = String(c.mode ?? 'tcp')
  form.word_order = String(c.word_order ?? 'little_endian')
  form.target_port = Number(c.target_port ?? 801)
  form.twincat_version = String(c.twincat_version ?? '2')
  form.reconnect_max_retries = Number(c.reconnect_max_retries ?? 5)
  form.reconnect_backoff_max = Number(c.reconnect_backoff_max ?? 30)
  form.common_addr = Number(c.common_addr ?? 1)
  form.k = Number(c.k ?? 12)
  form.w = Number(c.w ?? 8)
  form.t0 = Number(c.t0 ?? 30)
  form.t1 = Number(c.t1 ?? 15)
  form.t2 = Number(c.t2 ?? 10)
  form.t3 = Number(c.t3 ?? 20)
  form.max_reconnect_retries = Number(c.max_reconnect_retries ?? 5)
}

function openEditType(row: { id: string; name: string }) {
  resetForm()
  editingId.value = row.id
  form.id = row.id
  form.name = row.name
}

function openEditGroup(row: { id: string; device_type: string }) {
  resetForm()
  editingId.value = row.id
  form.id = row.id
  form.device_type = row.device_type
}

function onProtocolChange() {
  form.point_table = store.pointTables.find(t => t.protocol === form.protocol)?.id || ''
  if (form.protocol === 'ads') {
    form.port = 48898
    form.target_port = 801
  } else if (form.protocol === 'iec104') {
    form.port = 2404
  } else {
    form.port = 502
  }
}

function ensureId(id: string, exists: boolean, label: string) {
  if (!id.trim()) {
    ElMessage.error(label + ' ID is required')
    return false
  }
  if (!editingId.value && exists) {
    ElMessage.error(label + ' ID "' + id + '" already exists')
    return false
  }
  return true
}

function connectionDefaults(): Record<string, unknown> {
  if (form.protocol === 'ads') {
    return {
      port: form.port,
      timeout: form.timeout,
      target_port: form.target_port,
      twincat_version: form.twincat_version,
      reconnect_max_retries: form.reconnect_max_retries,
      reconnect_backoff_max: form.reconnect_backoff_max,
    }
  }
  if (form.protocol === 'modbus') {
    return {
      port: form.port,
      timeout: form.timeout,
      unit_id: form.unit_id,
      mode: form.mode,
      word_order: form.word_order,
      reconnect_max_retries: form.reconnect_max_retries,
      reconnect_backoff_max: form.reconnect_backoff_max,
    }
  }
  return {
    port: form.port,
    common_addr: form.common_addr,
    k: form.k,
    w: form.w,
    t0: form.t0,
    t1: form.t1,
    t2: form.t2,
    t3: form.t3,
    max_reconnect_retries: form.max_reconnect_retries,
  }
}

function saveModel() {
  const id = form.id.trim()
  if (!ensureId(id, store.deviceModels.some(x => x.id === id), 'Model')) return
  if (!form.device_type) {
    ElMessage.error('Device type is required')
    return
  }
  const table = store.pointTables.find(t => t.id === form.point_table)
  if (!table || table.protocol !== form.protocol) {
    ElMessage.error('Point table must exist and match model protocol')
    return
  }
  const payload: DeviceModelDef = {
    id,
    device_type: form.device_type,
    manufacturer: form.manufacturer.trim(),
    model: form.model.trim(),
    protocol: form.protocol,
    point_table: form.point_table,
    read_mode: form.protocol === 'ads' ? form.read_mode : '',
    properties: {},
    connection_defaults: connectionDefaults(),
  }
  if (editingId.value) {
    const target = store.deviceModels.find(x => x.id === editingId.value)
    if (target) Object.assign(target, payload, { id: target.id })
  } else {
    store.deviceModels.push(payload)
  }
  ElMessage.success(editingId.value ? 'Device model updated (mock)' : 'Device model created (mock)')
  resetForm()
}

function saveType() {
  const id = form.id.trim()
  if (!ensureId(id, store.deviceTypes.some(x => x.id === id), 'Type')) return
  if (editingId.value) {
    const target = store.deviceTypes.find(x => x.id === editingId.value)
    if (target) target.name = form.name.trim() || target.id
  } else {
    store.deviceTypes.push({ id, name: form.name.trim() || id })
  }
  ElMessage.success(editingId.value ? 'Device type updated (mock)' : 'Device type created (mock)')
  resetForm()
}

function saveGroup() {
  const id = form.id.trim()
  if (!ensureId(id, store.deviceGroups.some(x => x.id === id), 'Group')) return
  if (!form.device_type) {
    ElMessage.error('Device type is required')
    return
  }
  if (editingId.value) {
    const target = store.deviceGroups.find(x => x.id === editingId.value)
    if (target) target.device_type = form.device_type
  } else {
    store.deviceGroups.push({ id, device_type: form.device_type })
  }
  ElMessage.success(editingId.value ? 'Device group updated (mock)' : 'Device group created (mock)')
  resetForm()
}

function saveCurrent() {
  if (section.value === 'model') saveModel()
  else if (section.value === 'type') saveType()
  else saveGroup()
}

async function deleteModel(row: { id: string }) {
  const devices = store.devices.filter(d => d.model === row.id).length
  if (devices) {
    ElMessage.warning('Cannot delete: referenced by ' + devices + ' device(s)')
    return
  }
  await ElMessageBox.confirm('Delete device model "' + row.id + '"?', 'Delete Device Model', { type: 'warning' })
  store.deviceModels.splice(store.deviceModels.findIndex(x => x.id === row.id), 1)
  if (editingId.value === row.id) resetForm()
}

async function deleteType(row: { id: string }) {
  const models = store.deviceModels.filter(m => m.device_type === row.id).length
  const groups = store.deviceGroups.filter(g => g.device_type === row.id).length
  if (models || groups) {
    ElMessage.warning('Cannot delete: referenced by ' + models + ' model(s) and ' + groups + ' group(s)')
    return
  }
  await ElMessageBox.confirm('Delete device type "' + row.id + '"?', 'Delete Device Type', { type: 'warning' })
  store.deviceTypes.splice(store.deviceTypes.findIndex(x => x.id === row.id), 1)
  if (editingId.value === row.id) resetForm()
}

async function deleteGroup(row: { id: string }) {
  const devices = store.devices.filter(d => d.device_group === row.id).length
  const tasks = store.tasks.filter(t => t.device_group === row.id).length
  if (devices || tasks) {
    ElMessage.warning('Cannot delete: referenced by ' + devices + ' device(s) and ' + tasks + ' task(s)')
    return
  }
  await ElMessageBox.confirm('Delete device group "' + row.id + '"?', 'Delete Device Group', { type: 'warning' })
  store.deviceGroups.splice(store.deviceGroups.findIndex(x => x.id === row.id), 1)
  if (editingId.value === row.id) resetForm()
}
</script>

<template>
  <el-button @click="openManager">Manage</el-button>

  <el-dialog
    v-model="manageOpen"
    title="Manage Device Metadata"
    :width="isMobile ? '100%' : 'min(1040px, 94vw)'"
    :fullscreen="isMobile"
    class="metadata-manager-dialog"
  >
    <el-tabs v-model="section" class="metadata-tabs" @tab-change="onSectionChange">
      <el-tab-pane label="Device Models" name="model" />
      <el-tab-pane label="Device Types" name="type" />
      <el-tab-pane label="Device Groups" name="group" />
    </el-tabs>

    <el-container class="metadata-layout">
      <el-aside width="330px" class="metadata-list-aside">
        <el-menu :default-active="editingId" class="metadata-list-menu" @select="onItemSelect">
          <template v-if="section === 'model'">
            <el-menu-item v-for="row in modelRows" :key="row.id" :index="row.id">
              <div class="metadata-menu-row">
                <div><b>{{ row.model || row.id }}</b><small>{{ row.id }} · {{ row.type_name }}</small><small>{{ row.protocol.toUpperCase() }} · {{ row.point_table }} · {{ row.devices }} devices</small></div>
                <el-button link type="danger" @click.stop="deleteModel(row)">Delete</el-button>
              </div>
            </el-menu-item>
          </template>
          <template v-else-if="section === 'type'">
            <el-menu-item v-for="row in typeRows" :key="row.id" :index="row.id">
              <div class="metadata-menu-row">
                <div><b>{{ row.name }}</b><small>{{ row.id }}</small><small>{{ row.models }} models · {{ row.groups }} groups</small></div>
                <el-button link type="danger" @click.stop="deleteType(row)">Delete</el-button>
              </div>
            </el-menu-item>
          </template>
          <template v-else>
            <el-menu-item v-for="row in groupRows" :key="row.id" :index="row.id">
              <div class="metadata-menu-row">
                <div><b>{{ row.id }}</b><small>{{ row.type_name }}</small><small>{{ row.devices }} devices · {{ row.tasks }} tasks</small></div>
                <el-button link type="danger" @click.stop="deleteGroup(row)">Delete</el-button>
              </div>
            </el-menu-item>
          </template>
        </el-menu>
      </el-aside>

      <el-main class="metadata-editor-main">
        <div class="metadata-editor-title">
          <h3>{{ editingId ? 'Edit' : 'New' }} {{ section === 'model' ? 'Model' : section === 'type' ? 'Type' : 'Group' }}</h3>
          <p>{{ editingId ? editingId : '创建后 ID 不再修改' }}</p>
        </div>

        <el-form label-position="top">
          <template v-if="section === 'model'">
            <div class="metadata-form-grid">
              <el-form-item label="Model ID"><el-input v-model="form.id" :disabled="!!editingId" /></el-form-item>
              <el-form-item label="Hardware Model"><el-input v-model="form.model" /></el-form-item>
              <el-form-item label="Manufacturer"><el-input v-model="form.manufacturer" /></el-form-item>
              <el-form-item label="Device Type"><el-select v-model="form.device_type"><el-option v-for="t in store.deviceTypes" :key="t.id" :label="t.name + ' · ' + t.id" :value="t.id" /></el-select></el-form-item>
              <el-form-item label="Protocol"><el-select v-model="form.protocol" @change="onProtocolChange"><el-option v-for="p in PROTOCOLS" :key="p" :label="p.toUpperCase()" :value="p" /></el-select></el-form-item>
              <el-form-item label="Point Table"><el-select v-model="form.point_table"><el-option v-for="t in tablesOfProtocol" :key="t.id" :label="t.id" :value="t.id" /></el-select></el-form-item>
              <el-form-item v-if="form.protocol === 'ads'" label="Read Mode"><el-select v-model="form.read_mode"><el-option v-for="r in ADS_READ_MODES" :key="r" :label="r" :value="r" /></el-select></el-form-item>
            </div>
            <el-divider content-position="left">Connection Defaults</el-divider>
            <div class="metadata-form-grid">
              <el-form-item label="Port"><el-input-number v-model="form.port" :min="1" :max="65535" :controls="false" /></el-form-item>
              <template v-if="form.protocol === 'ads'">
                <el-form-item label="Target Port"><el-input-number v-model="form.target_port" :min="1" :max="65535" :controls="false" /></el-form-item>
                <el-form-item label="TwinCAT Version"><el-select v-model="form.twincat_version"><el-option label="TwinCAT 2" value="2" /><el-option label="TwinCAT 3" value="3" /></el-select></el-form-item>
                <el-form-item label="Timeout (s)"><el-input-number v-model="form.timeout" :min="0.1" :step="0.5" :controls="false" /></el-form-item>
                <el-form-item label="Reconnect Max Retries"><el-input-number v-model="form.reconnect_max_retries" :min="0" :controls="false" /></el-form-item>
                <el-form-item label="Reconnect Backoff Max (s)"><el-input-number v-model="form.reconnect_backoff_max" :min="0" :controls="false" /></el-form-item>
              </template>
              <template v-else-if="form.protocol === 'modbus'">
                <el-form-item label="Unit ID"><el-input-number v-model="form.unit_id" :min="0" :max="255" :controls="false" /></el-form-item>
                <el-form-item label="Mode"><el-select v-model="form.mode"><el-option label="TCP" value="tcp" /></el-select></el-form-item>
                <el-form-item label="Timeout (s)"><el-input-number v-model="form.timeout" :min="0.1" :step="0.5" :controls="false" /></el-form-item>
                <el-form-item label="Word Order"><el-select v-model="form.word_order"><el-option label="little_endian" value="little_endian" /><el-option label="big_endian" value="big_endian" /></el-select></el-form-item>
                <el-form-item label="Reconnect Max Retries"><el-input-number v-model="form.reconnect_max_retries" :min="0" :controls="false" /></el-form-item>
                <el-form-item label="Reconnect Backoff Max (s)"><el-input-number v-model="form.reconnect_backoff_max" :min="0" :controls="false" /></el-form-item>
              </template>
              <template v-else>
                <el-form-item label="Common Address"><el-input-number v-model="form.common_addr" :min="1" :max="65535" :controls="false" /></el-form-item>
                <el-form-item label="K Window"><el-input-number v-model="form.k" :min="1" :controls="false" /></el-form-item>
                <el-form-item label="W Window"><el-input-number v-model="form.w" :min="1" :controls="false" /></el-form-item>
                <el-form-item label="T0 (s)"><el-input-number v-model="form.t0" :min="0.1" :controls="false" /></el-form-item>
                <el-form-item label="T1 (s)"><el-input-number v-model="form.t1" :min="0.1" :controls="false" /></el-form-item>
                <el-form-item label="T2 (s)"><el-input-number v-model="form.t2" :min="0.1" :controls="false" /></el-form-item>
                <el-form-item label="T3 (s)"><el-input-number v-model="form.t3" :min="0.1" :controls="false" /></el-form-item>
                <el-form-item label="Max Reconnect Retries"><el-input-number v-model="form.max_reconnect_retries" :min="0" :controls="false" /></el-form-item>
              </template>
            </div>
          </template>
          <template v-else-if="section === 'type'">
            <el-form-item label="Type ID"><el-input v-model="form.id" :disabled="!!editingId" /></el-form-item>
            <el-form-item label="Name"><el-input v-model="form.name" /></el-form-item>
          </template>
          <template v-else>
            <el-form-item label="Group ID"><el-input v-model="form.id" :disabled="!!editingId" /></el-form-item>
            <el-form-item label="Device Type"><el-select v-model="form.device_type" style="width:100%"><el-option v-for="t in store.deviceTypes" :key="t.id" :label="t.name + ' · ' + t.id" :value="t.id" /></el-select></el-form-item>
          </template>
        </el-form>
        <div class="metadata-editor-actions"><el-button @click="resetForm">Clear</el-button><el-button type="primary" @click="saveCurrent">{{ editingId ? 'Update' : 'Create' }}</el-button></div>
      </el-main>
    </el-container>
  </el-dialog>
</template>

<style scoped>
.metadata-tabs{margin-top:-8px}.metadata-layout{min-height:520px}
.metadata-list-aside{border-right:1px solid var(--app-border-soft);padding-right:12px}
.metadata-list-menu{border-right:0!important;background:transparent}
.metadata-list-menu .el-menu-item{height:auto;min-height:70px;line-height:normal;padding:10px 8px!important;border-radius:7px;margin-bottom:2px}
.metadata-list-menu .el-menu-item.is-active{background:#f4f7fb;color:var(--app-text-primary)}
.metadata-menu-row{width:100%;display:flex;align-items:center;justify-content:space-between;gap:12px}
.metadata-menu-row>div{min-width:0}.metadata-menu-row b,.metadata-menu-row small{display:block}
.metadata-menu-row b{font-size:var(--app-font-body);font-weight:var(--app-font-weight-semibold);color:var(--app-text-primary)}
.metadata-menu-row small{margin-top:4px;color:var(--app-text-muted);font-size:var(--app-font-caption)}
.metadata-editor-main{padding:4px 8px 4px 24px!important}.metadata-editor-title{margin-bottom:16px}
.metadata-editor-title h3{margin:0;font-size:var(--app-font-section-title);font-weight:var(--app-font-weight-semibold)}
.metadata-editor-title p{margin:4px 0 0;color:var(--app-text-muted);font-size:var(--app-font-caption)}
.metadata-form-grid{display:grid;grid-template-columns:1fr 1fr;gap:0 14px}
.metadata-form-grid :deep(.el-select),.metadata-form-grid :deep(.el-input-number){width:100%}
.metadata-editor-actions{display:flex;justify-content:flex-end;gap:var(--app-space-2);margin-top:8px}
@media(max-width:900px){.metadata-layout{flex-direction:column}.metadata-list-aside{width:100%!important;border-right:0;border-bottom:1px solid var(--app-border-soft);padding:0 0 12px}.metadata-editor-main{padding:16px 0 0!important}.metadata-form-grid{grid-template-columns:1fr}}
</style>
