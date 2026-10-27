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
    /** el-input-number 是否显示步进按钮（Manage Metadata 为紧凑无按钮形态）。 */
    controls?: boolean
  }>(),
  {
    showTargetNetId: false,
    controls: true,
  },
)
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
  </template>

  <template v-else-if="protocol === 'modbus'">
    <el-form-item label="Unit ID"
      ><el-input-number
        v-model="connection.slave_id"
        :min="0"
        :max="255"
        :controls="controls"
        class="app-full-width"
    /></el-form-item>
    <el-form-item label="Mode"
      ><el-select v-model="connection.mode" class="app-full-width"
        ><el-option label="TCP" value="tcp" /></el-select
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
  </template>

  <template v-else-if="protocol === 'iec104'">
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
    <el-form-item label="Read Timeout (s)"
      ><el-input-number v-model="connection.read_timeout" :min="0.1" :controls="controls" class="app-full-width"
    /></el-form-item>
    <el-form-item label="Command Timeout (s)"
      ><el-input-number v-model="connection.command_timeout" :min="0.1" :controls="controls" class="app-full-width"
    /></el-form-item>
    <el-form-item label="Interrogation Timeout (s)"
      ><el-input-number v-model="connection.interrogation_timeout" :min="0.1" :controls="controls" class="app-full-width"
    /></el-form-item>
  </template>
</template>
