<script setup lang="ts">
// Devices 页：列表/筛选/分页/分组卡片编排，以及页面级批量操作
// （Verify All、Delete All、启停）。设备详情、新建、数据、趋势、命令、
// 验证等业务行为已拆到 components/devices 与 composables（§3 职责拆分）。
import { computed, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import DeviceMetadataManager from '../components/DeviceMetadataManager.vue'
import DeviceCreateDrawer from '../components/devices/DeviceCreateDrawer.vue'
import DeviceDetailDrawer from '../components/devices/DeviceDetailDrawer.vue'
import { useDeviceVerification } from '../composables/useDeviceVerification'
import { useViewport } from '../composables/useViewport'
import { modelOf } from '../domain/devices'
import { tasksAffectedByDevice } from '../domain/tasks'
import { useConfigStore } from '../stores/config'
import type { DeviceInst } from '../domain/types'
import { EMPTY } from '../utils/format'
import { statusTagType } from '../utils/status'

const configStore = useConfigStore()
const modelOfDevice = (d: DeviceInst) => modelOf(configStore, d)

const { verifyAllRunning, verifyOperationActive, verifyOf, verifyAll } = useDeviceVerification()

const search = ref('')
const typeFilter = ref('All')
const modelFilter = ref('All')
const statusFilter = ref('All')
const drawer = ref(false)
const selected = ref<DeviceInst | null>(null)
const { isMobile } = useViewport()

function typeName(typeId: string) {
  return configStore.deviceTypes.find((x) => x.id === typeId)?.name || typeId
}

const filteredDevices = computed(() =>
  configStore.devices.filter((d) => {
    const model = modelOf(configStore, d)
    const v = verifyOf(d)
    const q = search.value.trim().toLowerCase()
    const matchesSearch =
      !q ||
      [d.device_id, d.host, d.model, d.device_group, model?.protocol || ''].some((x) =>
        x.toLowerCase().includes(q),
      )

    const matchesType = typeFilter.value === 'All' || model?.device_type === typeFilter.value
    const matchesModel = modelFilter.value === 'All' || d.model === modelFilter.value
    const matchesStatus =
      statusFilter.value === 'All' ||
      (statusFilter.value === 'Healthy' && v.state === 'success') ||
      (statusFilter.value === 'Warning' && v.state === 'warning') ||
      (statusFilter.value === 'Fault' && v.state === 'failed') ||
      (statusFilter.value === 'Unverified' && v.state === 'idle')

    return matchesSearch && matchesType && matchesModel && matchesStatus
  }),
)

const devicePage = ref(1)
const devicePageSize = ref(24)
const pagedDevices = computed(() => {
  const start = (devicePage.value - 1) * devicePageSize.value
  return filteredDevices.value.slice(start, start + devicePageSize.value)
})
watch([search, typeFilter, modelFilter, statusFilter], () => {
  devicePage.value = 1
})

function groupKeyOf(d: DeviceInst) {
  const model = modelOf(configStore, d)
  return `${model?.device_type || 'unknown'}::${d.model}`
}

const groupedDevices = computed(() => {
  // 组统计基于 filteredDevices（过滤后的全量），分页只决定当前页显示哪些卡片，
  // 避免组头 healthy/warning/fault 计数被误读为全局统计。
  const stats = new Map<
    string,
    { total: number; healthy: number; warning: number; failed: number }
  >()
  for (const d of filteredDevices.value) {
    const key = groupKeyOf(d)
    if (!stats.has(key)) stats.set(key, { total: 0, healthy: 0, warning: 0, failed: 0 })
    const s = stats.get(key)!
    s.total += 1
    const state = verifyOf(d).state
    if (state === 'success') s.healthy += 1
    else if (state === 'warning') s.warning += 1
    else if (state === 'failed') s.failed += 1
  }

  const groups = new Map<string, DeviceInst[]>()
  for (const d of pagedDevices.value) {
    const key = groupKeyOf(d)
    if (!groups.has(key)) groups.set(key, [])
    groups.get(key)!.push(d)
  }

  return Array.from(groups.entries()).map(([key, devices]) => {
    const [typeId, modelId] = key.split('::')
    const model = configStore.deviceModels.find((m) => m.id === modelId)
    const s = stats.get(key) || { total: devices.length, healthy: 0, warning: 0, failed: 0 }
    return {
      key,
      typeId,
      modelId,
      model,
      devices,
      total: s.total,
      healthy: s.healthy,
      warning: s.warning,
      failed: s.failed,
    }
  })
})

function verifyAllFiltered() {
  void verifyAll([...filteredDevices.value])
}

// ---- 启停 ----
async function changeDeviceEnabled(d: DeviceInst, enabled: boolean) {
  const affected = tasksAffectedByDevice(configStore, d)
  const running = affected.filter((t) => t.runtime === 'RUNNING')
  if (!enabled && running.length) {
    try {
      await ElMessageBox.confirm(
        'Disabling "' +
          d.device_id +
          '" affects ' +
          running.length +
          ' running Task definition(s). The device instances will be stopped; other group members remain unaffected.',
        'Disable Device',
        { type: 'warning', confirmButtonText: 'Disable' },
      )
    } catch {
      return
    }
  }
  configStore.mutate(() => {
    d.enabled = enabled
    if (!enabled) {
      for (const t of running) {
        if (t.device === d.device_id) t.runtime = 'STOPPED'
      }
    }
  })
}

// ---- 详情 Drawer：以 device_id 为 key 重建，设备切换即重置全部详情状态 ----
function openDev(d: DeviceInst) {
  selected.value = d
  drawer.value = true
}

// ---- Delete All ----
const addOpen = ref(false)
const deleteAllOpen = ref(false)
const deleteAllConfirm = ref('')

function openDeleteAll() {
  if (!configStore.devices.length) {
    ElMessage.info('No devices to delete')
    return
  }
  deleteAllConfirm.value = ''
  deleteAllOpen.value = true
}

function deleteAllDevices() {
  if (deleteAllConfirm.value !== 'DELETE ALL') return
  const count = configStore.devices.length
  configStore.mutate(() => {
    for (const t of configStore.tasks) t.runtime = 'STOPPED'
    configStore.devices = []
    for (const key of Object.keys(configStore.deviceVerification))
      delete configStore.deviceVerification[key]
  })
  selected.value = null
  drawer.value = false
  deleteAllOpen.value = false
  ElMessage.success(count + ' devices deleted; Task Definitions preserved and revalidated ')
}
</script>

<template>
  <div class="devices-page">
    <div class="head page-head">
      <div>
        <h1>Devices</h1>
        <p>设备资产、通信状态、诊断、实时数据与控制</p>
      </div>
      <div class="head-actions">
        <el-button type="primary" @click="addOpen = true">+ Add Device</el-button>
        <el-dropdown trigger="click">
          <el-button :loading="verifyAllRunning">Actions</el-button>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item
                :disabled="verifyOperationActive || !filteredDevices.length"
                @click="verifyAllFiltered"
              >
                Verify All
              </el-dropdown-item>
              <DeviceMetadataManager dropdown-item />
              <el-dropdown-item
                divided
                :disabled="!configStore.devices.length"
                @click="openDeleteAll"
              >
                Delete All Devices
              </el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
      </div>
    </div>

    <el-card shadow="never" class="filter-card">
      <div class="filter-grid">
        <el-input
          v-model="search"
          clearable
          placeholder="Search device ID / IP / model / protocol..."
        />
        <el-select v-model="typeFilter" class="device-filter">
          <el-option label="All Types" value="All" />
          <el-option
            v-for="t in configStore.deviceTypes"
            :key="t.id"
            :label="t.name"
            :value="t.id"
          />
        </el-select>
        <el-select v-model="modelFilter" class="device-filter">
          <el-option label="All Models" value="All" />
          <el-option
            v-for="m in configStore.deviceModels"
            :key="m.id"
            :label="m.id"
            :value="m.id"
          />
        </el-select>
        <el-select v-model="statusFilter" class="device-filter">
          <el-option label="All Status" value="All" />
          <el-option label="Healthy" value="Healthy" />
          <el-option label="Warning" value="Warning" />
          <el-option label="Fault" value="Fault" />
          <el-option label="Unverified" value="Unverified" />
        </el-select>
      </div>
    </el-card>

    <el-empty
      v-if="!groupedDevices.length"
      description="No devices match current filters."
      :image-size="72"
    />

    <section v-for="group in groupedDevices" :key="group.key" class="model-group">
      <div class="group-head">
        <div>
          <div class="group-title">
            <h2>{{ typeName(group.typeId) }}</h2>
            <span>/</span>
            <b>{{ group.modelId }}</b>
          </div>
          <div class="group-meta">
            <span>{{ group.model?.protocol?.toUpperCase() }}</span>
            <span>Point Table: {{ group.model?.point_table }}</span>
            <span>{{ group.total }} devices</span>
          </div>
        </div>
        <div class="group-summary">
          <span class="ok">{{ group.healthy }} healthy</span>
          <span class="warn">{{ group.warning }} warning</span>
          <span class="bad">{{ group.failed }} fault</span>
        </div>
      </div>

      <div class="device-grid">
        <el-card
          v-for="d in group.devices"
          :key="d.device_id"
          shadow="never"
          class="device-card device-card-compact"
          :class="{ 'device-disabled': !d.enabled }"
        >
          <div class="device-card-top">
            <el-button link class="device-id-link" @click="openDev(d)">{{ d.device_id }}</el-button>
            <div class="device-enabled">
              <span>Enabled</span>
              <el-switch
                :model-value="d.enabled"
                size="small"
                @change="changeDeviceEnabled(d, !!$event)"
              />
            </div>
          </div>

          <div class="device-fields">
            <div>
              <span>Protocol</span>
              <b>{{ modelOfDevice(d)?.protocol?.toUpperCase() || EMPTY }}</b>
            </div>
            <div>
              <span>IP</span>
              <b>{{ d.host }}</b>
            </div>
            <div>
              <span>Port</span>
              <b>{{ d.port || modelOfDevice(d)?.connection_defaults?.port || EMPTY }}</b>
            </div>
          </div>

          <div class="status-pills">
            <el-tag
              size="small"
              :type="statusTagType(verifyOf(d).network)"
              :effect="verifyOf(d).network === 'checking' ? 'dark' : 'light'"
              >Network</el-tag
            >
            <el-tag
              size="small"
              :type="statusTagType(verifyOf(d).protocol)"
              :effect="verifyOf(d).protocol === 'checking' ? 'dark' : 'light'"
              >Protocol</el-tag
            >
            <el-tag
              size="small"
              :type="statusTagType(verifyOf(d).points)"
              :effect="verifyOf(d).points === 'checking' ? 'dark' : 'light'"
              >Points</el-tag
            >
          </div>
        </el-card>
      </div>
    </section>

    <div v-if="filteredDevices.length > devicePageSize" class="pagination">
      <el-pagination
        v-model:current-page="devicePage"
        v-model:page-size="devicePageSize"
        :page-sizes="[12, 24, 48]"
        :total="filteredDevices.length"
        :layout="isMobile ? 'prev, pager, next' : 'total, sizes, prev, pager, next'"
      />
    </div>

    <DeviceCreateDrawer v-model="addOpen" />

    <DeviceDetailDrawer
      v-if="selected"
      :key="selected.device_id"
      v-model="drawer"
      :device="selected"
    />

    <el-dialog
      v-model="deleteAllOpen"
      title="Delete All Devices"
      width="var(--app-dialog-width-sm)"
    >
      <el-alert
        type="error"
        :closable="false"
        show-icon
        title="All Devices will be removed. Metadata and Task Definitions are preserved."
      />
      <el-descriptions :column="1" border size="small" class="delete-summary">
        <el-descriptions-item label="Devices">{{
          configStore.devices.length
        }}</el-descriptions-item>
        <el-descriptions-item label="Task Definitions"
          >{{ configStore.tasks.length }} preserved</el-descriptions-item
        >
        <el-descriptions-item label="Running Tasks"
          >{{ configStore.tasks.filter((t) => t.runtime === 'RUNNING').length }} will
          stop</el-descriptions-item
        >
        <el-descriptions-item label="After Delete"
          >Tasks without target devices become INVALID</el-descriptions-item
        >
      </el-descriptions>
      <el-form label-position="top">
        <el-form-item label='Type "DELETE ALL" to confirm'
          ><el-input v-model="deleteAllConfirm" autocomplete="off"
        /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="deleteAllOpen = false">Cancel</el-button>
        <el-button
          type="danger"
          :disabled="deleteAllConfirm !== 'DELETE ALL'"
          @click="deleteAllDevices"
          >Delete All Devices</el-button
        >
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.delete-summary {
  margin: var(--app-space-4) 0;
}
</style>
