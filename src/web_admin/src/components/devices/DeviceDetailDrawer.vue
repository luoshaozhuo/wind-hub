<script setup lang="ts">
// 设备详情 Drawer 编排壳：tab 组合、编辑脏守卫、按 tab 激活的
// Data/Trend feature 生命周期。业务状态在各自 feature composable，
// 这里只做组合（页面以 device_id 为 key 重建本组件实现设备切换重置）。
import { computed, ref, toRef } from 'vue'
import { ElMessageBox } from 'element-plus'
import DeviceCommandPanel from './DeviceCommandPanel.vue'
import DeviceConfigPanel from './DeviceConfigPanel.vue'
import DeviceDataPanel from './DeviceDataPanel.vue'
import DeviceReadTestPanel from './DeviceReadTestPanel.vue'
import DeviceTrendPanel from './DeviceTrendPanel.vue'
import { useDeviceData } from '../../composables/useDeviceData'
import { useDeviceEditor } from '../../composables/useDeviceEditor'
import { useDeviceTrend } from '../../composables/useDeviceTrend'
import {
  statusLabel,
  useDeviceVerification,
  verifyTagType,
} from '../../composables/useDeviceVerification'
import { useViewport } from '../../composables/useViewport'
import { effectiveConnection, modelOf } from '../../domain/devices'
import { useConfigStore } from '../../stores/config'
import type { DeviceInst } from '../../domain/types'

const props = defineProps<{
  device: DeviceInst
}>()

const open = defineModel<boolean>({ required: true })

const configStore = useConfigStore()
const { isMobile } = useViewport()
const drawerSize = computed(() => (isMobile.value ? '100%' : '72%'))

const device = toRef(props, 'device')
const tab = ref('Config')

const editor = useDeviceEditor(device, {
  onDeleted: () => {
    open.value = false
  },
})
editor.loadEditForm()

// Server State 按 tab 激活：与原 DevicesPage 的按需拉取/自动刷新语义一致。
const dataActive = computed(() => open.value && tab.value === 'Data')
const trendActive = computed(() => open.value && tab.value === 'ControlTrend')
const data = useDeviceData(device, dataActive)
const trend = useDeviceTrend(device, trendActive, data.dataRows)
const resolvedPoints = computed(() => data.resolvedPoints.value)

const { verifyOf } = useDeviceVerification()
const selectedVerify = computed(() => verifyOf(props.device))
const selectedModel = computed(() => modelOf(configStore, props.device))

function connValue(key: string, fallback: unknown = '') {
  const value = effectiveConnection(configStore, props.device)[key]
  return value === undefined || value === null || value === '' ? fallback : value
}

async function confirmDiscard(): Promise<boolean> {
  try {
    await ElMessageBox.confirm('Discard unsaved Device changes?', 'Unsaved Changes', {
      type: 'warning',
      confirmButtonText: 'Discard',
    })
    return true
  } catch {
    return false
  }
}

// el-drawer 用户侧关闭（ESC / 遮罩）走 before-close；Close 按钮走 v-model。
async function beforeClose(done: () => void) {
  if (!editor.dirty.value || (await confirmDiscard())) done()
}

async function closeDrawer() {
  if (!editor.dirty.value || (await confirmDiscard())) open.value = false
}
</script>

<template>
  <el-drawer
    v-model="open"
    :size="drawerSize"
    :with-header="false"
    class="device-drawer"
    :before-close="beforeClose"
  >
    <div class="drawer-head">
      <div>
        <div class="drawer-title-row">
          <h2>{{ device.device_id }}</h2>
          <el-tag :type="verifyTagType(selectedVerify)">
            {{ statusLabel(selectedVerify) }}
          </el-tag>
        </div>
        <p>
          {{ device.model }} · {{ selectedModel?.protocol?.toUpperCase() }} · {{ device.host
          }}<template v-if="connValue('port')">:{{ connValue('port') }}</template>
        </p>
      </div>
      <el-button text @click="closeDrawer">Close</el-button>
    </div>

    <el-tabs v-model="tab" class="drawer-tabs">
      <el-tab-pane label="Config" name="Config">
        <DeviceConfigPanel :device="device" :editor="editor" />
      </el-tab-pane>

      <el-tab-pane label="Read Test" name="ReadTest">
        <DeviceReadTestPanel :device="device" :resolved-points="resolvedPoints" />
      </el-tab-pane>

      <el-tab-pane label="Data" name="Data">
        <DeviceDataPanel :data="data" />
      </el-tab-pane>

      <el-tab-pane label="Control & Trend" name="ControlTrend">
        <div class="control-trend-layout">
          <DeviceCommandPanel :device="device" :data="data" @sent="trend.render" />
          <DeviceTrendPanel :trend="trend" />
        </div>
      </el-tab-pane>
    </el-tabs>
  </el-drawer>
</template>

<style scoped>
.control-trend-layout {
  display: grid;
  grid-template-columns: minmax(var(--app-layout-pane-min-width), 0.72fr) minmax(0, 1.28fr);
  gap: var(--app-space-4);
  align-items: start;
}
@media (max-width: 1199px) {
  .control-trend-layout {
    grid-template-columns: minmax(var(--app-layout-pane-min-width), 0.8fr) minmax(0, 1.2fr);
  }
}
@media (max-width: 767px) {
  .control-trend-layout {
    grid-template-columns: 1fr;
  }
}
</style>
