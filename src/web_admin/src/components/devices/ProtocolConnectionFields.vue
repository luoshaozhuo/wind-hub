<script setup lang="ts">
// 协议连接字段的统一渲染：ADS / Modbus / IEC104 三组字段只在这里维护，
// 新建设备、编辑设备配置、Manage Metadata 模型默认值三处复用。
// 字段语义与默认值见 domain/deviceConnection.ts。
import type { ConnectionFormState } from '../../domain/deviceConnection'
import type { Protocol } from '../../domain/types'

const connection = defineModel<ConnectionFormState>({ required: true })

withDefaults(
  defineProps<{
    protocol: Protocol
    /** 渲染 ADS Target AMS Net ID（仅新建设备表单把身份键放在协议区）。 */
    showTargetNetId?: boolean
    /** Modbus Mode 可选值：编辑设备允许 tcp/rtu，其余仅 tcp。 */
    modbusModes?: string[]
    /** 渲染重连参数（仅 Manage Metadata 的模型默认值表单）。 */
    showReconnect?: boolean
    /** el-input-number 是否显示步进按钮（Manage Metadata 为紧凑无按钮形态）。 */
    controls?: boolean
  }>(),
  {
    showTargetNetId: false,
    modbusModes: () => ['tcp'],
    showReconnect: false,
    controls: true,
  },
)

const MODE_LABELS: Record<string, string> = { tcp: 'TCP', rtu: 'RTU' }
</script>

<template>
  <template v-if="protocol === 'ads'">
    <el-form-item v-if="showTargetNetId" label="Target AMS Net ID"
      ><el-input v-model="connection.target_net_id"
    /></el-form-item>
    <el-form-item label="TwinCAT Version"
      ><el-select
        v-model="connection.twincat_version"
        class="app-full-width"
        @change="connection.port = $event === '3' ? 851 : 801"
        ><el-option label="TwinCAT 2" value="2" /><el-option
          label="TwinCAT 3"
          value="3" /></el-select
    ></el-form-item>
    <el-form-item label="Timeout (s)"
      ><el-input-number
        v-model="connection.timeout"
        :min="0.1"
        :step="0.5"
        :controls="controls"
        class="app-full-width"
    /></el-form-item>
    <template v-if="showReconnect">
      <el-form-item label="Reconnect Max Retries"
        ><el-input-number
          v-model="connection.reconnect_max_retries"
          :min="0"
          :controls="controls"
          class="app-full-width"
      /></el-form-item>
      <el-form-item label="Reconnect Backoff Max (s)"
        ><el-input-number
          v-model="connection.reconnect_backoff_max"
          :min="0"
          :controls="controls"
          class="app-full-width"
      /></el-form-item>
    </template>
  </template>

  <template v-else-if="protocol === 'modbus'">
    <el-form-item label="Unit ID"
      ><el-input-number
        v-model="connection.unit_id"
        :min="0"
        :max="255"
        :controls="controls"
        class="app-full-width"
    /></el-form-item>
    <el-form-item label="Mode"
      ><el-select v-model="connection.mode" class="app-full-width"
        ><el-option
          v-for="m in modbusModes"
          :key="m"
          :label="MODE_LABELS[m] || m.toUpperCase()"
          :value="m" /></el-select
    ></el-form-item>
    <el-form-item label="Timeout (s)"
      ><el-input-number
        v-model="connection.timeout"
        :min="0.1"
        :step="0.5"
        :controls="controls"
        class="app-full-width"
    /></el-form-item>
    <el-form-item label="Word Order"
      ><el-select v-model="connection.word_order" class="app-full-width"
        ><el-option label="little_endian" value="little_endian" /><el-option
          label="big_endian"
          value="big_endian" /></el-select
    ></el-form-item>
    <template v-if="showReconnect">
      <el-form-item label="Reconnect Max Retries"
        ><el-input-number
          v-model="connection.reconnect_max_retries"
          :min="0"
          :controls="controls"
          class="app-full-width"
      /></el-form-item>
      <el-form-item label="Reconnect Backoff Max (s)"
        ><el-input-number
          v-model="connection.reconnect_backoff_max"
          :min="0"
          :controls="controls"
          class="app-full-width"
      /></el-form-item>
    </template>
  </template>

  <template v-else-if="protocol === 'iec104'">
    <el-form-item label="Common Address"
      ><el-input-number
        v-model="connection.common_addr"
        :min="1"
        :max="65535"
        :controls="controls"
        class="app-full-width"
    /></el-form-item>
    <el-form-item label="K Window"
      ><el-input-number v-model="connection.k" :min="1" :controls="controls" class="app-full-width"
    /></el-form-item>
    <el-form-item label="W Window"
      ><el-input-number v-model="connection.w" :min="1" :controls="controls" class="app-full-width"
    /></el-form-item>
    <el-form-item label="T0 (s)"
      ><el-input-number
        v-model="connection.t0"
        :min="0.1"
        :controls="controls"
        class="app-full-width"
    /></el-form-item>
    <el-form-item label="T1 (s)"
      ><el-input-number
        v-model="connection.t1"
        :min="0.1"
        :controls="controls"
        class="app-full-width"
    /></el-form-item>
    <el-form-item label="T2 (s)"
      ><el-input-number
        v-model="connection.t2"
        :min="0.1"
        :controls="controls"
        class="app-full-width"
    /></el-form-item>
    <el-form-item label="T3 (s)"
      ><el-input-number
        v-model="connection.t3"
        :min="0.1"
        :controls="controls"
        class="app-full-width"
    /></el-form-item>
    <el-form-item label="Max Reconnect Retries"
      ><el-input-number
        v-model="connection.max_reconnect_retries"
        :min="0"
        :controls="controls"
        class="app-full-width"
    /></el-form-item>
  </template>
</template>
