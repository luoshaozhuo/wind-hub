<script setup lang="ts">
import { computed, onBeforeUnmount, reactive, ref } from 'vue'

const props = withDefaults(defineProps<{ dropdownItem?: boolean }>(), {
  dropdownItem: false,
})
import { ElMessage, ElMessageBox } from 'element-plus'
import { devicesForTask, refreshTaskValidity, resetDeviceConnectionOverrides, store } from '../mock/data'
import { ADS_READ_MODES, PROTOCOLS } from '../mock/types'
import type { DeviceModelDef, Protocol } from '../mock/types'

type ManageSection = 'model' | 'type' | 'group'

const manageOpen = ref(false)
const section = ref<ManageSection>('model')
const editingId = ref('')
const metadataSnapshot = ref('')
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

const metadataDirty = computed(() => !!editingId.value && JSON.stringify(form) !== metadataSnapshot.value)
async function beforeMetadataClose(done:()=>void){
  if(!metadataDirty.value){done();return}
  try{
    await ElMessageBox.confirm('Discard unsaved metadata changes?','Unsaved Changes',{type:'warning',confirmButtonText:'Discard'})
    done()
  }catch{}
}
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

function onItemRowClick(row: { id: string }) {
  if (section.value === 'model') {
    const item = store.deviceModels.find(x => x.id === row.id)
    if (item) openEditModel(item)
  } else if (section.value === 'type') {
    const item = store.deviceTypes.find(x => x.id === row.id)
    if (item) openEditType(item)
  } else {
    const item = store.deviceGroups.find(x => x.id === row.id)
    if (item) openEditGroup(item)
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
  metadataSnapshot.value = JSON.stringify(form)
}

function openEditType(row: { id: string; name: string }) {
  resetForm()
  editingId.value = row.id
  form.id = row.id
  form.name = row.name
  metadataSnapshot.value = JSON.stringify(form)
}

function openEditGroup(row: { id: string; device_type: string }) {
  resetForm()
  editingId.value = row.id
  form.id = row.id
  form.device_type = row.device_type
  metadataSnapshot.value = JSON.stringify(form)
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

async function saveModel() {
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
    if (!target) return

    const devices = store.devices.filter(d => d.model === target.id)
    const deviceIds = new Set(devices.map(d => d.device_id))
    const affectedTasks = store.tasks.filter(t => devicesForTask(t).some(d => deviceIds.has(d.device_id)))
    const running = affectedTasks.filter(t => t.runtime === 'RUNNING')
    const protocolChanged = target.protocol !== payload.protocol
    const tableChanged = target.point_table !== payload.point_table
    const connectionChanged = JSON.stringify(target.connection_defaults) !== JSON.stringify(payload.connection_defaults)
    const runtimeImpact = protocolChanged || tableChanged || connectionChanged || target.read_mode !== payload.read_mode

    if (runtimeImpact && devices.length) {
      await ElMessageBox.confirm(
        '<b>Device Model Change Impact</b><br><br>' +
        devices.length + ' Device(s) affected.<br>' +
        affectedTasks.length + ' Task(s) affected; ' + running.length + ' currently running.<br>' +
        (protocolChanged || connectionChanged ? 'Affected protocol connections will be rebuilt.<br>' : '') +
        (tableChanged ? 'Point mappings will be replaced.<br>' : '') +
        '<br>Running tasks will be stopped while applying and restored if still valid.',
        'Apply Device Model Changes',
        { type: 'warning', confirmButtonText: 'Apply Changes', dangerouslyUseHTMLString: true },
      )
    }

    const runningIds = new Set(running.map(t => t.task_id))
    for (const t of affectedTasks) if (runningIds.has(t.task_id)) t.runtime = 'STOPPED'
    Object.assign(target, payload, { id: target.id })
    refreshTaskValidity()
    for (const t of affectedTasks) {
      if (runningIds.has(t.task_id) && t.valid !== false && t.enabled) t.runtime = 'RUNNING'
    }
  } else {
    store.deviceModels.push(payload)
  }
  const savedId = id
  const wasEditing = !!editingId.value
  ElMessage.success(wasEditing ? 'Device model updated (mock)' : 'Device model created (mock)')
  if (wasEditing) {
    const saved = store.deviceModels.find(x => x.id === savedId)
    if (saved) openEditModel(saved)
  } else resetForm()
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
  const savedId = id
  const wasEditing = !!editingId.value
  ElMessage.success(wasEditing ? 'Device type updated (mock)' : 'Device type created (mock)')
  if (wasEditing) {
    const saved = store.deviceTypes.find(x => x.id === savedId)
    if (saved) openEditType(saved)
  } else resetForm()
}

async function saveGroup() {
  const id = form.id.trim()
  if (!ensureId(id, store.deviceGroups.some(x => x.id === id), 'Group')) return
  if (!form.device_type) {
    ElMessage.error('Device type is required')
    return
  }
  if (editingId.value) {
    const target = store.deviceGroups.find(x => x.id === editingId.value)
    if (!target) return
    if (target.device_type !== form.device_type) {
      const devices = store.devices.filter(d => d.device_group === target.id)
      const incompatible = devices.filter(d =>
        store.deviceModels.find(m => m.id === d.model)?.device_type !== form.device_type,
      )
      if (incompatible.length) {
        ElMessage.error('Cannot change Device Type: ' + incompatible.length + ' existing device(s) use incompatible models')
        return
      }
      const tasks = store.tasks.filter(t => t.device_group === target.id)
      if (devices.length || tasks.length) {
        await ElMessageBox.confirm(
          devices.length + ' Device(s) and ' + tasks.length + ' group Task(s) reference this group. Apply the classification change?',
          'Device Group Change Impact',
          { type: 'warning', confirmButtonText: 'Apply Changes' },
        )
      }
      target.device_type = form.device_type
    }
  } else {
    store.deviceGroups.push({ id, device_type: form.device_type })
  }
  const savedId = id
  const wasEditing = !!editingId.value
  ElMessage.success(wasEditing ? 'Device group updated (mock)' : 'Device group created (mock)')
  if (wasEditing) {
    const saved = store.deviceGroups.find(x => x.id === savedId)
    if (saved) openEditGroup(saved)
  } else resetForm()
}

function saveCurrent() {
  if (section.value === 'model') saveModel()
  else if (section.value === 'type') saveType()
  else saveGroup()
}

async function resetModelDeviceOverrides() {
  if (!editingId.value || section.value !== 'model') return
  const model = store.deviceModels.find(m => m.id === editingId.value)
  if (!model) return

  const devices = store.devices.filter(d => d.model === model.id)
  const affected = devices.map(device => ({
    device,
    count: Object.keys(device.extensions || {}).filter(key => key !== 'target_net_id').length +
      (device.port !== undefined ? 1 : 0),
  })).filter(x => x.count > 0)

  if (!affected.length) {
    ElMessage.info('No Device connection overrides to reset')
    return
  }

  const ids = new Set(affected.map(x => x.device.device_id))
  const tasks = store.tasks.filter(t => devicesForTask(t).some(d => ids.has(d.device_id)))
  const running = tasks.filter(t => t.runtime === 'RUNNING')
  const overrideCount = affected.reduce((sum, x) => sum + x.count, 0)

  await ElMessageBox.confirm(
    '<b>Reset Device Overrides?</b><br><br>' +
    affected.length + ' Device(s) affected.<br>' +
    overrideCount + ' override(s) will be removed.<br>' +
    running.length + ' running Task(s) affected.<br><br>' +
    '<b>Preserved:</b> Device ID, Host / Remote IP, Target AMS Net ID and Device Group.',
    'Reset Device Overrides',
    { type: 'warning', confirmButtonText: 'Reset Overrides', dangerouslyUseHTMLString: true },
  )

  const runningIds = new Set(running.map(t => t.task_id))
  for (const t of running) t.runtime = 'STOPPED'
  for (const x of affected) resetDeviceConnectionOverrides(x.device)
  refreshTaskValidity()
  for (const t of tasks) {
    if (runningIds.has(t.task_id) && t.enabled && t.valid !== false) t.runtime = 'RUNNING'
  }
  ElMessage.success('Device connection overrides reset to Model defaults (mock)')
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
  <el-dropdown-item v-if="props.dropdownItem" @click="openManager">Manage Metadata</el-dropdown-item>
  <el-button v-else @click="openManager">Manage</el-button>

  <el-drawer
    v-model="manageOpen"
    title="Manage Device Metadata"
    direction="rtl"
    :size="isMobile ? '100%' : 'min(1080px, 86vw)'"
    append-to-body
    destroy-on-close
    class="metadata-manager-drawer"
    :before-close="beforeMetadataClose"
  >
    <el-tabs v-model="section" class="metadata-tabs" @tab-change="onSectionChange">
      <el-tab-pane label="Device Models" name="model" />
      <el-tab-pane label="Device Types" name="type" />
      <el-tab-pane label="Device Groups" name="group" />
    </el-tabs>

    <el-container class="metadata-layout">
      <el-aside width="360px" class="metadata-list-aside">
        <el-table
          v-if="section === 'model'"
          :data="modelRows"
          row-key="id"
          :show-header="false"
          highlight-current-row
          :current-row-key="editingId"
          class="metadata-object-table"
          @row-click="onItemRowClick"
        >
          <el-table-column min-width="260">
            <template #default="{ row }">
              <div class="metadata-object-info">
                <b>{{ row.model || row.id }}</b>
                <small>{{ row.id }} · {{ row.type_name }}</small>
                <small>{{ row.protocol.toUpperCase() }} · {{ row.point_table }} · {{ row.devices }} devices</small>
              </div>
            </template>
          </el-table-column>
          <el-table-column width="78" align="right">
            <template #default="{ row }"><el-button link type="danger" @click.stop="deleteModel(row)">Delete</el-button></template>
          </el-table-column>
        </el-table>

        <el-table
          v-else-if="section === 'type'"
          :data="typeRows"
          row-key="id"
          :show-header="false"
          highlight-current-row
          :current-row-key="editingId"
          class="metadata-object-table"
          @row-click="onItemRowClick"
        >
          <el-table-column min-width="260">
            <template #default="{ row }">
              <div class="metadata-object-info">
                <b>{{ row.name }}</b>
                <small>{{ row.id }}</small>
                <small>{{ row.models }} models · {{ row.groups }} groups</small>
              </div>
            </template>
          </el-table-column>
          <el-table-column width="78" align="right">
            <template #default="{ row }"><el-button link type="danger" @click.stop="deleteType(row)">Delete</el-button></template>
          </el-table-column>
        </el-table>

        <el-table
          v-else
          :data="groupRows"
          row-key="id"
          :show-header="false"
          highlight-current-row
          :current-row-key="editingId"
          class="metadata-object-table"
          @row-click="onItemRowClick"
        >
          <el-table-column min-width="260">
            <template #default="{ row }">
              <div class="metadata-object-info">
                <b>{{ row.id }}</b>
                <small>{{ row.type_name }}</small>
                <small>{{ row.devices }} devices · {{ row.tasks }} tasks</small>
              </div>
            </template>
          </el-table-column>
          <el-table-column width="78" align="right">
            <template #default="{ row }"><el-button link type="danger" @click.stop="deleteGroup(row)">Delete</el-button></template>
          </el-table-column>
        </el-table>
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
        <div class="metadata-editor-actions">
          <el-button v-if="section === 'model' && editingId" @click="resetModelDeviceOverrides">Reset Device Overrides</el-button>
          <el-button v-if="!editingId" @click="resetForm">Clear</el-button>
          <el-button type="primary" :disabled="!!editingId && !metadataDirty" @click="saveCurrent">{{ editingId ? 'Save' : 'Create' }}</el-button>
        </div>
      </el-main>
    </el-container>
  </el-drawer>
</template>

<style scoped>
.metadata-tabs{margin-top:-8px}.metadata-layout{min-height:520px}
.metadata-list-aside{border-right:1px solid var(--app-border-soft);padding-right:12px}
.metadata-object-table{width:100%;cursor:pointer}
.metadata-object-table :deep(.el-table__inner-wrapper::before){display:none}
.metadata-object-table :deep(.el-table__cell){padding:9px 0!important}
.metadata-object-table :deep(.el-table__row.current-row>td.el-table__cell){background:#f4f7fb}
.metadata-object-info{min-width:0;padding-left:4px}
.metadata-object-info b,.metadata-object-info small{display:block}
.metadata-object-info b{font-size:var(--app-font-body);font-weight:var(--app-font-weight-semibold);color:var(--app-text-primary)}
.metadata-object-info small{margin-top:4px;color:var(--app-text-muted);font-size:var(--app-font-caption);white-space:normal;line-height:var(--app-line-height-compact)}
.metadata-editor-main{padding:4px 8px 4px 24px!important}.metadata-editor-title{margin-bottom:16px}
.metadata-editor-title h3{margin:0;font-size:var(--app-font-section-title);font-weight:var(--app-font-weight-semibold)}
.metadata-editor-title p{margin:4px 0 0;color:var(--app-text-muted);font-size:var(--app-font-caption)}
.metadata-form-grid{display:grid;grid-template-columns:1fr 1fr;gap:0 14px}
.metadata-form-grid :deep(.el-select),.metadata-form-grid :deep(.el-input-number){width:100%}
.metadata-editor-actions{display:flex;justify-content:flex-end;gap:var(--app-space-2);margin-top:8px}
@media(max-width:900px){.metadata-layout{flex-direction:column}.metadata-list-aside{width:100%!important;border-right:0;border-bottom:1px solid var(--app-border-soft);padding:0 0 12px}.metadata-editor-main{padding:16px 0 0!important}.metadata-form-grid{grid-template-columns:1fr}}
</style>
