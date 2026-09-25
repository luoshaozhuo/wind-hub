<script setup lang="ts">
import { computed, ref } from 'vue'
const scale = ref(1)
const offset = ref(0)
const unit = ref('kW')
const rows = computed(() => [
  ['int16', 17096], ['uint16', 17096], ['int32', 1120403456], ['uint32', 1120403456], ['float32', 100.0], ['bool', true],
].map(([type, value]) => ({ type, value, engineering: typeof value === 'number' ? value * scale.value + offset.value : value })))
</script>
<template><div><div class="page-head"><div><h1>Debug</h1><p>网络、协议与原始数据现场调试</p></div></div>
<el-tabs><el-tab-pane label="Raw Data"><div class="two-col"><el-card shadow="never"><el-form label-position="top"><el-form-item label="Device"><el-select model-value="wtg-002" style="width:100%"><el-option label="wtg-002" value="wtg-002"/></el-select></el-form-item><el-form-item label="Address"><el-input model-value="31001"/></el-form-item><el-form-item label="Raw registers"><el-input model-value="0x42C8 0x0000" readonly/></el-form-item><div class="field-grid"><el-form-item label="Scale"><el-input-number v-model="scale"/></el-form-item><el-form-item label="Offset"><el-input-number v-model="offset"/></el-form-item><el-form-item label="Unit"><el-input v-model="unit"/></el-form-item></div><el-button type="primary">Start Watch</el-button><el-button>Stop</el-button></el-form></el-card>
<el-card shadow="never"><el-table :data="rows"><el-table-column prop="type" label="Type"/><el-table-column prop="value" label="Parsed value" min-width="160"/><el-table-column label="Engineering value" min-width="180"><template #default="s">{{s.row.engineering}} {{unit}}</template></el-table-column></el-table></el-card></div></el-tab-pane><el-tab-pane label="Network">Ping / TCP / discovery placeholder</el-tab-pane><el-tab-pane label="Modbus">Modbus protocol debug placeholder</el-tab-pane><el-tab-pane label="ADS">ADS route / state / symbol debug placeholder</el-tab-pane><el-tab-pane label="IEC104">IEC104 debug placeholder</el-tab-pane></el-tabs></div></template>
