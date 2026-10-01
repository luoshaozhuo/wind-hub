import { reactive, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { api, jsonBody } from './client'
import type {
  DeviceGroupDef, DeviceInst, DeviceModelDef, DeviceTypeDef,
  DeviceVerification, PointDef, PointGroupDef, PointTableDef,
  SinkDef, TaskDef, UnitDef,
} from './types'

interface Page<T> { items:T[]; page:{page:number;page_size:number;total:number} }
interface DeviceDto {
  device_id:string; protocol:string; host:string; port:number; point_table:string;
  device_type?:string|null; model?:string|null; device_group?:string|null;
  enabled:boolean; connected:boolean; consecutive_failures:number; last_error?:string|null;
  extensions?:Record<string,unknown>
}
interface TaskDto {
  task_id:string; device?:string|null; device_group?:string|null; point_group:string;
  interval:number|null; targets:string[]; enabled:boolean; runtime_state:string;
}
interface SinkDto {
  name:string; type:'kafka'|'db'|'file'; enabled:boolean; params:Record<string,unknown>;
  healthy:boolean; message?:string|null; queue_depth:number
}
interface SettingsDto {
  site_id:string; site_name?:string|null; api_enabled:boolean; api_host:string; api_port:number;
  ads_local_ip?:string|null; ads_local_ams_net_id?:string|null; ads_username:string; ads_password:string
}
interface DefinitionsDto {
  units:Record<string,Record<string,unknown>>;
  device_types:Record<string,Record<string,unknown>>;
  device_models:Record<string,Record<string,unknown>>;
  point_tables:Record<string,Record<string,unknown>>;
  point_groups:string[]; device_groups:string[]
}
interface OverviewDto {
  runtime_running:boolean; site_id?:string|null; site_name?:string|null
}
interface ApplyResult { success:boolean; revision?:number|null; errors:string[]; rollback_performed:boolean }

export const DEFAULT_POINT_TABLE_ID = 'default'
export const DEFAULT_POINT_GROUP_ID = 'default'

interface SystemInfo {
  siteId:string; siteName:string; collectorVersion:string; adminVersion:string;
  runtimeStatus:string; configSet:string; timezone:string; logLevel:string;
  tempDirectory:string; dataDirectory:string; reloadPolicy:string;
  apiHost:string; apiPort:number; timeSync:string;
  ads:{local_ip:string;local_ams_net_id:string;username:string;password:string}
}
interface AdminStore {
  systemInfo:SystemInfo
  units:Record<string,UnitDef>
  deviceTypes:DeviceTypeDef[]
  deviceGroups:DeviceGroupDef[]
  deviceModels:DeviceModelDef[]
  pointTables:PointTableDef[]
  pointGroups:PointGroupDef[]
  points:Record<string,PointDef[]>
  devices:DeviceInst[]
  deviceVerification:Record<string,DeviceVerification>
  sinks:SinkDef[]
  tasks:TaskDef[]
}

function emptyVerify():DeviceVerification {
  return {state:'idle',network:'unknown',protocol:'unknown',points:'unknown',point_total:0,point_success:0,point_failed:0,verified_at:'',latency_ms:0,errors:[]}
}

export const store = reactive<AdminStore>({
  systemInfo:{
    siteId:'',siteName:'',collectorVersion:'server',adminVersion:'v0.3.0',
    runtimeStatus:'STARTING',configSet:'',timezone:'',logLevel:'INFO',
    tempDirectory:'',dataDirectory:'',reloadPolicy:'incremental',
    apiHost:'',apiPort:8080,timeSync:'',
    ads:{local_ip:'',local_ams_net_id:'',username:'Administrator',password:''},
  },
  units:{},
  deviceTypes:[],
  deviceGroups:[],
  deviceModels:[],
  pointTables:[],
  pointGroups:[],
  points:{},
  devices:[],
  deviceVerification:{},
  sinks:[],
  tasks:[],
})

let rawPointTables:Record<string,Record<string,unknown>>={}
let baselinePointProjection=''
let hydrated=false
let persistTimer:number|undefined
let persistInFlight=false
let persistAgain=false

function asString(value:unknown, fallback=''){return typeof value==='string'?value:fallback}
function asNumber(value:unknown, fallback=0){return typeof value==='number'?value:fallback}
function asObject(value:unknown){return value&&typeof value==='object'&&!Array.isArray(value)?value as Record<string,unknown>:{}}
function asArray(value:unknown){return Array.isArray(value)?value:[]}

function normalizePoint(raw:Record<string,unknown>):PointDef {
  return {
    point_id:asString(raw.point_id),
    variable_name:asString(raw.variable_name,asString(raw.point_id)),
    point_groups:asArray(raw.point_groups).map(String),
    address:{...asObject(raw.address)},
    data_type:asString(raw.data_type,'float32'),
    scale:asNumber(raw.scale,1),
    offset:asNumber(raw.offset,0),
    unit:asString(raw.unit,'none'),
    description:asString(raw.description),
  }
}

function hydrateDefinitions(defs:DefinitionsDto){
  store.units=Object.fromEntries(Object.entries(defs.units).map(([id,value])=>[
    id,{symbol:asString(value.symbol),name:asString(value.name,id)},
  ]))
  store.deviceTypes=Object.entries(defs.device_types).map(([id,value])=>({
    id,name:asString(value.name,id),
  }))
  store.deviceModels=Object.entries(defs.device_models).map(([id,value])=>({
    id,
    device_type:asString(value.device_type),
    manufacturer:asString(value.manufacturer),
    model:asString(value.model)||undefined,
    protocol:asString(value.protocol) as DeviceModelDef['protocol'],
    point_table:asString(value.point_table),
    read_mode:asString(value.read_mode),
    properties:{...asObject(value.properties)},
    connection_defaults:{...asObject(value.connection_defaults)},
  }))
  rawPointTables=structuredClone(defs.point_tables)
  const tables:PointTableDef[]=[{id:DEFAULT_POINT_TABLE_ID,protocol:'generic',extends:'',remove_points:[],system:true}]
  const points:Record<string,PointDef[]>={[DEFAULT_POINT_TABLE_ID]:[]}
  for(const [id,value] of Object.entries(defs.point_tables)){
    tables.push({
      id,
      protocol:(asString(value.protocol)||'generic') as PointTableDef['protocol'],
      extends:asString(value.extends),
      remove_points:asArray(value.remove_points).map(String),
    })
    points[id]=asArray(value.points).map(item=>normalizePoint(asObject(item)))
  }
  store.pointTables=tables
  store.points=points
  store.pointGroups=[
    {id:DEFAULT_POINT_GROUP_ID,name:'Default / Unassigned',system:true},
    ...defs.point_groups.filter(id=>id!==DEFAULT_POINT_GROUP_ID).map(id=>({id,name:id})),
  ]
}

function hydrateDevices(page:Page<DeviceDto>, defs:DefinitionsDto){
  store.devices=page.items.map(row=>({
    device_id:row.device_id,
    model:row.model||'',
    device_group:row.device_group||'',
    host:row.host,
    port:row.port,
    extensions:{...(row.extensions||{})},
    enabled:row.enabled,
    online:row.connected,
  }))
  const groupIds=[...new Set([...defs.device_groups,...store.devices.map(d=>d.device_group).filter(Boolean)])]
  store.deviceGroups=groupIds.map(id=>{
    const device=store.devices.find(d=>d.device_group===id)
    const model=device?store.deviceModels.find(m=>m.id===device.model):undefined
    return {id,device_type:model?.device_type||''}
  })
  const existing=store.deviceVerification
  store.deviceVerification={}
  for(const device of store.devices) store.deviceVerification[device.device_id]=existing[device.device_id]||emptyVerify()
}

function hydrateTasks(page:Page<TaskDto>){
  const now=''
  store.tasks=page.items.map(row=>({
    task_id:row.task_id,
    device:row.device||'',
    device_group:row.device_group||'',
    point_group:row.point_group,
    interval:row.interval,
    sinks:[...row.targets],
    enabled:row.enabled,
    runtime:row.runtime_state.toUpperCase(),
    valid:true,
    invalid_reason:'',
    created_at:now,
    updated_at:now,
  }))
}

function hydrateSinks(rows:SinkDto[]){
  const previous=Object.fromEntries(store.sinks.map(s=>[s.name,s]))
  store.sinks=rows.map(row=>{
    const old=previous[row.name]
    return {
      name:row.name,type:row.type,enabled:row.enabled,params:{...row.params},
      runtime_state:!row.enabled?'disabled':row.healthy?'healthy':'failed',
      last_test_at:old?.last_test_at||'',last_write_at:old?.last_write_at||'',
      latency_ms:old?.latency_ms||0,error:row.message||'',queue_depth:row.queue_depth,
      writes_total:old?.writes_total||0,failures_total:old?.failures_total||0,
      dropped_points:old?.dropped_points||0,
      verification:old?.verification||{state:'never',checked_at:'',passed:0,total:0,checks:[]},
    }
  })
}

export async function initializeData(){
  hydrated=false
  const [settings,defs,devices,tasks,sinks,overview]=await Promise.all([
    api<SettingsDto>('/settings'),
    api<DefinitionsDto>('/definitions'),
    api<Page<DeviceDto>>('/devices?page_size=200'),
    api<Page<TaskDto>>('/tasks?page_size=200'),
    api<SinkDto[]>('/sinks'),
    api<OverviewDto>('/overview'),
  ])
  store.systemInfo.siteId=settings.site_id
  store.systemInfo.siteName=settings.site_name||settings.site_id
  store.systemInfo.apiHost=settings.api_host
  store.systemInfo.apiPort=settings.api_port
  store.systemInfo.ads={
    local_ip:settings.ads_local_ip||'',
    local_ams_net_id:settings.ads_local_ams_net_id||'',
    username:settings.ads_username,
    password:settings.ads_password,
  }
  store.systemInfo.runtimeStatus=overview.runtime_running?'RUNNING':'STOPPED'
  hydrateDefinitions(defs)
  hydrateDevices(devices,defs)
  hydrateTasks(tasks)
  hydrateSinks(sinks)
  refreshTaskValidity()
  baselinePointProjection=pointProjection()
  hydrated=true
  installPersistence()
}

function pointProjection(){
  return JSON.stringify(store.pointTables.filter(t=>!t.system).map(t=>({
    ...t,points:store.points[t.id]||[],
  })))
}

function definitionsPayload(){
  const units=Object.fromEntries(Object.entries(store.units).map(([id,value])=>[id,{...value}]))
  const device_types=Object.fromEntries(store.deviceTypes.map(item=>[
    item.id,{name:item.name||null},
  ]))
  const device_models=Object.fromEntries(store.deviceModels.map(item=>{
    const value:Record<string,unknown>={
      device_type:item.device_type,
      manufacturer:item.manufacturer||null,
      model:item.model||null,
      protocol:item.protocol,
      point_table:item.point_table,
      properties:{...item.properties},
      connection_defaults:{...item.connection_defaults},
    }
    if(item.read_mode)value.read_mode=item.read_mode
    return [item.id,value]
  }))
  const currentProjection=pointProjection()
  let point_tables:Record<string,Record<string,unknown>>
  if(currentProjection===baselinePointProjection){
    point_tables=structuredClone(rawPointTables)
  }else{
    point_tables=Object.fromEntries(store.pointTables.filter(t=>!t.system).map(table=>{
      const value:Record<string,unknown>={
        remove_points:[...(table.remove_points||[])],
        points:(store.points[table.id]||[]).map(point=>({
          point_id:point.point_id,
          variable_name:point.variable_name||undefined,
          point_groups:[...point.point_groups],
          address:{...point.address},
          data_type:point.data_type,
          scale:point.scale,
          offset:point.offset,
          unit:point.unit,
          description:point.description||undefined,
        })),
      }
      if(table.protocol!=='generic')value.protocol=table.protocol
      if(table.extends)value.extends=table.extends
      return [table.id,value]
    }))
  }
  return {units,device_types,device_models,point_tables}
}

function adminStatePayload(){
  return {
    devices:store.devices.map(d=>({
      device_id:d.device_id,model:d.model,device_group:d.device_group||null,
      host:d.host,port:d.port??null,extensions:{...(d.extensions||{})},enabled:d.enabled,
    })),
    tasks:store.tasks.map(t=>({
      task_id:t.task_id,device:t.device||null,device_group:t.device_group||null,
      point_group:t.point_group,interval:t.interval,sinks:[...t.sinks],enabled:t.enabled,
    })),
    sinks:store.sinks.map(s=>({
      name:s.name,type:s.type,enabled:s.enabled,params:{...s.params},
    })),
    definitions:definitionsPayload(),
  }
}

function configProjection(){
  return JSON.stringify(adminStatePayload())
}

let lastProjection=''
function installPersistence(){
  if(lastProjection)return
  lastProjection=configProjection()
  watch(configProjection,projection=>{
    if(!hydrated||projection===lastProjection)return
    lastProjection=projection
    if(persistTimer)window.clearTimeout(persistTimer)
    persistTimer=window.setTimeout(()=>void persistStructuredState(),350)
  })
}

async function persistStructuredState(){
  if(!hydrated)return
  if(persistInFlight){persistAgain=true;return}
  persistInFlight=true
  try{
    const result=await api<ApplyResult>('/admin-state',{
      method:'PUT',body:jsonBody(adminStatePayload()),
    })
    if(!result.success)throw new Error(result.errors.join('; ')||'Configuration apply failed')
    if(pointProjection()!==baselinePointProjection){
      rawPointTables=structuredClone(definitionsPayload().point_tables)
      baselinePointProjection=pointProjection()
    }
  }catch(error){
    ElMessage.error(error instanceof Error?error.message:'Configuration synchronization failed')
    try{await initializeData()}catch{/* keep current UI for diagnosis */}
  }finally{
    persistInFlight=false
    if(persistAgain){persistAgain=false;void persistStructuredState()}
  }
}

export async function refreshRuntimeState(){
  const [devices,tasks,sinks,defs,overview]=await Promise.all([
    api<Page<DeviceDto>>('/devices?page_size=200'),
    api<Page<TaskDto>>('/tasks?page_size=200'),
    api<SinkDto[]>('/sinks'),
    api<DefinitionsDto>('/definitions'),
    api<OverviewDto>('/overview'),
  ])
  hydrated=false
  hydrateDevices(devices,defs)
  hydrateTasks(tasks)
  hydrateSinks(sinks)
  store.systemInfo.runtimeStatus=overview.runtime_running?'RUNNING':'STOPPED'
  refreshTaskValidity()
  lastProjection=configProjection()
  hydrated=true
}

export function modelOf(d: DeviceInst): DeviceModelDef | undefined {
  return store.deviceModels.find(m => m.id === d.model)
}

export function protocolOfDevice(d: DeviceInst): string {
  return modelOf(d)?.protocol || ''
}

export function tableOfDevice(d: DeviceInst): string {
  return modelOf(d)?.point_table || ''
}

function clonePoint(p: PointDef): PointDef {
  return { ...p, address: { ...p.address }, point_groups: [...p.point_groups] }
}

export function pointsOfTable(tableId: string, stack: string[] = []): PointDef[] {
  if (stack.includes(tableId)) return []
  const table = store.pointTables.find(t => t.id === tableId)
  if (!table) return []

  const resolved = new Map<string, PointDef>()
  if (table.extends) {
    for (const p of pointsOfTable(table.extends, [...stack, tableId])) {
      resolved.set(p.point_id, clonePoint(p))
    }
  }
  for (const id of table.remove_points || []) resolved.delete(id)
  for (const p of store.points[tableId] || []) resolved.set(p.point_id, clonePoint(p))
  return [...resolved.values()]
}

export function pointOrigin(tableId: string, pointId: string): PointOrigin {
  const table = store.pointTables.find(t => t.id === tableId)
  const local = (store.points[tableId] || []).some(p => p.point_id === pointId)
  const inherited = !!table?.extends && pointsOfTable(table.extends).some(p => p.point_id === pointId)
  if (local && inherited) return 'override'
  if (local) return 'local'
  return 'inherited'
}

export function descendantTableIds(tableId: string): string[] {
  const result: string[] = []
  const visit = (id: string) => {
    for (const child of store.pointTables.filter(t => t.extends === id)) {
      if (!result.includes(child.id)) {
        result.push(child.id)
        visit(child.id)
      }
    }
  }
  visit(tableId)
  return result
}

export function affectedByPointTables(tableIds: string[]) {
  const tables = [...new Set(tableIds)]
  const modelIds = store.deviceModels.filter(m => tables.includes(m.point_table)).map(m => m.id)
  const devices = store.devices.filter(d => modelIds.includes(d.model))
  const deviceIds = new Set(devices.map(d => d.device_id))
  const groups = new Set(devices.map(d => d.device_group))
  const tasks = store.tasks.filter(t =>
    (t.device && deviceIds.has(t.device)) ||
    (t.device_group && groups.has(t.device_group))
  )
  return { tables, modelIds, devices, tasks }
}

export function tableProtocol(tableId: string): string {
  return store.pointTables.find(t => t.id === tableId)?.protocol || ''
}

export function unitSymbol(unitId: string): string {
  return store.units[unitId]?.symbol ?? unitId
}

export function addressText(protocol: string, p: PointDef): string {
  const a = p.address
  if (protocol === 'ads') {
    if (a.symbol && a.index_group) return `${a.symbol} (${a.index_group} / ${a.index_offset})`
    if (a.symbol) return a.symbol
    return `${a.index_group ?? ''} / ${a.index_offset ?? ''}`
  }
  if (protocol === 'modbus') return `${a.type ?? ''} ${a.address ?? ''}`
  return `ioa ${a.ioa ?? ''}${a.type ? ` · ${a.type}` : ''}`
}

export function validateAddress(protocol: string, a: PointAddress): string {
  if (protocol === 'ads') {
    const hasSymbol = !!(a.symbol && a.symbol.trim())
    const hasGroup = !!(a.index_group && a.index_group.trim())
    const hasOffset = !!(a.index_offset && a.index_offset.trim())
    if (hasGroup !== hasOffset) return "'index_group' and 'index_offset' must be configured together"
    if (!hasSymbol && !hasGroup) return "ADS point must define 'symbol' or 'index_group' + 'index_offset'"
    return ''
  }
  if (protocol === 'modbus') {
    if (!a.type) return 'Modbus point must define register type (holding/input/coil/discrete_input)'
    if (a.address === undefined || a.address === null || a.address < 0) return 'Modbus point must define a 0-based address'
    return ''
  }
  if (a.ioa === undefined || a.ioa === null || a.ioa < 0 || a.ioa > 0xFFFFFF) {
    return 'IEC104 ioa must be an integer in [0, 0xFFFFFF]'
  }
  return ''
}


export const DEVICE_IDENTITY_EXTENSION_KEYS = new Set(['target_net_id'])

export function effectiveConnection(d: DeviceInst): Record<string, unknown> {
  const model = modelOf(d)
  return {
    ...(model?.connection_defaults || {}),
    ...(d.extensions || {}),
    port: d.port ?? model?.connection_defaults?.port,
  }
}

export function deviceConnectionOverrides(d: DeviceInst): Record<string, unknown> {
  const model = modelOf(d)
  const defaults = model?.connection_defaults || {}
  const result: Record<string, unknown> = {}
  for (const [key, value] of Object.entries(d.extensions || {})) {
    if (DEVICE_IDENTITY_EXTENSION_KEYS.has(key) || defaults[key] !== value) result[key] = value
  }
  return result
}

export function resetDeviceConnectionOverrides(d: DeviceInst): number {
  const removable = Object.keys(d.extensions || {}).filter(key => !DEVICE_IDENTITY_EXTENSION_KEYS.has(key))
  d.extensions = Object.fromEntries(
    Object.entries(d.extensions || {}).filter(([key]) => DEVICE_IDENTITY_EXTENSION_KEYS.has(key)),
  )
  const hadPortOverride = d.port !== undefined
  d.port = undefined
  return removable.length + (hadPortOverride ? 1 : 0)
}

export function defaultPointTableFor(_protocol: string): string {
  return DEFAULT_POINT_TABLE_ID
}

export function isDefaultPointTable(tableId: string): boolean {
  return tableId === DEFAULT_POINT_TABLE_ID
}

export function isDefaultPointGroup(groupId: string): boolean {
  return groupId === DEFAULT_POINT_GROUP_ID
}

export function devicesForTask(t: TaskDef): DeviceInst[] {
  if (t.device) return store.devices.filter(d => d.device_id === t.device && d.enabled)
  if (t.device_group) return store.devices.filter(d => d.device_group === t.device_group && d.enabled)
  return []
}

export function taskInvalidReason(t: TaskDef): string {
  if (!t.sinks.length) return 'Task has no Sink target'
  for (const sinkName of t.sinks) {
    const sink = store.sinks.find(s => s.name === sinkName)
    if (!sink) return `Sink '${sinkName}' does not exist`
    if (!sink.enabled) return `Sink '${sinkName}' is disabled`
  }
  if (!store.pointGroups.some(g => g.id === t.point_group)) return 'Point Group does not exist'
  if (isDefaultPointGroup(t.point_group)) return 'Default Point Group is a placeholder and cannot be collected'

  const targets = devicesForTask(t)
  if (!targets.length) return 'Task target resolves to no devices'

  for (const d of targets) {
    const model = modelOf(d)
    if (!model) return `Device ${d.device_id} has no valid model`
    if (isDefaultPointTable(model.point_table)) return `Device ${d.device_id} is assigned to a default Point Table`
    if (!store.pointTables.some(pt => pt.id === model.point_table)) return `Device ${d.device_id} references a missing Point Table`
    if (!pointsOfTable(model.point_table).some(p => p.point_groups.includes(t.point_group))) {
      return `Device ${d.device_id} Point Table has no points in group '${t.point_group}'`
    }
  }
  return ''
}

export function refreshTaskValidity(): void {
  for (const t of store.tasks) {
    const reason = taskInvalidReason(t)
    t.valid = !reason
    t.invalid_reason = reason
    if (reason && t.runtime === 'RUNNING') t.runtime = 'STOPPED'
  }
}

