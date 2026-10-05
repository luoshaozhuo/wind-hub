<script setup lang="ts">
import { computed, reactive, ref } from 'vue'

const props = withDefaults(defineProps<{ dropdownItem?: boolean }>(), {
  dropdownItem: false,
})
import { ElMessage, ElMessageBox } from 'element-plus'
import ProtocolConnectionFields from './devices/ProtocolConnectionFields.vue'
import {
  connectionDefaultsFromForm,
  defaultConnectionForm,
  formFromModelDefaults,
  resetFormForProtocol,
} from '../domain/deviceConnection'
import { resetDeviceConnectionOverrides } from '../domain/devices'
import { devicesForTask, refreshTaskValidity } from '../domain/tasks'
import { useConfigStore } from '../stores/config'
import { ADS_READ_MODES, PROTOCOLS } from '../domain/types'
import type { DeviceModelDef, Protocol } from '../domain/types'
import { useViewport } from '../composables/useViewport'

const configStore = useConfigStore()

type ManageSection = 'model' | 'type' | 'group'

const manageOpen = ref(false)
const section = ref<ManageSection>('model')
const editingId = ref('')
const metadataSnapshot = ref('')
const { isMobile } = useViewport()

const form = reactive({
  id: '',
  name: '',
  device_type: '',
  manufacturer: '',
  model: '',
  protocol: 'modbus' as Protocol,
  point_table: '',
  read_mode: 'sum',
  connection: defaultConnectionForm('modbus'),
})

const metadataDirty = computed(
  () => !!editingId.value && JSON.stringify(form) !== metadataSnapshot.value,
)
async function beforeMetadataClose(done: () => void) {
  if (!metadataDirty.value) {
    done()
    return
  }
  try {
    await ElMessageBox.confirm('Discard unsaved metadata changes?', 'Unsaved Changes', {
      type: 'warning',
      confirmButtonText: 'Discard',
    })
    done()
  } catch {}
}
const tablesOfProtocol = computed(() =>
  configStore.pointTables.filter((t) => t.protocol === form.protocol),
)

const modelRows = computed(() =>
  configStore.deviceModels.map((m) => ({
    ...m,
    type_name: configStore.deviceTypes.find((t) => t.id === m.device_type)?.name || m.device_type,
    devices: configStore.devices.filter((d) => d.model === m.id).length,
  })),
)

const typeRows = computed(() =>
  configStore.deviceTypes.map((t) => ({
    ...t,
    models: configStore.deviceModels.filter((m) => m.device_type === t.id).length,
    groups: configStore.deviceGroups.filter((g) => g.device_type === t.id).length,
  })),
)

const groupRows = computed(() =>
  configStore.deviceGroups.map((g) => ({
    ...g,
    type_name: configStore.deviceTypes.find((t) => t.id === g.device_type)?.name || g.device_type,
    devices: configStore.devices.filter((d) => d.device_group === g.id).length,
    tasks: configStore.tasks.filter((t) => t.device_group === g.id).length,
  })),
)

function resetForm() {
  editingId.value = ''
  form.id = ''
  form.name = ''
  form.device_type = configStore.deviceTypes[0]?.id || ''
  form.manufacturer = ''
  form.model = ''
  form.protocol = 'modbus'
  form.point_table = configStore.pointTables.find((t) => t.protocol === 'modbus')?.id || ''
  form.read_mode = 'sum'
  form.connection = defaultConnectionForm('modbus')
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
    const item = configStore.deviceModels.find((x) => x.id === row.id)
    if (item) openEditModel(item)
  } else if (section.value === 'type') {
    const item = configStore.deviceTypes.find((x) => x.id === row.id)
    if (item) openEditType(item)
  } else {
    const item = configStore.deviceGroups.find((x) => x.id === row.id)
    if (item) openEditGroup(item)
  }
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
  form.connection = formFromModelDefaults(row)
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
  form.point_table = configStore.pointTables.find((t) => t.protocol === form.protocol)?.id || ''
  resetFormForProtocol(form.connection, form.protocol)
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
  return connectionDefaultsFromForm(form.connection, form.protocol)
}

async function saveModel() {
  const id = form.id.trim()
  if (
    !ensureId(
      id,
      configStore.deviceModels.some((x) => x.id === id),
      'Model',
    )
  )
    return
  if (!form.device_type) {
    ElMessage.error('Device type is required')
    return
  }
  const table = configStore.pointTables.find((t) => t.id === form.point_table)
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
    const target = configStore.deviceModels.find((x) => x.id === editingId.value)
    if (!target) return

    const devices = configStore.devices.filter((d) => d.model === target.id)
    const deviceIds = new Set(devices.map((d) => d.device_id))
    const affectedTasks = configStore.tasks.filter((t) =>
      devicesForTask(configStore, t).some((d) => deviceIds.has(d.device_id)),
    )
    const running = affectedTasks.filter((t) => t.runtime === 'RUNNING')
    const protocolChanged = target.protocol !== payload.protocol
    const tableChanged = target.point_table !== payload.point_table
    const connectionChanged =
      JSON.stringify(target.connection_defaults) !== JSON.stringify(payload.connection_defaults)
    const runtimeImpact =
      protocolChanged || tableChanged || connectionChanged || target.read_mode !== payload.read_mode

    if (runtimeImpact && devices.length) {
      try {
        await ElMessageBox.confirm(
          '<b>Device Model Change Impact</b><br><br>' +
            devices.length +
            ' Device(s) affected.<br>' +
            affectedTasks.length +
            ' Task(s) affected; ' +
            running.length +
            ' currently running.<br>' +
            (protocolChanged || connectionChanged
              ? 'Affected protocol connections will be rebuilt.<br>'
              : '') +
            (tableChanged ? 'Point mappings will be replaced.<br>' : '') +
            '<br>Running tasks will be stopped while applying and restored if still valid.',
          'Apply Device Model Changes',
          { type: 'warning', confirmButtonText: 'Apply Changes', dangerouslyUseHTMLString: true },
        )
      } catch {
        return
      }
    }

    const runningIds = new Set(running.map((t) => t.task_id))
    configStore.mutate(() => {
      for (const t of affectedTasks) if (runningIds.has(t.task_id)) t.runtime = 'STOPPED'
      Object.assign(target, payload, { id: target.id })
      refreshTaskValidity(configStore)
      for (const t of affectedTasks) {
        if (runningIds.has(t.task_id) && t.valid !== false && t.enabled) t.runtime = 'RUNNING'
      }
    })
  } else {
    configStore.mutate(() => {
      configStore.deviceModels.push(payload)
    })
  }
  const savedId = id
  const wasEditing = !!editingId.value
  ElMessage.success(wasEditing ? 'Device model updated ' : 'Device model created ')
  if (wasEditing) {
    const saved = configStore.deviceModels.find((x) => x.id === savedId)
    if (saved) openEditModel(saved)
  } else resetForm()
}

function saveType() {
  const id = form.id.trim()
  if (
    !ensureId(
      id,
      configStore.deviceTypes.some((x) => x.id === id),
      'Type',
    )
  )
    return
  if (editingId.value) {
    const target = configStore.deviceTypes.find((x) => x.id === editingId.value)
    if (target) {
      configStore.mutate(() => {
        target.name = form.name.trim() || target.id
      })
    }
  } else {
    configStore.mutate(() => {
      configStore.deviceTypes.push({ id, name: form.name.trim() || id })
    })
  }
  const savedId = id
  const wasEditing = !!editingId.value
  ElMessage.success(wasEditing ? 'Device type updated ' : 'Device type created ')
  if (wasEditing) {
    const saved = configStore.deviceTypes.find((x) => x.id === savedId)
    if (saved) openEditType(saved)
  } else resetForm()
}

async function saveGroup() {
  const id = form.id.trim()
  if (
    !ensureId(
      id,
      configStore.deviceGroups.some((x) => x.id === id),
      'Group',
    )
  )
    return
  if (!form.device_type) {
    ElMessage.error('Device type is required')
    return
  }
  if (editingId.value) {
    const target = configStore.deviceGroups.find((x) => x.id === editingId.value)
    if (!target) return
    if (target.device_type !== form.device_type) {
      const devices = configStore.devices.filter((d) => d.device_group === target.id)
      const incompatible = devices.filter(
        (d) =>
          configStore.deviceModels.find((m) => m.id === d.model)?.device_type !== form.device_type,
      )
      if (incompatible.length) {
        ElMessage.error(
          'Cannot change Device Type: ' +
            incompatible.length +
            ' existing device(s) use incompatible models',
        )
        return
      }
      const tasks = configStore.tasks.filter((t) => t.device_group === target.id)
      if (devices.length || tasks.length) {
        try {
          await ElMessageBox.confirm(
            devices.length +
              ' Device(s) and ' +
              tasks.length +
              ' group Task(s) reference this group. Apply the classification change?',
            'Device Group Change Impact',
            { type: 'warning', confirmButtonText: 'Apply Changes' },
          )
        } catch {
          return
        }
      }
      configStore.mutate(() => {
        target.device_type = form.device_type
      })
    }
  } else {
    configStore.mutate(() => {
      configStore.deviceGroups.push({ id, device_type: form.device_type })
    })
  }
  const savedId = id
  const wasEditing = !!editingId.value
  ElMessage.success(wasEditing ? 'Device group updated ' : 'Device group created ')
  if (wasEditing) {
    const saved = configStore.deviceGroups.find((x) => x.id === savedId)
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
  const model = configStore.deviceModels.find((m) => m.id === editingId.value)
  if (!model) return

  const devices = configStore.devices.filter((d) => d.model === model.id)
  const affected = devices
    .map((device) => ({
      device,
      count:
        Object.keys(device.extensions || {}).filter((key) => key !== 'target_net_id').length +
        (device.port !== undefined ? 1 : 0),
    }))
    .filter((x) => x.count > 0)

  if (!affected.length) {
    ElMessage.info('No Device connection overrides to reset')
    return
  }

  const ids = new Set(affected.map((x) => x.device.device_id))
  const tasks = configStore.tasks.filter((t) =>
    devicesForTask(configStore, t).some((d) => ids.has(d.device_id)),
  )
  const running = tasks.filter((t) => t.runtime === 'RUNNING')
  const overrideCount = affected.reduce((sum, x) => sum + x.count, 0)

  try {
    await ElMessageBox.confirm(
      '<b>Reset Device Overrides?</b><br><br>' +
        affected.length +
        ' Device(s) affected.<br>' +
        overrideCount +
        ' override(s) will be removed.<br>' +
        running.length +
        ' running Task(s) affected.<br><br>' +
        '<b>Preserved:</b> Device ID, Host / Remote IP, Target AMS Net ID and Device Group.',
      'Reset Device Overrides',
      { type: 'warning', confirmButtonText: 'Reset Overrides', dangerouslyUseHTMLString: true },
    )
  } catch {
    return
  }

  const runningIds = new Set(running.map((t) => t.task_id))
  configStore.mutate(() => {
    for (const t of running) t.runtime = 'STOPPED'
    for (const x of affected) resetDeviceConnectionOverrides(x.device)
    refreshTaskValidity(configStore)
    for (const t of tasks) {
      if (runningIds.has(t.task_id) && t.enabled && t.valid !== false) t.runtime = 'RUNNING'
    }
  })
  ElMessage.success('Device connection overrides reset to Model defaults ')
}

async function deleteModel(row: { id: string }) {
  const devices = configStore.devices.filter((d) => d.model === row.id).length
  if (devices) {
    ElMessage.warning('Cannot delete: referenced by ' + devices + ' device(s)')
    return
  }
  try {
    await ElMessageBox.confirm('Delete device model "' + row.id + '"?', 'Delete Device Model', {
      type: 'warning',
    })
  } catch {
    return
  }
  configStore.mutate(() => {
    configStore.deviceModels.splice(
      configStore.deviceModels.findIndex((x) => x.id === row.id),
      1,
    )
  })
  if (editingId.value === row.id) resetForm()
}

async function deleteType(row: { id: string }) {
  const models = configStore.deviceModels.filter((m) => m.device_type === row.id).length
  const groups = configStore.deviceGroups.filter((g) => g.device_type === row.id).length
  if (models || groups) {
    ElMessage.warning(
      'Cannot delete: referenced by ' + models + ' model(s) and ' + groups + ' group(s)',
    )
    return
  }
  try {
    await ElMessageBox.confirm('Delete device type "' + row.id + '"?', 'Delete Device Type', {
      type: 'warning',
    })
  } catch {
    return
  }
  configStore.mutate(() => {
    configStore.deviceTypes.splice(
      configStore.deviceTypes.findIndex((x) => x.id === row.id),
      1,
    )
  })
  if (editingId.value === row.id) resetForm()
}

async function deleteGroup(row: { id: string }) {
  const devices = configStore.devices.filter((d) => d.device_group === row.id).length
  const tasks = configStore.tasks.filter((t) => t.device_group === row.id).length
  if (devices || tasks) {
    ElMessage.warning(
      'Cannot delete: referenced by ' + devices + ' device(s) and ' + tasks + ' task(s)',
    )
    return
  }
  try {
    await ElMessageBox.confirm('Delete device group "' + row.id + '"?', 'Delete Device Group', {
      type: 'warning',
    })
  } catch {
    return
  }
  configStore.mutate(() => {
    configStore.deviceGroups.splice(
      configStore.deviceGroups.findIndex((x) => x.id === row.id),
      1,
    )
  })
  if (editingId.value === row.id) resetForm()
}
</script>

<template>
  <el-dropdown-item v-if="props.dropdownItem" @click="openManager"
    >Manage Metadata</el-dropdown-item
  >
  <el-button v-else @click="openManager">Manage</el-button>

  <el-drawer
    v-model="manageOpen"
    title="Manage Device Metadata"
    direction="rtl"
    :size="isMobile ? '100%' : 'min(var(--app-drawer-width-lg), 86vw)'"
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
      <el-aside width="var(--app-master-pane-width)" class="metadata-list-aside">
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
                <small
                  >{{ row.protocol.toUpperCase() }} · {{ row.point_table }} ·
                  {{ row.devices }} devices</small
                >
              </div>
            </template>
          </el-table-column>
          <el-table-column width="78" align="right">
            <template #default="{ row }"
              ><el-button link type="danger" @click.stop="deleteModel(row)"
                >Delete</el-button
              ></template
            >
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
            <template #default="{ row }"
              ><el-button link type="danger" @click.stop="deleteType(row)"
                >Delete</el-button
              ></template
            >
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
            <template #default="{ row }"
              ><el-button link type="danger" @click.stop="deleteGroup(row)"
                >Delete</el-button
              ></template
            >
          </el-table-column>
        </el-table>
      </el-aside>

      <el-main class="metadata-editor-main">
        <div class="metadata-editor-title">
          <h3>
            {{ editingId ? 'Edit' : 'New' }}
            {{ section === 'model' ? 'Model' : section === 'type' ? 'Type' : 'Group' }}
          </h3>
          <p>{{ editingId ? editingId : '创建后 ID 不再修改' }}</p>
        </div>

        <el-form label-position="top">
          <template v-if="section === 'model'">
            <div class="metadata-form-grid">
              <el-form-item label="Model ID"
                ><el-input v-model="form.id" :disabled="!!editingId"
              /></el-form-item>
              <el-form-item label="Hardware Model"><el-input v-model="form.model" /></el-form-item>
              <el-form-item label="Manufacturer"
                ><el-input v-model="form.manufacturer"
              /></el-form-item>
              <el-form-item label="Device Type"
                ><el-select v-model="form.device_type"
                  ><el-option
                    v-for="t in configStore.deviceTypes"
                    :key="t.id"
                    :label="t.name + ' · ' + t.id"
                    :value="t.id" /></el-select
              ></el-form-item>
              <el-form-item label="Protocol"
                ><el-select v-model="form.protocol" @change="onProtocolChange"
                  ><el-option
                    v-for="p in PROTOCOLS"
                    :key="p"
                    :label="p.toUpperCase()"
                    :value="p" /></el-select
              ></el-form-item>
              <el-form-item label="Point Table"
                ><el-select v-model="form.point_table"
                  ><el-option
                    v-for="t in tablesOfProtocol"
                    :key="t.id"
                    :label="t.id"
                    :value="t.id" /></el-select
              ></el-form-item>
              <el-form-item v-if="form.protocol === 'ads'" label="Read Mode"
                ><el-select v-model="form.read_mode"
                  ><el-option
                    v-for="r in ADS_READ_MODES"
                    :key="r"
                    :label="r"
                    :value="r" /></el-select
              ></el-form-item>
            </div>
            <el-divider content-position="left">Connection Defaults</el-divider>
            <div class="metadata-form-grid">
              <el-form-item label="Port"
                ><el-input-number
                  v-model="form.connection.port"
                  :min="1"
                  :max="65535"
                  :controls="false"
              /></el-form-item>
              <ProtocolConnectionFields
                v-model="form.connection"
                :protocol="form.protocol"
                show-reconnect
                :controls="false"
              />
            </div>
          </template>
          <template v-else-if="section === 'type'">
            <el-form-item label="Type ID"
              ><el-input v-model="form.id" :disabled="!!editingId"
            /></el-form-item>
            <el-form-item label="Name"><el-input v-model="form.name" /></el-form-item>
          </template>
          <template v-else>
            <el-form-item label="Group ID"
              ><el-input v-model="form.id" :disabled="!!editingId"
            /></el-form-item>
            <el-form-item label="Device Type"
              ><el-select v-model="form.device_type" class="app-full-width"
                ><el-option
                  v-for="t in configStore.deviceTypes"
                  :key="t.id"
                  :label="t.name + ' · ' + t.id"
                  :value="t.id" /></el-select
            ></el-form-item>
          </template>
        </el-form>
        <div class="metadata-editor-actions">
          <el-button v-if="section === 'model' && editingId" @click="resetModelDeviceOverrides"
            >Reset Device Overrides</el-button
          >
          <el-button v-if="!editingId" @click="resetForm">Clear</el-button>
          <el-button
            type="primary"
            :disabled="!!editingId && !metadataDirty"
            @click="saveCurrent"
            >{{ editingId ? 'Save' : 'Create' }}</el-button
          >
        </div>
      </el-main>
    </el-container>
  </el-drawer>
</template>

<style scoped>
.metadata-tabs {
  margin-top: calc(-1 * var(--app-space-2));
}
.metadata-layout {
  min-height: var(--app-master-detail-min-height);
}
.metadata-list-aside {
  border-right: 1px solid var(--app-border-soft);
  padding-right: var(--app-space-3);
}
.metadata-editor-main {
  padding: var(--app-space-1) var(--app-space-2) var(--app-space-1) var(--app-space-6) !important;
}
.metadata-editor-title {
  margin-bottom: var(--app-space-4);
}
.metadata-editor-title h3 {
  margin: 0;
  font-size: var(--app-font-section-title);
  font-weight: var(--app-font-weight-semibold);
}
.metadata-editor-title p {
  margin: var(--app-space-1) 0 0;
  color: var(--app-text-muted);
  font-size: var(--app-font-caption);
}
.metadata-form-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 0 var(--app-space-3);
}
.metadata-form-grid :deep(.el-select),
.metadata-form-grid :deep(.el-input-number) {
  width: 100%;
}
.metadata-editor-actions {
  display: flex;
  justify-content: flex-end;
  gap: var(--app-space-2);
  margin-top: var(--app-space-2);
}
@media (max-width: 1199px) {
  .metadata-layout {
    flex-direction: column;
  }
  .metadata-list-aside {
    width: 100% !important;
    border-right: 0;
    border-bottom: 1px solid var(--app-border-soft);
    padding: 0 0 var(--app-space-3);
  }
  .metadata-editor-main {
    padding: var(--app-space-4) 0 0 !important;
  }
  .metadata-form-grid {
    grid-template-columns: 1fr;
  }
}
</style>
