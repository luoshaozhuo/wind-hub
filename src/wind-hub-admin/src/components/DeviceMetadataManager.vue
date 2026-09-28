<script setup lang="ts">
import { computed, onBeforeUnmount, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { store } from '../mock/data'
import { ADS_READ_MODES, PROTOCOLS } from '../mock/types'
import type { DeviceModelDef, Protocol } from '../mock/types'

type ManageSection = 'model' | 'type' | 'group'

const active = ref<ManageSection | ''>('')
const editingId = ref('')
const dialogKind = ref<'type' | 'group' | ''>('')
const isMobile = ref(window.innerWidth < 768)
const isTablet = ref(window.innerWidth >= 768 && window.innerWidth < 1200)

function onResize() {
  isMobile.value = window.innerWidth < 768
  isTablet.value = window.innerWidth >= 768 && window.innerWidth < 1200
}
window.addEventListener('resize', onResize)
onBeforeUnmount(() => window.removeEventListener('resize', onResize))

const drawerSize = computed(() => isMobile.value ? '100%' : isTablet.value ? '72%' : '620px')

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

function openSection(section: ManageSection) {
  active.value = section
  editingId.value = ''
}

function openCreate(kind: ManageSection) {
  resetForm()
  if (kind === 'model') active.value = 'model'
  else dialogKind.value = kind
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
  active.value = 'model'
}

function openEditType(row: { id: string; name: string }) {
  resetForm()
  editingId.value = row.id
  form.id = row.id
  form.name = row.name
  dialogKind.value = 'type'
}

function openEditGroup(row: { id: string; device_type: string }) {
  resetForm()
  editingId.value = row.id
  form.id = row.id
  form.device_type = row.device_type
  dialogKind.value = 'group'
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
    ElMessage.error(`${label} ID is required`)
    return false
  }
  if (!editingId.value && exists) {
    ElMessage.error(`${label} ID "${id}" already exists`)
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
  if (!table) {
    ElMessage.error('Point table is required')
    return
  }
  if (table.protocol !== form.protocol) {
    ElMessage.error('Point table protocol must match model protocol')
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
  editingId.value = ''
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
  dialogKind.value = ''
  ElMessage.success(editingId.value ? 'Device type updated (mock)' : 'Device type created (mock)')
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
  dialogKind.value = ''
  ElMessage.success(editingId.value ? 'Device group updated (mock)' : 'Device group created (mock)')
}

async function deleteModel(row: { id: string }) {
  const devices = store.devices.filter(d => d.model === row.id).length
  if (devices) {
    ElMessage.warning(`Cannot delete: referenced by ${devices} device(s)`)
    return
  }
  await ElMessageBox.confirm(`Delete device model "${row.id}"?`, 'Delete Device Model', { type: 'warning' })
  store.deviceModels.splice(store.deviceModels.findIndex(x => x.id === row.id), 1)
}

async function deleteType(row: { id: string }) {
  const models = store.deviceModels.filter(m => m.device_type === row.id).length
  const groups = store.deviceGroups.filter(g => g.device_type === row.id).length
  if (models || groups) {
    ElMessage.warning(`Cannot delete: referenced by ${models} model(s) and ${groups} group(s)`)
    return
  }
  await ElMessageBox.confirm(`Delete device type "${row.id}"?`, 'Delete Device Type', { type: 'warning' })
  store.deviceTypes.splice(store.deviceTypes.findIndex(x => x.id === row.id), 1)
}

async function deleteGroup(row: { id: string }) {
  const devices = store.devices.filter(d => d.device_group === row.id).length
  const tasks = store.tasks.filter(t => t.device_group === row.id).length
  if (devices || tasks) {
    ElMessage.warning(`Cannot delete: referenced by ${devices} device(s) and ${tasks} task(s)`)
    return
  }
  await ElMessageBox.confirm(`Delete device group "${row.id}"?`, 'Delete Device Group', { type: 'warning' })
  store.deviceGroups.splice(store.deviceGroups.findIndex(x => x.id === row.id), 1)
}
</script>

<template>
  <el-popover placement="bottom-end" :width="360" trigger="click" popper-class="metadata-command-popover">
    <template #reference>
      <el-button>Manage</el-button>
    </template>
    <div class="manage-command">
      <div class="manage-command-head">
        <b>Manage Device Metadata</b>
        <span>低频定义集中管理</span>
      </div>
      <button @click="openSection('model')">
        <span><b>Device Models</b><small>型号、协议、点表与默认连接参数</small></span>
        <em>{{ store.deviceModels.length }}</em><i>›</i>
      </button>
      <button @click="openSection('type')">
        <span><b>Device Types</b><small>设备业务分类</small></span>
        <em>{{ store.deviceTypes.length }}</em><i>›</i>
      </button>
      <button @click="openSection('group')">
        <span><b>Device Groups</b><small>设备实例分组</small></span>
        <em>{{ store.deviceGroups.length }}</em><i>›</i>
      </button>
    </div>
  </el-popover>

  <el-drawer
    :model-value="active === 'model'"
    title="Device Models"
    :size="drawerSize"
    :with-header="false"
    :append-to-body="true"
    @close="active = ''"
  >
    <div class="metadata-drawer">
      <div class="metadata-drawer-head">
        <div>
          <h2>Device Models</h2>
          <p>型号、协议、点表与默认连接参数</p>
        </div>
        <el-button type="primary" @click="openCreate('model')">+ Add Model</el-button>
      </div>

      <div v-for="row in modelRows" :key="row.id" class="metadata-list-row">
        <div class="metadata-list-main">
          <b>{{ row.model || row.id }}</b>
          <span>{{ row.id }} · {{ row.type_name }}</span>
          <small>{{ row.protocol.toUpperCase() }} · {{ row.point_table }} · {{ row.devices }} devices</small>
        </div>
        <div class="metadata-list-actions">
          <el-button link type="primary" @click="openEditModel(row)">Edit</el-button>
          <el-button link type="danger" @click="deleteModel(row)">Delete</el-button>
        </div>
      </div>

      <div class="model-editor">
        <div class="model-editor-head">
          <b>{{ editingId ? 'Edit Model' : 'New Model' }}</b>
          <span>结构化协议参数，不使用 JSON 文本框</span>
        </div>
        <el-form label-position="top">
          <div class="metadata-form-grid">
            <el-form-item label="Model ID"><el-input v-model="form.id" :disabled="!!editingId" /></el-form-item>
            <el-form-item label="Hardware Model"><el-input v-model="form.model" /></el-form-item>
            <el-form-item label="Manufacturer"><el-input v-model="form.manufacturer" /></el-form-item>
            <el-form-item label="Device Type">
              <el-select v-model="form.device_type">
                <el-option v-for="t in store.deviceTypes" :key="t.id" :label="`${t.name} · ${t.id}`" :value="t.id" />
              </el-select>
            </el-form-item>
            <el-form-item label="Protocol">
              <el-select v-model="form.protocol" @change="onProtocolChange">
                <el-option v-for="p in PROTOCOLS" :key="p" :label="p.toUpperCase()" :value="p" />
              </el-select>
            </el-form-item>
            <el-form-item label="Point Table">
              <el-select v-model="form.point_table">
                <el-option v-for="t in tablesOfProtocol" :key="t.id" :label="t.id" :value="t.id" />
              </el-select>
            </el-form-item>
            <el-form-item v-if="form.protocol === 'ads'" label="Read Mode">
              <el-select v-model="form.read_mode">
                <el-option v-for="r in ADS_READ_MODES" :key="r" :label="r" :value="r" />
              </el-select>
            </el-form-item>
          </div>

          <div class="model-subhead">Connection Defaults</div>
          <div class="metadata-form-grid">
            <el-form-item label="Port"><el-input-number v-model="form.port" :min="1" :max="65535" :controls="false" /></el-form-item>

            <template v-if="form.protocol === 'ads'">
              <el-form-item label="Target Port"><el-input-number v-model="form.target_port" :min="1" :max="65535" :controls="false" /></el-form-item>
              <el-form-item label="TwinCAT Version">
                <el-select v-model="form.twincat_version"><el-option label="TwinCAT 2" value="2" /><el-option label="TwinCAT 3" value="3" /></el-select>
              </el-form-item>
              <el-form-item label="Timeout (s)"><el-input-number v-model="form.timeout" :min="0.1" :step="0.5" :controls="false" /></el-form-item>
              <el-form-item label="Reconnect Max Retries"><el-input-number v-model="form.reconnect_max_retries" :min="0" :controls="false" /></el-form-item>
              <el-form-item label="Reconnect Backoff Max (s)"><el-input-number v-model="form.reconnect_backoff_max" :min="0" :controls="false" /></el-form-item>
            </template>

            <template v-else-if="form.protocol === 'modbus'">
              <el-form-item label="Unit ID"><el-input-number v-model="form.unit_id" :min="0" :max="255" :controls="false" /></el-form-item>
              <el-form-item label="Mode"><el-select v-model="form.mode"><el-option label="TCP" value="tcp" /></el-select></el-form-item>
              <el-form-item label="Timeout (s)"><el-input-number v-model="form.timeout" :min="0.1" :step="0.5" :controls="false" /></el-form-item>
              <el-form-item label="Word Order">
                <el-select v-model="form.word_order"><el-option label="little_endian" value="little_endian" /><el-option label="big_endian" value="big_endian" /></el-select>
              </el-form-item>
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
        </el-form>
        <div class="metadata-editor-actions">
          <el-button @click="resetForm">Clear</el-button>
          <el-button type="primary" @click="saveModel">{{ editingId ? 'Update Model' : 'Create Model' }}</el-button>
        </div>
      </div>
    </div>
  </el-drawer>

  <el-dialog
    :model-value="active === 'type'"
    title="Device Types"
    :width="isMobile ? '96%' : '620px'"
    :fullscreen="isMobile"
    @close="active = ''"
  >
    <div class="metadata-dialog-head"><span>设备业务分类</span><el-button type="primary" @click="openCreate('type')">+ Add Type</el-button></div>
    <div v-for="row in typeRows" :key="row.id" class="metadata-list-row">
      <div class="metadata-list-main"><b>{{ row.name }}</b><span>{{ row.id }}</span><small>{{ row.models }} models · {{ row.groups }} groups</small></div>
      <div class="metadata-list-actions"><el-button link type="primary" @click="openEditType(row)">Edit</el-button><el-button link type="danger" @click="deleteType(row)">Delete</el-button></div>
    </div>
  </el-dialog>

  <el-dialog
    :model-value="active === 'group'"
    title="Device Groups"
    :width="isMobile ? '96%' : '620px'"
    :fullscreen="isMobile"
    @close="active = ''"
  >
    <div class="metadata-dialog-head"><span>设备实例分组</span><el-button type="primary" @click="openCreate('group')">+ Add Group</el-button></div>
    <div v-for="row in groupRows" :key="row.id" class="metadata-list-row">
      <div class="metadata-list-main"><b>{{ row.id }}</b><span>{{ row.type_name }}</span><small>{{ row.devices }} devices · {{ row.tasks }} tasks</small></div>
      <div class="metadata-list-actions"><el-button link type="primary" @click="openEditGroup(row)">Edit</el-button><el-button link type="danger" @click="deleteGroup(row)">Delete</el-button></div>
    </div>
  </el-dialog>

  <el-dialog
    :model-value="dialogKind === 'type'"
    :title="editingId ? 'Edit Device Type' : 'Add Device Type'"
    :width="isMobile ? '96%' : '460px'"
    :fullscreen="isMobile"
    append-to-body
    @close="dialogKind = ''"
  >
    <el-form label-position="top">
      <el-form-item label="Type ID"><el-input v-model="form.id" :disabled="!!editingId" /></el-form-item>
      <el-form-item label="Name"><el-input v-model="form.name" /></el-form-item>
    </el-form>
    <template #footer><el-button @click="dialogKind = ''">Cancel</el-button><el-button type="primary" @click="saveType">{{ editingId ? 'Update' : 'Create' }}</el-button></template>
  </el-dialog>

  <el-dialog
    :model-value="dialogKind === 'group'"
    :title="editingId ? 'Edit Device Group' : 'Add Device Group'"
    :width="isMobile ? '96%' : '460px'"
    :fullscreen="isMobile"
    append-to-body
    @close="dialogKind = ''"
  >
    <el-form label-position="top">
      <el-form-item label="Group ID"><el-input v-model="form.id" :disabled="!!editingId" /></el-form-item>
      <el-form-item label="Device Type">
        <el-select v-model="form.device_type" style="width:100%">
          <el-option v-for="t in store.deviceTypes" :key="t.id" :label="`${t.name} · ${t.id}`" :value="t.id" />
        </el-select>
      </el-form-item>
    </el-form>
    <template #footer><el-button @click="dialogKind = ''">Cancel</el-button><el-button type="primary" @click="saveGroup">{{ editingId ? 'Update' : 'Create' }}</el-button></template>
  </el-dialog>
</template>

<style scoped>
.manage-command{padding:4px}
.manage-command-head{padding:6px 8px 10px}
.manage-command-head b,.manage-command-head span{display:block}
.manage-command-head b{font-size:13px;color:#344054}
.manage-command-head span{margin-top:3px;color:#98a2b3;font-size:11px}
.manage-command>button{width:100%;display:grid;grid-template-columns:1fr auto auto;align-items:center;gap:10px;border:0;background:transparent;text-align:left;padding:11px 9px;border-radius:8px;cursor:pointer}
.manage-command>button:hover{background:#f6f8fa}
.manage-command>button span b,.manage-command>button span small{display:block}
.manage-command>button span b{font-size:12px;color:#344054;font-weight:600}
.manage-command>button span small{margin-top:3px;color:#98a2b3;font-size:10px}
.manage-command em{font-style:normal;color:#98a2b3;font-size:11px}
.manage-command i{font-style:normal;color:#a8b0bc;font-size:18px}
.metadata-drawer{padding:0 4px 20px}
.metadata-drawer-head,.metadata-dialog-head,.metadata-editor-actions{display:flex;align-items:center;justify-content:space-between;gap:12px}
.metadata-drawer-head{padding:2px 0 16px}
.metadata-drawer-head h2{margin:0;font-size:20px}.metadata-drawer-head p{margin:4px 0 0;color:#98a2b3;font-size:11px}
.metadata-dialog-head{margin-bottom:12px}.metadata-dialog-head span{color:#98a2b3;font-size:11px}
.metadata-list-row{display:flex;align-items:center;justify-content:space-between;gap:14px;padding:12px 2px;border-bottom:1px solid #eef0f3}
.metadata-list-main{min-width:0}.metadata-list-main b,.metadata-list-main span,.metadata-list-main small{display:block}
.metadata-list-main b{font-size:12px;color:#344054}.metadata-list-main span{margin-top:3px;color:#667085;font-size:11px}.metadata-list-main small{margin-top:4px;color:#98a2b3;font-size:10px}
.metadata-list-actions{display:flex;flex:0 0 auto}
.model-editor{margin-top:20px;padding-top:18px;border-top:1px solid #e9edf2}
.model-editor-head{margin-bottom:14px}.model-editor-head b,.model-editor-head span{display:block}.model-editor-head b{font-size:13px;color:#344054}.model-editor-head span{margin-top:3px;color:#98a2b3;font-size:10px}
.metadata-form-grid{display:grid;grid-template-columns:1fr 1fr;gap:0 14px}
.metadata-form-grid :deep(.el-select),.metadata-form-grid :deep(.el-input-number){width:100%}
.model-subhead{margin:6px 0 12px;padding-top:14px;border-top:1px solid #eef0f3;color:#667085;font-size:11px;font-weight:600}
.metadata-editor-actions{justify-content:flex-end;margin-top:8px}
@media(max-width:767px){
  .metadata-form-grid{grid-template-columns:1fr}
  .metadata-drawer-head{align-items:flex-start;flex-direction:column}
  .metadata-list-row{align-items:flex-start}
}
</style>
