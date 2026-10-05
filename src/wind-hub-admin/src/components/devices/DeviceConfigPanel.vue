<script setup lang="ts">
// 设备配置 panel：编辑表单（身份 + 连接覆盖）、保存/删除与 Connectivity 验证展示。
// 编辑状态与保存/删除编排在 useDeviceEditor；验证编排在 useDeviceVerification。
import { computed } from 'vue'
import ProtocolConnectionFields from './ProtocolConnectionFields.vue'
import {
  useDeviceVerification,
  verifyStepClass,
  verifyStepText,
} from '../../composables/useDeviceVerification'
import type { DeviceEditorFeature } from '../../composables/useDeviceEditor'
import { deviceConnectionOverrides, modelOf, tableOfDevice } from '../../domain/devices'
import { useConfigStore } from '../../stores/config'
import type { DeviceInst } from '../../domain/types'

const props = defineProps<{
  device: DeviceInst
  editor: DeviceEditorFeature
}>()

const configStore = useConfigStore()
const { verifyOf, verifyDevice, verifyingDeviceId, verifyOperationActive } = useDeviceVerification()

const { editForm, dirty: deviceDirty, onEditModelChange, saveConfig, deleteDevice } = props.editor

const selectedVerify = computed(() => verifyOf(props.device))

function overrideKeys(d: DeviceInst): string[] {
  return [
    ...(d.port !== undefined ? ['port'] : []),
    ...Object.keys(deviceConnectionOverrides(configStore, d)).filter(
      (key) => key !== 'target_net_id',
    ),
  ]
}

function protocolDescription(d: DeviceInst) {
  const model = modelOf(configStore, d)
  if (model?.protocol === 'ads') {
    const ams = `${d.host}.1.1`
    return `ADS · AMS ${ams} · Port 801`
  }
  if (model?.protocol === 'modbus') {
    return `Modbus TCP · ${d.host}:${d.port || 502}`
  }
  if (model?.protocol === 'iec104') {
    return `IEC 60870-5-104 · ${d.host}:${d.port || 2404}`
  }
  return model?.protocol || 'Unknown'
}
</script>

<template>
  <div class="config-layout">
    <section class="panel-card">
      <div class="panel-head">
        <div>
          <h3>Device Configuration</h3>
          <p>
            {{ overrideKeys(device).length }} connection override(s) · Model defaults are inherited
          </p>
        </div>
        <div>
          <el-button type="primary" :disabled="!deviceDirty" @click="saveConfig">Save</el-button>
        </div>
      </div>

      <el-form label-position="top" class="device-config-form">
        <el-divider content-position="left">Device Identity</el-divider>
        <div class="form-grid config-edit-grid">
          <el-form-item label="Device ID"
            ><el-input v-model="editForm.device_id" disabled
          /></el-form-item>
          <el-form-item label="Model">
            <el-select v-model="editForm.model" @change="onEditModelChange" class="app-full-width">
              <el-option
                v-for="m in configStore.deviceModels"
                :key="m.id"
                :label="m.id"
                :value="m.id"
              />
            </el-select>
          </el-form-item>
          <el-form-item label="Group">
            <el-select v-model="editForm.device_group" class="app-full-width">
              <el-option
                v-for="g in configStore.deviceGroups.filter(
                  (g) => g.device_type === editForm.device_type,
                )"
                :key="g.id"
                :label="g.id"
                :value="g.id"
              />
            </el-select>
          </el-form-item>
          <el-form-item label="Host / Remote IP"><el-input v-model="editForm.host" /></el-form-item>
          <el-form-item v-if="editForm.protocol === 'ads'" label="Target AMS Net ID"
            ><el-input v-model="editForm.connection.target_net_id"
          /></el-form-item>
        </div>

        <el-divider content-position="left">Connection Overrides</el-divider>
        <div class="form-grid config-edit-grid">
          <el-form-item label="Port"
            ><el-input-number
              v-model="editForm.connection.port"
              :min="1"
              :max="65535"
              class="app-full-width"
          /></el-form-item>
          <ProtocolConnectionFields
            v-model="editForm.connection"
            :protocol="editForm.protocol"
            :modbus-modes="['tcp', 'rtu']"
          />
        </div>

        <el-alert
          type="info"
          :closable="false"
          title="Protocol, Point Table, Read Mode, manufacturer and hardware model are Model properties. Change them in Manage Metadata."
        />
      </el-form>

      <div class="danger-row">
        <el-button type="danger" plain @click="deleteDevice">Delete Device</el-button>
      </div>
    </section>

    <section class="panel-card">
      <div class="panel-head">
        <div>
          <h3>Connectivity</h3>
          <p>网络、协议和点表读取验证</p>
        </div>
        <el-button
          type="primary"
          :loading="verifyingDeviceId === device.device_id"
          :disabled="verifyOperationActive && verifyingDeviceId !== device.device_id"
          @click="verifyDevice(device)"
        >
          Verify Device
        </el-button>
      </div>

      <div class="connectivity-list connectivity-result-list">
        <div>
          <span>Network</span>
          <b :class="verifyStepClass(selectedVerify.network)">{{
            verifyStepText(selectedVerify.network)
          }}</b>
          <small
            >{{ device.host
            }}<template v-if="selectedVerify.latency_ms">
              · {{ selectedVerify.latency_ms }} ms</template
            ></small
          >
        </div>
        <div>
          <span>Protocol</span>
          <b :class="verifyStepClass(selectedVerify.protocol)">{{
            verifyStepText(selectedVerify.protocol)
          }}</b>
          <small>{{ protocolDescription(device) }}</small>
        </div>
        <div>
          <span>Point Read</span>
          <b :class="verifyStepClass(selectedVerify.points)">{{
            verifyStepText(selectedVerify.points)
          }}</b>
          <small>
            <template v-if="selectedVerify.point_total"
              >{{ selectedVerify.point_success }} /
              {{ selectedVerify.point_total }} passed</template
            >
            <template v-else>Point Table: {{ tableOfDevice(configStore, device) }}</template>
          </small>
        </div>
      </div>
    </section>
  </div>

  <section
    v-if="selectedVerify.errors.length"
    class="panel-card verification-panel error-information"
  >
    <div class="panel-head">
      <div>
        <h3>Error Information</h3>
        <p>最近一次验证发现的错误</p>
      </div>
      <span v-if="selectedVerify.verified_at" class="subtle">{{ selectedVerify.verified_at }}</span>
    </div>
    <div class="error-list">
      <div
        v-for="(e, i) in selectedVerify.errors"
        :key="`${e.stage}-${e.target}-${i}`"
        class="error-item"
      >
        <div class="error-item-head">
          <b>{{ e.target }}</b
          ><span>{{ e.stage }}</span>
        </div>
        <p>{{ e.message }}</p>
      </div>
    </div>
  </section>
</template>

<style scoped>
.connectivity-result-list > div {
  display: grid;
  grid-template-columns: minmax(max-content, 0.45fr) minmax(max-content, 0.45fr) minmax(0, 1fr);
  align-items: center;
  gap: var(--app-space-2);
}
.connectivity-result-list b {
  font-size: var(--app-font-body);
  font-weight: var(--app-font-weight-semibold);
}
.connectivity-result-list small {
  min-width: 0;
  color: var(--app-text-muted);
  font-size: var(--app-font-label);
  font-weight: var(--app-font-weight-regular);
  overflow-wrap: anywhere;
}
@media (max-width: 767px) {
  .connectivity-result-list > div {
    grid-template-columns: 1fr;
    gap: var(--app-space-1);
  }
}
</style>
