<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { modelOf, pointsOfTable, store, tableOfDevice, unitSymbol } from '../mock/data'
import { DATA_TYPES, MODBUS_REGISTER_TYPES } from '../mock/types'
import type { DeviceInst } from '../mock/types'

// 现场通信调试：网络/协议连通性、手工读点、原始数据多类型解释、写入测试
const dbgTab = ref('Raw Data')

function deviceById(id: string): DeviceInst | undefined { return store.devices.find(d => d.device_id === id) }
function protoOf(deviceId: string): string { const d = deviceById(deviceId); return d ? (modelOf(d)?.protocol || '') : '' }

// ---- A. Network / Protocol Debug ----
const netDevice = ref('wtg-001')
const netResults = ref<{ time: string, op: string, result: string, ok: boolean }[]>([])
function now() { return new Date().toLocaleTimeString('en-GB') }
function netOp(op: string) {
  const d = deviceById(netDevice.value)
  if (!d) return
  const ok = d.online
  const detail: Record<string, string> = {
    Ping: ok ? `reply from ${d.host} time=${(12 + netResults.value.length * 3) % 60} ms` : `no reply from ${d.host}`,
    'TCP Connect': ok ? `${d.host}:${d.port ?? ''} connected` : `${d.host}:${d.port ?? ''} connection refused`,
    'Protocol Connect': ok ? `${protoOf(d.device_id)} session established` : `${protoOf(d.device_id)} connect failed`,
    'Read Test': ok ? `read 3 points in ${40 + netResults.value.length * 7} ms` : 'read failed: device offline',
  }
  netResults.value.unshift({ time: now(), op, result: detail[op], ok })
}

// ---- B. 手工读点（字段随设备协议变化）----
const read = reactive({ device: 'wtg-001', symbol: 'MAIN.rotorSpeed', index_group: '', index_offset: '', register_type: 'holding', address: 100, ioa: 1001, data_type: 'float32' })
const readProto = computed(() => protoOf(read.device) || 'modbus')
const readResult = ref('')
function doRead() {
  const d = deviceById(read.device)
  if (!d) return
  if (!d.online) { readResult.value = `${now()}  ${d.device_id}: read failed — device offline`; return }
  let addr = ''
  if (readProto.value === 'ads') {
    if (!read.symbol.trim() && !(read.index_group.trim() && read.index_offset.trim())) {
      ElMessage.error("ADS read requires 'symbol' or 'index_group' + 'index_offset'"); return
    }
    addr = read.symbol.trim() || `${read.index_group}/${read.index_offset}`
  } else if (readProto.value === 'modbus') {
    addr = `${read.register_type} ${read.address}`
  } else {
    addr = `ioa ${read.ioa}`
  }
  readResult.value = `${now()}  ${d.device_id}  ${addr}  (${read.data_type}) = ${(Math.random() * 100).toFixed(2)}`
}

// ---- C. 原始数据多类型解释（同一 raw 同时展示全部候选，不做下拉切换）----
const rawDevice = ref('wtg-001')
const rawAddress = ref('31001')
const rawRegs = ref('0x42C8 0x0000')
const scale = ref(1)
const offset = ref(0)
const rawUnit = ref('kilowatt')
const watching = ref(false)
const rawRows = computed(() => [
  { type: 'int16', value: 17096 }, { type: 'uint16', value: 17096 },
  { type: 'int32', value: 1120403456 }, { type: 'uint32', value: 1120403456 },
  { type: 'float32', value: 100.0 }, { type: 'bool', value: true },
].map(r => ({ ...r, engineering: typeof r.value === 'number' ? r.value * scale.value + offset.value : r.value })))
function toggleWatch(on: boolean) {
  watching.value = on
  ElMessage.success(on ? `Watching ${rawDevice.value} @ ${rawAddress.value} (mock)` : 'Watch stopped (mock)')
}

// ---- D. Write Test ----
const wt = reactive({ device: 'wtg-001', point: '', value: '' })
const wtPoints = computed(() => { const d = deviceById(wt.device); return d ? pointsOfTable(tableOfDevice(d)).map(p => p.point_id) : [] })
const wtResults = ref<{ time: string, text: string }[]>([])
function doWrite() {
  if (!wt.point) { ElMessage.warning('Select a point first'); return }
  if (!wt.value.trim()) { ElMessage.warning('Value is required'); return }
  wtResults.value.unshift({ time: now(), text: `${wt.device}/${wt.point} ← ${wt.value} · write ok (mock)` })
  ElMessage.success('Write command sent (mock)')
}
</script>

<template><div><div class="head"><div><h1>Debug</h1><p>网络、协议与原始数据现场调试</p></div></div>
<el-tabs v-model="dbgTab">
<el-tab-pane label="Network" name="Network"><el-card shadow="never"><el-form label-position="top"><div class="grid"><el-form-item label="Device"><el-select v-model="netDevice" filterable><el-option v-for="d in store.devices" :label="`${d.device_id} (${modelOf(d)?.protocol})`" :value="d.device_id"/></el-select></el-form-item></div><el-button @click="netOp('Ping')">Ping</el-button><el-button @click="netOp('TCP Connect')">TCP Connect</el-button><el-button @click="netOp('Protocol Connect')">Protocol Connect</el-button><el-button type="primary" @click="netOp('Read Test')">Read Test</el-button></el-form></el-card><el-card shadow="never" style="margin-top:16px"><el-table :data="netResults" height="300"><el-table-column prop="time" label="Time" width="110"/><el-table-column prop="op" label="Operation" width="160"/><el-table-column prop="result" label="Result"/><el-table-column label="Status" width="100"><template #default="s"><el-tag :type="s.row.ok?'success':'danger'">{{s.row.ok?'OK':'FAIL'}}</el-tag></template></el-table-column></el-table></el-card></el-tab-pane>
<el-tab-pane label="Manual Read" name="Manual Read"><el-card shadow="never"><el-form label-position="top"><div class="grid"><el-form-item label="Device"><el-select v-model="read.device" filterable><el-option v-for="d in store.devices" :label="`${d.device_id} (${modelOf(d)?.protocol})`" :value="d.device_id"/></el-select></el-form-item><template v-if="readProto==='ads'"><el-form-item label="Symbol"><el-input v-model="read.symbol" placeholder="MAIN.rotorSpeed"/></el-form-item><el-form-item label="Index Group"><el-input v-model="read.index_group" placeholder="0x4020"/></el-form-item><el-form-item label="Index Offset"><el-input v-model="read.index_offset" placeholder="0x1234"/></el-form-item></template><template v-else-if="readProto==='modbus'"><el-form-item label="Register Type"><el-select v-model="read.register_type"><el-option v-for="r in MODBUS_REGISTER_TYPES" :label="r" :value="r"/></el-select></el-form-item><el-form-item label="Address (0-based)"><el-input-number v-model="read.address" :min="0" :controls="false" style="width:100%"/></el-form-item></template><template v-else><el-form-item label="IOA"><el-input-number v-model="read.ioa" :min="0" :max="16777215" :controls="false" style="width:100%"/></el-form-item></template><el-form-item label="Data Type"><el-select v-model="read.data_type"><el-option v-for="t in DATA_TYPES" :label="t" :value="t"/></el-select></el-form-item></div><el-button type="primary" @click="doRead">Read</el-button></el-form><pre v-if="readResult" style="margin-top:14px">{{readResult}}</pre></el-card></el-tab-pane>
<el-tab-pane label="Raw Data" name="Raw Data"><div class="upload"><el-card shadow="never"><el-form label-position="top"><el-form-item label="Device"><el-select v-model="rawDevice" filterable style="width:100%"><el-option v-for="d in store.devices" :label="d.device_id" :value="d.device_id"/></el-select></el-form-item><el-form-item label="Address"><el-input v-model="rawAddress"/></el-form-item><el-form-item label="Raw registers"><el-input v-model="rawRegs" readonly/></el-form-item><div class="grid"><el-form-item label="Scale"><el-input-number v-model="scale"/></el-form-item><el-form-item label="Offset"><el-input-number v-model="offset"/></el-form-item><el-form-item label="Unit"><el-select v-model="rawUnit"><el-option v-for="(u,id) in store.units" :label="id+(u.symbol?` (${u.symbol})`:'')" :value="id"/></el-select></el-form-item></div><el-button type="primary" :disabled="watching" @click="toggleWatch(true)">Start Watch</el-button><el-button :disabled="!watching" @click="toggleWatch(false)">Stop</el-button></el-form></el-card>
<el-card shadow="never"><el-table :data="rawRows"><el-table-column prop="type" label="Type"/><el-table-column prop="value" label="Parsed value" min-width="160"/><el-table-column label="Engineering value" min-width="180"><template #default="s">{{s.row.engineering}} {{unitSymbol(rawUnit)}}</template></el-table-column></el-table></el-card></div></el-tab-pane>
<el-tab-pane label="Write Test" name="Write Test"><el-card shadow="never"><el-form label-position="top"><div class="grid"><el-form-item label="Device"><el-select v-model="wt.device" filterable @change="wt.point=''"><el-option v-for="d in store.devices" :label="`${d.device_id} (${modelOf(d)?.protocol})`" :value="d.device_id"/></el-select></el-form-item><el-form-item label="Point"><el-select v-model="wt.point" filterable><el-option v-for="p in wtPoints" :label="p" :value="p"/></el-select></el-form-item><el-form-item label="Value"><el-input v-model="wt.value"/></el-form-item></div><el-button type="primary" @click="doWrite">Send Write</el-button></el-form></el-card><el-card shadow="never" style="margin-top:16px"><el-table :data="wtResults" height="240"><el-table-column prop="time" label="Time" width="110"/><el-table-column prop="text" label="Write Result"/></el-table></el-card></el-tab-pane>
</el-tabs></div></template>
