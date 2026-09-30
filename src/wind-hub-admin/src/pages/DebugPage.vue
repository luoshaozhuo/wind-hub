<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  effectiveConnection,
  pointsOfTable,
  protocolOfDevice,
  store,
  tableOfDevice,
  unitSymbol,
} from '../mock/data'
import { DATA_TYPES, MODBUS_REGISTER_TYPES } from '../mock/types'

type TargetMode = 'defined' | 'manual'
type DefinedKind = 'device' | 'sink' | 'point'

const targetMode = ref<TargetMode>('defined')
const definedKind = ref<DefinedKind>('device')
const selectedDeviceId = ref(store.devices[0]?.device_id || '')
const selectedSinkName = ref(store.sinks[0]?.name || '')
const selectedPointId = ref('')
const activeTool = ref<'network'|'protocol'|'data'|'write'>('network')
const running = ref(false)

const manualTarget = reactive({
  cidr: '192.168.151.0/24',
  host: '192.168.151.25',
  port: 48898,
  protocol: 'ads',
})

const selectedDevice = computed(() => store.devices.find(d => d.device_id === selectedDeviceId.value))
const selectedSink = computed(() => store.sinks.find(s => s.name === selectedSinkName.value))
const devicePoints = computed(() => selectedDevice.value ? pointsOfTable(tableOfDevice(selectedDevice.value)) : [])

watch(selectedDeviceId, () => {
  selectedPointId.value = devicePoints.value[0]?.point_id || ''
}, { immediate:true })

function sinkHost() {
  const s = selectedSink.value
  if (!s) return ''
  const raw = String(s.params.bootstrap_servers || s.params.dsn || s.params.path || '')
  if (raw.includes('://')) return raw.split('://')[1]?.split(/[/:]/)[0] || raw
  return raw.split(':')[0]
}
function sinkPort() {
  const s = selectedSink.value
  if (!s) return 0
  if (s.type === 'kafka') return Number(String(s.params.bootstrap_servers || 'localhost:9092').split(':').pop()) || 9092
  if (s.type === 'db') return 5432
  return 0
}

const resolvedTarget = computed(() => {
  if (targetMode.value === 'manual') {
    return { name:'Manual target', host:manualTarget.host, port:manualTarget.port, protocol:manualTarget.protocol }
  }
  if (definedKind.value === 'sink') {
    return { name:selectedSink.value?.name || 'Sink', host:sinkHost(), port:sinkPort(), protocol:selectedSink.value?.type || '' }
  }
  const d = selectedDevice.value
  const conn = d ? effectiveConnection(d) : {}
  return {
    name:d?.device_id || 'Device',
    host:d?.host || '',
    port:Number(conn.port || 0),
    protocol:d ? protocolOfDevice(d) : '',
  }
})

function sleep(ms:number) {
  return new Promise(resolve => setTimeout(resolve, ms))
}

const networkResults = ref<Array<Record<string,string|number>>>([])
const networkPage = ref(1)
const networkPageSize = ref(20)
const pagedNetworkResults = computed(() => {
  const start = (networkPage.value - 1) * networkPageSize.value
  return networkResults.value.slice(start, start + networkPageSize.value)
})

async function runHostDiscovery() {
  running.value = true
  await sleep(450)
  const prefix = manualTarget.cidr.split('/')[0].split('.').slice(0,3).join('.')
  networkResults.value = store.devices
    .filter(d => !prefix || d.host.startsWith(prefix + '.'))
    .map(d => ({
      IP:d.host,
      Reachable:d.online ? 'Yes' : 'No',
      Latency:d.online ? 8 + (Number(d.device_id.replace(/\D/g,'')) % 13) + ' ms' : '—',
      KnownObject:d.device_id,
      Protocol:protocolOfDevice(d).toUpperCase(),
    }))
  networkPage.value = 1
  running.value = false
}

async function runReachability() {
  running.value = true
  await sleep(300)
  networkResults.value = [{
    IP:resolvedTarget.value.host,
    Reachable:resolvedTarget.value.host ? 'Yes' : 'No',
    Latency:'8 ms',
    KnownObject:resolvedTarget.value.name,
    Protocol:resolvedTarget.value.protocol.toUpperCase(),
  }]
  running.value = false
}

const portProfiles = [
  { service:'ADS', port:48898 },
  { service:'Modbus TCP', port:502 },
  { service:'IEC 104', port:2404 },
  { service:'PostgreSQL', port:5432 },
  { service:'Redis', port:6379 },
  { service:'Kafka', port:9092 },
]
async function runPortProbe() {
  running.value = true
  await sleep(380)
  networkResults.value = portProfiles.map((p,index) => ({
    IP:resolvedTarget.value.host,
    Port:p.port,
    Service:p.service,
    State:p.port === resolvedTarget.value.port || index % 4 === 0 ? 'Open' : 'Closed',
    Latency:(6 + index * 3) + ' ms',
  }))
  running.value = false
}

const protocolResult = ref<Array<Record<string,string|number>>>([])
const protocolActions = computed(() => {
  const p = resolvedTarget.value.protocol
  if (p === 'ads') return ['Connect','Read State','Read Symbol','Read IG/IO']
  if (p === 'modbus') return ['Connect','Read Holding','Read Input','Read Coil','Read Discrete']
  if (p === 'iec104') return ['Connect','General Interrogation']
  return ['TCP Connect','Session / Auth','Target Check']
})
async function protocolAction(action:string) {
  running.value = true
  await sleep(350)
  protocolResult.value = [{
    Check:action,
    Target:resolvedTarget.value.host + ':' + resolvedTarget.value.port,
    Result:'Passed',
    Latency:'18 ms',
    Evidence:resolvedTarget.value.protocol.toUpperCase() + ' response received',
  }]
  running.value = false
}

const readMode = ref<'defined'|'manual'>('defined')
const manualRead = reactive({
  symbol:'',
  register_type:'holding',
  address:0,
  ioa:1,
  data_type:'float32',
})
const readResults = ref<Array<Record<string,string|number>>>([])

function pointAddress(p:any) {
  if (p?.address?.symbol) return p.address.symbol
  if (p?.address?.address !== undefined) return String(p.address.type || '') + ' ' + p.address.address
  if (p?.address?.ioa !== undefined) return 'IOA ' + p.address.ioa
  return '—'
}
async function readOnce() {
  running.value = true
  await sleep(320)
  const point = devicePoints.value.find(p => p.point_id === selectedPointId.value)
  const address = readMode.value === 'defined'
    ? pointAddress(point)
    : resolvedTarget.value.protocol === 'ads'
      ? manualRead.symbol
      : resolvedTarget.value.protocol === 'modbus'
        ? manualRead.register_type + ' ' + manualRead.address
        : 'IOA ' + manualRead.ioa

  readResults.value = [{
    Target:resolvedTarget.value.name,
    Address:address || '—',
    Type:readMode.value === 'defined' ? (point?.data_type || '—') : manualRead.data_type,
    Raw:'42 C8 00 00',
    Decoded:'100',
    Engineering:'100 ' + (point ? unitSymbol(point.unit) : ''),
    Latency:'14 ms',
    Timestamp:new Date().toLocaleTimeString(),
  }]
  running.value = false
}

const writeMode = ref<'defined'|'manual'>('defined')
const writeValue = ref('')
const writePointId = ref('')
const writablePoints = computed(() => devicePoints.value.filter(p => p.point_groups.includes('control')))
const writeResult = ref<Array<Record<string,string|number>>>([])

async function runWrite() {
  if (!writeValue.value.trim()) {
    ElMessage.warning('Enter a write value')
    return
  }
  const point = writeMode.value === 'defined'
    ? writablePoints.value.find(p => p.point_id === writePointId.value)
    : null

  await ElMessageBox.confirm(
    'Send one diagnostic write to ' + resolvedTarget.value.name + '? Production backend must audit this operation and perform readback.',
    'Diagnostic Write',
    { type:'warning', confirmButtonText:'Write Once' },
  )
  running.value = true
  await sleep(380)
  writeResult.value = [{
    Target:resolvedTarget.value.name,
    Point:point?.point_id || 'Manual address',
    Value:writeValue.value,
    Result:'Passed',
    Readback:writeValue.value,
    Latency:'21 ms',
  }]
  running.value = false
}
</script>

<template>
  <div class="diagnostics-page">
    <div class="head">
      <div>
        <h1>Diagnostics</h1>
        <p>工程探索工作台：自由选择已定义对象或手工目标，执行网络、协议、读写测试。</p>
      </div>
    </div>

    <div class="explorer-layout">
      <aside class="target-panel">
        <div class="panel-heading">
          <h2>Target</h2>
          <p>Target 是整个 Diagnostics 工作区共享的上下文。</p>
        </div>

        <el-segmented
          v-model="targetMode"
          :options="[{label:'Defined Object',value:'defined'},{label:'Manual Target',value:'manual'}]"
          class="full-segment"
        />

        <el-form v-if="targetMode==='defined'" label-position="top" class="target-form">
          <el-form-item label="Object Type">
            <el-radio-group v-model="definedKind">
              <el-radio-button value="device">Device</el-radio-button>
              <el-radio-button value="sink">Sink</el-radio-button>
              <el-radio-button value="point">Point</el-radio-button>
            </el-radio-group>
          </el-form-item>
          <el-form-item v-if="definedKind!=='sink'" label="Device">
            <el-select v-model="selectedDeviceId" filterable style="width:100%">
              <el-option
                v-for="d in store.devices"
                :key="d.device_id"
                :label="d.device_id + ' · ' + d.host"
                :value="d.device_id"
              />
            </el-select>
          </el-form-item>
          <el-form-item v-if="definedKind==='sink'" label="Sink">
            <el-select v-model="selectedSinkName" style="width:100%">
              <el-option v-for="s in store.sinks" :key="s.name" :label="s.name + ' · ' + s.type" :value="s.name" />
            </el-select>
          </el-form-item>
          <el-form-item v-if="definedKind==='point'" label="Point">
            <el-select v-model="selectedPointId" filterable style="width:100%">
              <el-option v-for="p in devicePoints" :key="p.point_id" :label="p.point_id" :value="p.point_id" />
            </el-select>
          </el-form-item>
        </el-form>

        <el-form v-else label-position="top" class="target-form">
          <el-form-item label="Subnet / CIDR"><el-input v-model="manualTarget.cidr" /></el-form-item>
          <el-form-item label="Host / IP"><el-input v-model="manualTarget.host" /></el-form-item>
          <div class="two-col">
            <el-form-item label="Port"><el-input-number v-model="manualTarget.port" :min="1" :max="65535" style="width:100%" /></el-form-item>
            <el-form-item label="Protocol">
              <el-select v-model="manualTarget.protocol">
                <el-option label="ADS" value="ads" />
                <el-option label="Modbus TCP" value="modbus" />
                <el-option label="IEC 104" value="iec104" />
                <el-option label="Other" value="other" />
              </el-select>
            </el-form-item>
          </div>
        </el-form>

        <div class="resolved-target">
          <span>Resolved Target</span>
          <b>{{ resolvedTarget.name }}</b>
          <code>{{ resolvedTarget.host }}<template v-if="resolvedTarget.port">:{{ resolvedTarget.port }}</template></code>
          <small>{{ resolvedTarget.protocol.toUpperCase() || 'UNSPECIFIED' }}</small>
        </div>
      </aside>

      <main class="workspace-panel">
        <el-tabs v-model="activeTool">
          <el-tab-pane label="Network" name="network">
            <div class="tool-heading">
              <div><h2>Network Explorer</h2><p>Host Discovery 支持 CIDR；Port Probe 使用固定协议端口组合。</p></div>
              <div class="tool-actions">
                <el-button :loading="running" @click="runReachability">Reachability</el-button>
                <el-button :loading="running" @click="runPortProbe">Port Probe</el-button>
                <el-button type="primary" :loading="running" @click="runHostDiscovery">Host Discovery</el-button>
              </div>
            </div>

            <el-table :data="pagedNetworkResults" empty-text="Run an exploration action">
              <el-table-column
                v-for="key in Object.keys(networkResults[0] || {})"
                :key="key"
                :prop="key"
                :label="key"
                min-width="120"
              />
            </el-table>
            <div v-if="networkResults.length>networkPageSize" class="result-pagination">
              <el-pagination
                v-model:current-page="networkPage"
                v-model:page-size="networkPageSize"
                :page-sizes="[20,50,100]"
                :total="networkResults.length"
                layout="total, sizes, prev, pager, next"
              />
            </div>
          </el-tab-pane>

          <el-tab-pane label="Protocol" name="protocol">
            <div class="tool-heading">
              <div><h2>Protocol Explorer</h2><p>只显示当前 Target 协议相关工具。</p></div>
            </div>
            <div class="tool-actions protocol-actions">
              <el-button v-for="action in protocolActions" :key="action" :loading="running" @click="protocolAction(action)">{{ action }}</el-button>
            </div>
            <el-table :data="protocolResult" empty-text="Choose a protocol action">
              <el-table-column prop="Check" label="Check" min-width="130" />
              <el-table-column prop="Target" label="Target" min-width="180" />
              <el-table-column prop="Result" label="Result" width="100" />
              <el-table-column prop="Latency" label="Latency" width="100" />
              <el-table-column prop="Evidence" label="Evidence" min-width="230" />
            </el-table>
          </el-tab-pane>

          <el-tab-pane label="Data" name="data">
            <div class="tool-heading">
              <div><h2>Data Explorer</h2><p>Defined Point 使用配置；Manual Address 仅用于探索，不写入 Point Table。</p></div>
            </div>
            <el-segmented v-model="readMode" :options="[{label:'Defined Point',value:'defined'},{label:'Manual Address',value:'manual'}]" />
            <el-form label-position="top" class="tool-form">
              <el-form-item v-if="readMode==='defined'" label="Point">
                <el-select v-model="selectedPointId" filterable style="width:100%">
                  <el-option v-for="p in devicePoints" :key="p.point_id" :label="p.point_id + ' · ' + pointAddress(p)" :value="p.point_id" />
                </el-select>
              </el-form-item>
              <template v-else>
                <el-form-item v-if="resolvedTarget.protocol==='ads'" label="Symbol"><el-input v-model="manualRead.symbol" /></el-form-item>
                <div v-else-if="resolvedTarget.protocol==='modbus'" class="two-col">
                  <el-form-item label="Register Type">
                    <el-select v-model="manualRead.register_type">
                      <el-option v-for="r in MODBUS_REGISTER_TYPES" :key="r" :label="r" :value="r" />
                    </el-select>
                  </el-form-item>
                  <el-form-item label="0-based Address"><el-input-number v-model="manualRead.address" :min="0" style="width:100%" /></el-form-item>
                </div>
                <el-form-item v-else label="IOA"><el-input-number v-model="manualRead.ioa" :min="0" /></el-form-item>
                <el-form-item label="Data Type">
                  <el-select v-model="manualRead.data_type">
                    <el-option v-for="t in DATA_TYPES" :key="t" :label="t" :value="t" />
                  </el-select>
                </el-form-item>
              </template>
              <el-button type="primary" :loading="running" @click="readOnce">Read Once</el-button>
            </el-form>
            <el-table :data="readResults" empty-text="Run a read">
              <el-table-column prop="Target" label="Target" min-width="120" />
              <el-table-column prop="Address" label="Address" min-width="180" />
              <el-table-column prop="Type" label="Type" width="100" />
              <el-table-column prop="Raw" label="Raw" min-width="110" />
              <el-table-column prop="Decoded" label="Decoded" width="100" />
              <el-table-column prop="Engineering" label="Engineering" min-width="130" />
              <el-table-column prop="Latency" label="Latency" width="90" />
              <el-table-column prop="Timestamp" label="Timestamp" width="110" />
            </el-table>
          </el-tab-pane>

          <el-tab-pane label="Write" name="write">
            <div class="tool-heading">
              <div><h2>Write Explorer</h2><p>单次执行、二次确认、生产后端审计并 readback。</p></div>
            </div>
            <el-alert type="warning" :closable="false" title="Write tests can change real equipment state." />
            <el-segmented v-model="writeMode" :options="[{label:'Defined Point',value:'defined'},{label:'Manual Address',value:'manual'}]" class="write-mode" />
            <el-form label-position="top" class="tool-form">
              <el-form-item v-if="writeMode==='defined'" label="Writable Point">
                <el-select v-model="writePointId" filterable style="width:100%">
                  <el-option v-for="p in writablePoints" :key="p.point_id" :label="p.point_id + ' · ' + pointAddress(p)" :value="p.point_id" />
                </el-select>
              </el-form-item>
              <el-form-item v-else label="Manual Address / Symbol"><el-input v-model="manualRead.symbol" /></el-form-item>
              <el-form-item label="Write Value"><el-input v-model="writeValue" /></el-form-item>
              <el-button type="danger" :loading="running" @click="runWrite">Write Once & Readback</el-button>
            </el-form>
            <el-table :data="writeResult" empty-text="No write executed">
              <el-table-column prop="Target" label="Target" />
              <el-table-column prop="Point" label="Point" />
              <el-table-column prop="Value" label="Write" />
              <el-table-column prop="Result" label="Result" />
              <el-table-column prop="Readback" label="Readback" />
              <el-table-column prop="Latency" label="Latency" />
            </el-table>
          </el-tab-pane>
        </el-tabs>
      </main>
    </div>
  </div>
</template>

<style scoped>
.explorer-layout{display:grid;grid-template-columns:300px minmax(0,1fr);gap:var(--app-space-4);align-items:start}.target-panel{padding:var(--app-space-4);border:1px solid var(--app-border-soft);border-radius:var(--app-panel-radius);background:var(--el-bg-color)}
.panel-heading h2,.tool-heading h2{margin:0;font-size:var(--app-font-section-title)}.panel-heading p,.tool-heading p{margin:4px 0 0;color:var(--app-text-muted);font-size:var(--app-font-caption);line-height:1.5}.full-segment{width:100%;margin-top:var(--app-space-4)}.target-form{margin-top:var(--app-space-4)}.target-form :deep(.el-form-item){margin-bottom:var(--app-space-3)}
.two-col{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:var(--app-space-3)}.resolved-target{display:grid;gap:5px;margin-top:var(--app-space-4);padding-top:var(--app-space-4);border-top:1px solid var(--app-border-soft)}.resolved-target span,.resolved-target small{color:var(--app-text-muted);font-size:var(--app-font-caption)}.resolved-target b{font-size:var(--app-font-panel-title)}.resolved-target code{overflow-wrap:anywhere;color:var(--app-text-secondary)}
.workspace-panel{min-width:0}.tool-heading{display:flex;align-items:flex-start;justify-content:space-between;gap:var(--app-space-4);margin:var(--app-space-3) 0 var(--app-space-4)}.tool-actions{display:flex;gap:var(--app-space-2);flex-wrap:wrap}.protocol-actions{margin-bottom:var(--app-space-4)}.result-pagination{display:flex;justify-content:flex-end;margin-top:var(--app-space-3)}.tool-form{max-width:720px;margin-top:var(--app-space-4)}.write-mode{margin-top:var(--app-space-4)}
@media(max-width:1199px){.explorer-layout{grid-template-columns:1fr}.target-panel{display:grid;grid-template-columns:minmax(220px,.5fr) minmax(0,1fr);gap:var(--app-space-4)}.panel-heading,.resolved-target{grid-column:1/-1}.full-segment{align-self:start}.target-form{margin-top:0}}
@media(max-width:767px){.target-panel{display:block}.full-segment,.target-form{margin-top:var(--app-space-3)}.tool-heading{flex-direction:column}.two-col{grid-template-columns:1fr}.result-pagination{justify-content:center;overflow-x:auto}}
</style>
