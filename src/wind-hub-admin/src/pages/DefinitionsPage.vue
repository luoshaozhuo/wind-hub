<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { store } from '../mock/data'
import { ADS_READ_MODES, PROTOCOLS } from '../mock/types'
import type { DeviceModelDef, Protocol } from '../mock/types'

type EditKind = 'model' | 'type' | 'group'

const dlg = ref<EditKind | ''>('')
const editingId = ref('')

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
  unit_id: 1,
  mode: 'tcp',
  timeout: 3,
  word_order: 'little_endian',
  target_port: 801,
  twincat_version: '2',
  reconnect_max_retries: 5,
  reconnect_backoff_max: 30,
})

const modelRows = computed(() =>
  store.deviceModels.map(m => ({
    ...m,
    type_name: store.deviceTypes.find(t => t.id === m.device_type)?.name || m.device_type,
    devices: store.devices.filter(d => d.model === m.id).length,
  })),
)

const typeRows = computed(() =>
  store.deviceTypes.map(t => ({
    ...t,
    models: store.deviceModels.filter(m => m.device_type === t.id).length,
    groups: store.deviceGroups.filter(g => g.device_type === t.id).length,
  })),
)

const groupRows = computed(() =>
  store.deviceGroups.map(g => ({
    ...g,
    type_name: store.deviceTypes.find(t => t.id === g.device_type)?.name || g.device_type,
    devices: store.devices.filter(d => d.device_group === g.id).length,
    tasks: store.tasks.filter(t => t.device_group === g.id).length,
  })),
)

const tablesOfProtocol = computed(() =>
  store.pointTables.filter(t => t.protocol === form.protocol),
)

function resetForm() {
  form.id = ''
  form.name = ''
  form.device_type = store.deviceTypes[0]?.id || ''
  form.manufacturer = ''
  form.model = ''
  form.protocol = 'modbus'
  form.point_table = ''
  form.read_mode = 'sum'
  form.port = 502
  form.unit_id = 1
  form.mode = 'tcp'
  form.timeout = 3
  form.word_order = 'little_endian'
  form.target_port = 801
  form.twincat_version = '2'
  form.reconnect_max_retries = 5
  form.reconnect_backoff_max = 30
  editingId.value = ''
}

function openCreate(kind: EditKind) {
  resetForm()
  dlg.value = kind
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
  form.unit_id = Number(c.unit_id ?? 1)
  form.mode = String(c.mode ?? 'tcp')
  form.timeout = Number(c.timeout ?? 3)
  form.word_order = String(c.word_order ?? 'little_endian')
  form.target_port = Number(c.target_port ?? 801)
  form.twincat_version = String(c.twincat_version ?? '2')
  form.reconnect_max_retries = Number(c.reconnect_max_retries ?? 5)
  form.reconnect_backoff_max = Number(c.reconnect_backoff_max ?? 30)
  dlg.value = 'model'
}

function openEditType(row: { id: string; name: string }) {
  resetForm()
  editingId.value = row.id
  form.id = row.id
  form.name = row.name
  dlg.value = 'type'
}

function openEditGroup(row: { id: string; device_type: string }) {
  resetForm()
  editingId.value = row.id
  form.id = row.id
  form.device_type = row.device_type
  dlg.value = 'group'
}

function onProtocolChange() {
  form.point_table = ''
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

function saveType() {
  const id = form.id.trim()
  if (!ensureId(id, store.deviceTypes.some(x => x.id === id), 'Type')) return

  if (editingId.value) {
    const target = store.deviceTypes.find(x => x.id === editingId.value)
    if (target) target.name = form.name.trim() || target.id
  } else {
    store.deviceTypes.push({ id, name: form.name.trim() || id })
  }

  dlg.value = ''
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

  dlg.value = ''
  ElMessage.success(editingId.value ? 'Device group updated (mock)' : 'Device group created (mock)')
}

function connectionDefaults(): Record<string, unknown> {
  const common = {
    port: form.port,
    timeout: form.timeout,
    reconnect_max_retries: form.reconnect_max_retries,
    reconnect_backoff_max: form.reconnect_backoff_max,
  }

  if (form.protocol === 'ads') {
    return {
      ...common,
      target_port: form.target_port,
      twincat_version: form.twincat_version,
    }
  }

  if (form.protocol === 'modbus') {
    return {
      ...common,
      unit_id: form.unit_id,
      mode: form.mode,
      word_order: form.word_order,
    }
  }

  return common
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

  dlg.value = ''
  ElMessage.success(editingId.value ? 'Device model updated (mock)' : 'Device model created (mock)')
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

async function deleteModel(row: { id: string }) {
  const devices = store.devices.filter(d => d.model === row.id).length
  if (devices) {
    ElMessage.warning(`Cannot delete: referenced by ${devices} device(s)`)
    return
  }
  await ElMessageBox.confirm(`Delete device model "${row.id}"?`, 'Delete Device Model', { type: 'warning' })
  store.deviceModels.splice(store.deviceModels.findIndex(x => x.id === row.id), 1)
}
</script>

<template>
  <div class="metadata-page">
    <div class="head">
      <div>
        <h1>Device Metadata</h1>
        <p>设备型号、类型与分组定义</p>
      </div>
    </div>

    <el-card shadow="never" class="metadata-card">
      <div class="section-head">
        <div>
          <h3>Device Models</h3>
          <p>定义设备型号、协议、点表及默认通信参数</p>
        </div>
        <el-button type="primary" @click="openCreate('model')">+ Add Model</el-button>
      </div>

      <el-table :data="modelRows">
        <el-table-column label="Model" min-width="210">
          <template #default="{ row }">
            <div class="primary">{{ row.model || row.id }}</div>
            <div class="secondary">{{ row.id }}</div>
          </template>
        </el-table-column>
        <el-table-column label="Device Type" min-width="170">
          <template #default="{ row }">{{ row.type_name }}</template>
        </el-table-column>
        <el-table-column label="Protocol" width="110">
          <template #default="{ row }"><el-tag effect="plain">{{ row.protocol.toUpperCase() }}</el-tag></template>
        </el-table-column>
        <el-table-column prop="point_table" label="Point Table" min-width="180" />
        <el-table-column prop="manufacturer" label="Manufacturer" min-width="140">
          <template #default="{ row }">{{ row.manufacturer || '—' }}</template>
        </el-table-column>
        <el-table-column label="Devices" width="90">
          <template #default="{ row }">{{ row.devices }}</template>
        </el-table-column>
        <el-table-column label="Actions" width="150" align="right">
          <template #default="{ row }">
            <el-button link type="primary" @click="openEditModel(row)">Edit</el-button>
            <el-button link type="danger" @click="deleteModel(row)">Delete</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <div class="metadata-bottom">
      <el-card shadow="never" class="metadata-card">
        <div class="section-head compact">
          <div>
            <h3>Device Types</h3>
            <p>设备业务分类</p>
          </div>
          <el-button @click="openCreate('type')">+ Add Type</el-button>
        </div>

        <el-table :data="typeRows">
          <el-table-column prop="id" label="Type ID" min-width="130" />
          <el-table-column prop="name" label="Name" min-width="150" />
          <el-table-column label="References" min-width="140">
            <template #default="{ row }">{{ row.models }} models · {{ row.groups }} groups</template>
          </el-table-column>
          <el-table-column label="" width="120" align="right">
            <template #default="{ row }">
              <el-button link type="primary" @click="openEditType(row)">Edit</el-button>
              <el-button link type="danger" @click="deleteType(row)">Delete</el-button>
            </template>
          </el-table-column>
        </el-table>
      </el-card>

      <el-card shadow="never" class="metadata-card">
        <div class="section-head compact">
          <div>
            <h3>Device Groups</h3>
            <p>设备实例分组</p>
          </div>
          <el-button @click="openCreate('group')">+ Add Group</el-button>
        </div>

        <el-table :data="groupRows">
          <el-table-column prop="id" label="Group ID" min-width="150" />
          <el-table-column label="Device Type" min-width="150">
            <template #default="{ row }">{{ row.type_name }}</template>
          </el-table-column>
          <el-table-column label="References" min-width="140">
            <template #default="{ row }">{{ row.devices }} devices · {{ row.tasks }} tasks</template>
          </el-table-column>
          <el-table-column label="" width="120" align="right">
            <template #default="{ row }">
              <el-button link type="primary" @click="openEditGroup(row)">Edit</el-button>
              <el-button link type="danger" @click="deleteGroup(row)">Delete</el-button>
            </template>
          </el-table-column>
        </el-table>
      </el-card>
    </div>

    <el-dialog
      :model-value="dlg === 'type'"
      :title="editingId ? 'Edit Device Type' : 'Add Device Type'"
      width="460"
      @close="dlg = ''"
    >
      <el-form label-position="top">
        <el-form-item label="Type ID">
          <el-input v-model="form.id" :disabled="!!editingId" />
        </el-form-item>
        <el-form-item label="Name">
          <el-input v-model="form.name" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dlg = ''">Cancel</el-button>
        <el-button type="primary" @click="saveType">{{ editingId ? 'Update' : 'Create' }}</el-button>
      </template>
    </el-dialog>

    <el-dialog
      :model-value="dlg === 'group'"
      :title="editingId ? 'Edit Device Group' : 'Add Device Group'"
      width="460"
      @close="dlg = ''"
    >
      <el-form label-position="top">
        <el-form-item label="Group ID">
          <el-input v-model="form.id" :disabled="!!editingId" />
        </el-form-item>
        <el-form-item label="Device Type">
          <el-select v-model="form.device_type" style="width: 100%">
            <el-option
              v-for="t in store.deviceTypes"
              :key="t.id"
              :label="`${t.name} · ${t.id}`"
              :value="t.id"
            />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dlg = ''">Cancel</el-button>
        <el-button type="primary" @click="saveGroup">{{ editingId ? 'Update' : 'Create' }}</el-button>
      </template>
    </el-dialog>

    <el-dialog
      :model-value="dlg === 'model'"
      :title="editingId ? 'Edit Device Model' : 'Add Device Model'"
      width="720"
      @close="dlg = ''"
    >
      <el-form label-position="top">
        <div class="form-grid">
          <el-form-item label="Model ID">
            <el-input v-model="form.id" :disabled="!!editingId" />
          </el-form-item>

          <el-form-item label="Hardware Model">
            <el-input v-model="form.model" />
          </el-form-item>

          <el-form-item label="Manufacturer">
            <el-input v-model="form.manufacturer" />
          </el-form-item>

          <el-form-item label="Device Type">
            <el-select v-model="form.device_type">
              <el-option
                v-for="t in store.deviceTypes"
                :key="t.id"
                :label="`${t.name} · ${t.id}`"
                :value="t.id"
              />
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

        <div class="subheading">Connection Defaults</div>

        <div class="form-grid">
          <el-form-item label="Port">
            <el-input-number v-model="form.port" :min="1" :max="65535" :controls="false" />
          </el-form-item>

          <el-form-item label="Timeout (s)">
            <el-input-number v-model="form.timeout" :min="0.1" :step="0.5" :controls="false" />
          </el-form-item>

          <template v-if="form.protocol === 'modbus'">
            <el-form-item label="Unit ID">
              <el-input-number v-model="form.unit_id" :min="0" :max="255" :controls="false" />
            </el-form-item>
            <el-form-item label="Mode">
              <el-select v-model="form.mode">
                <el-option label="TCP" value="tcp" />
              </el-select>
            </el-form-item>
            <el-form-item label="Word Order">
              <el-select v-model="form.word_order">
                <el-option label="little_endian" value="little_endian" />
                <el-option label="big_endian" value="big_endian" />
              </el-select>
            </el-form-item>
          </template>

          <template v-if="form.protocol === 'ads'">
            <el-form-item label="Target Port">
              <el-input-number v-model="form.target_port" :min="1" :controls="false" />
            </el-form-item>
            <el-form-item label="TwinCAT Version">
              <el-select v-model="form.twincat_version">
                <el-option label="TwinCAT 2" value="2" />
                <el-option label="TwinCAT 3" value="3" />
              </el-select>
            </el-form-item>
          </template>

          <el-form-item label="Reconnect Max Retries">
            <el-input-number v-model="form.reconnect_max_retries" :min="0" :controls="false" />
          </el-form-item>

          <el-form-item label="Reconnect Backoff Max (s)">
            <el-input-number v-model="form.reconnect_backoff_max" :min="0" :controls="false" />
          </el-form-item>
        </div>
      </el-form>

      <template #footer>
        <el-button @click="dlg = ''">Cancel</el-button>
        <el-button type="primary" @click="saveModel">{{ editingId ? 'Update' : 'Create' }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.metadata-card {
  border-radius: 10px;
}
.metadata-bottom {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
  margin-top: 16px;
}
.section-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 14px;
}
.section-head.compact {
  margin-bottom: 10px;
}
.section-head h3 {
  margin: 0;
  font-size: 15px;
}
.section-head p {
  margin: 4px 0 0;
  color: #8a94a3;
  font-size: 12px;
}
.primary {
  font-weight: 600;
  color: #263244;
}
.secondary {
  margin-top: 2px;
  color: #8d98a8;
  font-size: 11px;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
.form-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 0 14px;
}
.form-grid :deep(.el-select),
.form-grid :deep(.el-input-number) {
  width: 100%;
}
.subheading {
  margin: 4px 0 14px;
  padding-top: 16px;
  border-top: 1px solid #edf0f4;
  color: #526071;
  font-size: 12px;
  font-weight: 700;
}
@media (max-width: 1050px) {
  .metadata-bottom {
    grid-template-columns: 1fr;
  }
}
</style>
