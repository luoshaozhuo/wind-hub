<script setup lang="ts">
// 新建设备 feature：单台创建表单与批量模板创建，独立于设备列表页。
// 协议连接字段逻辑统一走 domain/deviceConnection；批量预览走 domain/deviceBatch。
import { computed, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import ProtocolConnectionFields from './ProtocolConnectionFields.vue'
import {
  amsNetIdFromHost,
  applyModelDefaults,
  connectionOverridesFromForm,
  defaultConnectionForm,
  validateConnectionForm,
} from '../../domain/deviceConnection'
import { buildBatchPreview, type BatchTemplate } from '../../domain/deviceBatch'
import { emptyVerification } from '../../domain/devices'
import { useConfigStore } from '../../stores/config'
import { useViewport } from '../../composables/useViewport'

const open = defineModel<boolean>({ required: true })

const configStore = useConfigStore()
const { isMobile } = useViewport()

const addMode = ref<'single' | 'batch'>('single')

// ---- 单台创建 ----
const newDev = ref({
  id: '',
  type: 'turbine',
  group: 'turbine_modbus',
  model: 'modbus_wtg',
  host: '',
  enabled: true,
  connection: defaultConnectionForm('modbus'),
})

const newDevModel = computed(() =>
  configStore.deviceModels.find((m) => m.id === newDev.value.model),
)

function applyNewModelDefaults() {
  const model = newDevModel.value
  if (!model) return

  newDev.value.type = model.device_type
  applyModelDefaults(newDev.value.connection, model, newDev.value.host)

  const validGroup = configStore.deviceGroups.some(
    (g) => g.id === newDev.value.group && g.device_type === model.device_type,
  )
  if (!validGroup) {
    newDev.value.group =
      configStore.deviceGroups.find((g) => g.device_type === model.device_type)?.id || ''
  }
}

function onNewType() {
  const model = configStore.deviceModels.find((m) => m.device_type === newDev.value.type)
  if (model) {
    newDev.value.model = model.id
    applyNewModelDefaults()
  } else {
    newDev.value.model = ''
    newDev.value.group =
      configStore.deviceGroups.find((g) => g.device_type === newDev.value.type)?.id || ''
  }
}

function onNewModel() {
  applyNewModelDefaults()
}

function onNewHostChange() {
  if (newDevModel.value?.protocol === 'ads' && newDev.value.host.trim()) {
    newDev.value.connection.target_net_id = amsNetIdFromHost(newDev.value.host)
  }
}

function resetSingleForm() {
  addMode.value = 'single'
  newDev.value = {
    id: '',
    type: 'turbine',
    group: 'turbine_modbus',
    model: 'modbus_wtg',
    host: '',
    enabled: true,
    connection: defaultConnectionForm('modbus'),
  }
  applyNewModelDefaults()
}

watch(open, (value) => {
  if (value) resetSingleForm()
})

function addDevice() {
  const id = newDev.value.id.trim()
  const host = newDev.value.host.trim()
  const model = newDevModel.value

  if (!id || !host) {
    ElMessage.error('Device ID and Host are required')
    return
  }
  if (configStore.devices.some((d) => d.device_id === id)) {
    ElMessage.error(`Device ${id} already exists`)
    return
  }
  if (!model) {
    ElMessage.error('Select a valid model')
    return
  }
  if (
    !configStore.deviceGroups.some(
      (g) => g.id === newDev.value.group && g.device_type === model.device_type,
    )
  ) {
    ElMessage.error('Select a valid device group')
    return
  }

  const connError = validateConnectionForm(newDev.value.connection, model.protocol)
  if (connError) {
    ElMessage.error(connError)
    return
  }
  const { extensions, port } = connectionOverridesFromForm(newDev.value.connection, model)

  configStore.mutate(() => {
    configStore.devices.push({
      device_id: id,
      model: model.id,
      device_group: newDev.value.group,
      host,
      port,
      extensions,
      enabled: newDev.value.enabled,
      online: false,
    })
    configStore.deviceVerification[id] = emptyVerification()
  })
  open.value = false
  ElMessage.success('Device created ')
}

// ---- 批量创建 ----
const batch = ref<BatchTemplate>({
  model: 'beckhoff_wtg',
  group: 'turbine_ads',
  from: 1,
  to: 48,
  exclude: '',
  id_pattern: 'wtg-{num:03}',
  host_pattern: '192.168.151.{num}',
  netid_pattern: '192.168.151.{num}.1.1',
})
const batchModel = computed(() => configStore.deviceModels.find((m) => m.id === batch.value.model))

const batchPreview = computed(() =>
  buildBatchPreview(batch.value, batchModel.value, configStore.devices),
)

function onBatchModelChange() {
  const model = batchModel.value
  if (!model) return
  if (
    !configStore.deviceGroups.some(
      (g) => g.id === batch.value.group && g.device_type === model.device_type,
    )
  ) {
    batch.value.group =
      configStore.deviceGroups.find((g) => g.device_type === model.device_type)?.id || ''
  }
}

function createBatchDevices() {
  const model = batchModel.value
  const rows = batchPreview.value
  if (!model || !rows.length) {
    ElMessage.error('No devices to create')
    return
  }
  if (rows.some((r) => r.error)) {
    ElMessage.error('Resolve all Batch Preview errors before creating devices')
    return
  }
  const group = configStore.deviceGroups.find((g) => g.id === batch.value.group)
  if (!group || group.device_type !== model.device_type) {
    ElMessage.error('Device Group must match the selected Model')
    return
  }

  configStore.mutate(() => {
    for (const row of rows) {
      const extensions: Record<string, unknown> = {}
      if (model.protocol === 'ads') extensions.target_net_id = row.netid
      configStore.devices.push({
        device_id: row.id,
        model: model.id,
        device_group: batch.value.group,
        host: row.host,
        port: undefined,
        extensions,
        enabled: true,
        online: false,
      })
      configStore.deviceVerification[row.id] = emptyVerification()
    }
  })
  open.value = false
  ElMessage.success(rows.length + ' devices created from templates ')
}
</script>

<template>
  <el-drawer
    v-model="open"
    title="Add Device"
    direction="rtl"
    :size="isMobile ? '100%' : addMode === 'batch' ? 'min(980px, 86vw)' : 'min(760px, 78vw)'"
    append-to-body
    destroy-on-close
  >
    <el-tabs v-model="addMode">
      <el-tab-pane label="Single" name="single">
        <el-alert
          type="info"
          :closable="false"
          title="Model connection defaults are inherited. Only values that differ from the Model are stored as Device overrides."
        />
        <el-form label-position="top" class="add-device-form">
          <div class="form-grid add-device-grid">
            <el-form-item label="Device ID"
              ><el-input v-model="newDev.id" placeholder="wtg-001"
            /></el-form-item>
            <el-form-item label="Type"
              ><el-select v-model="newDev.type" @change="onNewType" class="app-full-width"
                ><el-option
                  v-for="t in configStore.deviceTypes"
                  :key="t.id"
                  :label="t.name"
                  :value="t.id" /></el-select
            ></el-form-item>
            <el-form-item label="Model"
              ><el-select v-model="newDev.model" @change="onNewModel" class="app-full-width"
                ><el-option
                  v-for="m in configStore.deviceModels.filter((m) => m.device_type === newDev.type)"
                  :key="m.id"
                  :label="m.id"
                  :value="m.id" /></el-select
            ></el-form-item>
            <el-form-item label="Group"
              ><el-select v-model="newDev.group" class="app-full-width"
                ><el-option
                  v-for="g in configStore.deviceGroups.filter((g) => g.device_type === newDev.type)"
                  :key="g.id"
                  :label="g.id"
                  :value="g.id" /></el-select
            ></el-form-item>
            <el-form-item label="Protocol"
              ><el-input :model-value="newDevModel?.protocol?.toUpperCase() || ''" disabled
            /></el-form-item>
            <el-form-item label="Host / Remote IP"
              ><el-input
                v-model="newDev.host"
                placeholder="192.168.151.1"
                @change="onNewHostChange"
            /></el-form-item>
            <el-form-item label="Port"
              ><el-input-number
                v-model="newDev.connection.port"
                :min="1"
                :max="65535"
                class="app-full-width"
            /></el-form-item>
            <el-form-item label="Enabled"><el-switch v-model="newDev.enabled" /></el-form-item>
          </div>

          <div v-if="newDevModel" class="form-grid add-device-grid">
            <ProtocolConnectionFields
              v-model="newDev.connection"
              :protocol="newDevModel.protocol"
              show-target-net-id
            />
          </div>
        </el-form>
      </el-tab-pane>

      <el-tab-pane label="Batch" name="batch">
        <el-alert
          type="info"
          :closable="false"
          show-icon
          title="Templates: {num}, {num:03}, {num+100}, {num+100:03}. Exclude examples: 5,17,30-32."
        />
        <el-form label-position="top">
          <div class="form-grid add-device-grid">
            <el-form-item label="Model"
              ><el-select v-model="batch.model" @change="onBatchModelChange" class="app-full-width"
                ><el-option
                  v-for="m in configStore.deviceModels"
                  :key="m.id"
                  :label="m.id + ' · ' + m.protocol.toUpperCase()"
                  :value="m.id" /></el-select
            ></el-form-item>
            <el-form-item label="Group"
              ><el-select v-model="batch.group" class="app-full-width"
                ><el-option
                  v-for="g in configStore.deviceGroups.filter(
                    (g) => g.device_type === batchModel?.device_type,
                  )"
                  :key="g.id"
                  :label="g.id"
                  :value="g.id" /></el-select
            ></el-form-item>
            <el-form-item label="From"
              ><el-input-number v-model="batch.from" :min="0" :max="9999" class="app-full-width"
            /></el-form-item>
            <el-form-item label="To"
              ><el-input-number v-model="batch.to" :min="0" :max="9999" class="app-full-width"
            /></el-form-item>
            <el-form-item label="Exclude"
              ><el-input v-model="batch.exclude" placeholder="5,17,30-32"
            /></el-form-item>
            <el-form-item label="Device ID Pattern"
              ><el-input v-model="batch.id_pattern" placeholder="wtg-{num:03}"
            /></el-form-item>
            <el-form-item label="Host Pattern"
              ><el-input v-model="batch.host_pattern" placeholder="192.168.151.{num}"
            /></el-form-item>
            <el-form-item v-if="batchModel?.protocol === 'ads'" label="Target AMS Net ID Pattern"
              ><el-input v-model="batch.netid_pattern" placeholder="192.168.151.{num}.1.1"
            /></el-form-item>
          </div>
        </el-form>
        <el-divider content-position="left">Preview · {{ batchPreview.length }} Devices</el-divider>
        <el-table :data="batchPreview" max-height="340" size="small">
          <el-table-column prop="num" label="#" width="64" />
          <el-table-column prop="id" label="Device ID" min-width="140" />
          <el-table-column prop="host" label="Host" min-width="150" />
          <el-table-column
            v-if="batchModel?.protocol === 'ads'"
            prop="netid"
            label="Target AMS Net ID"
            min-width="180"
          />
          <el-table-column label="Validation" min-width="170"
            ><template #default="s"
              ><el-tag :type="s.row.error ? 'danger' : 'success'" size="small">{{
                s.row.error || 'OK'
              }}</el-tag></template
            ></el-table-column
          >
        </el-table>
      </el-tab-pane>
    </el-tabs>

    <template #footer>
      <el-button @click="open = false">Cancel</el-button>
      <el-button v-if="addMode === 'single'" type="primary" @click="addDevice"
        >Add Device</el-button
      >
      <el-button
        v-else
        type="primary"
        :disabled="!batchPreview.length || batchPreview.some((r) => !!r.error)"
        @click="createBatchDevices"
        >Create {{ batchPreview.length }} Devices</el-button
      >
    </template>
  </el-drawer>
</template>
