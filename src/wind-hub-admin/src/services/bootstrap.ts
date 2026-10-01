import { store, refreshTaskValidity } from '../mock/data'
import { emptyVerification } from './backend'
import {
  loadDefinitions,
  loadDevices,
  loadSettings,
  loadSinks,
  loadTasks,
} from './backend'

export async function hydrateAdminStore():Promise<void>{
  const [devices,tasks,sinks,definitions,settings]=await Promise.all([
    loadDevices(),
    loadTasks(),
    loadSinks(),
    loadDefinitions(),
    loadSettings(),
  ])

  store.devices.splice(0,store.devices.length,...devices.map((row:any)=>({
    device_id:row.device_id,
    model:row.model||'',
    device_group:row.device_group||'',
    host:row.host,
    port:row.port,
    extensions:row.extensions||{},
    enabled:row.enabled,
    online:row.connected,
  })))

  for(const key of Object.keys(store.deviceVerification))delete store.deviceVerification[key]
  for(const row of store.devices)store.deviceVerification[row.device_id]=emptyVerification()

  store.tasks.splice(0,store.tasks.length,...tasks.map((row:any)=>({
    task_id:row.task_id,
    device:row.device||'',
    device_group:row.device_group||'',
    point_group:row.point_group,
    interval:row.interval,
    sinks:row.targets||[],
    enabled:row.enabled,
    runtime:String(row.runtime_state||'stopped').toUpperCase(),
    valid:true,
    invalid_reason:'',
    created_at:'',
    updated_at:'',
  })))

  store.sinks.splice(0,store.sinks.length,...sinks)

  store.deviceTypes.splice(
    0,
    store.deviceTypes.length,
    ...Object.entries(definitions.device_types||{}).map(([id,value]:any)=>({
      id,
      name:value.name||id,
    })),
  )

  store.deviceModels.splice(
    0,
    store.deviceModels.length,
    ...Object.entries(definitions.device_models||{}).map(([id,value]:any)=>({
      id,
      device_type:value.device_type,
      manufacturer:value.manufacturer||'',
      model:value.model||'',
      protocol:value.protocol,
      point_table:value.point_table,
      read_mode:value.read_mode||'',
      properties:value.properties||{},
      connection_defaults:value.connection_defaults||{},
    })),
  )

  const groupTypes=new Map<string,string>()
  for(const device of store.devices){
    const model=store.deviceModels.find(item=>item.id===device.model)
    if(device.device_group&&!groupTypes.has(device.device_group)){
      groupTypes.set(device.device_group,model?.device_type||'')
    }
  }
  store.deviceGroups.splice(
    0,
    store.deviceGroups.length,
    ...Array.from(groupTypes.entries()).map(([id,device_type])=>({
      id,
      device_type,
    })),
  )

  for(const key of Object.keys(store.units))delete store.units[key]
  for(const [id,value] of Object.entries(definitions.units||{}) as Array<[string,any]>){
    store.units[id]={symbol:value.symbol||'',name:value.name||id}
  }

  store.pointTables.splice(
    0,
    store.pointTables.length,
    ...Object.entries(definitions.point_tables||{}).map(([id,value]:any)=>({
      id,
      protocol:value.protocol||'generic',
      extends:'',
      remove_points:[],
    })),
  )
  for(const key of Object.keys(store.points))delete store.points[key]
  for(const [id,value] of Object.entries(definitions.point_tables||{}) as Array<[string,any]>){
    store.points[id]=(value.points||[]).map((point:any)=>({
      point_id:point.point_id,
      variable_name:point.variable_name||'',
      point_groups:point.point_groups||[],
      address:point.address||{},
      data_type:point.data_type,
      scale:point.scale,
      offset:point.offset,
      unit:point.unit,
      description:point.description||'',
    }))
  }

  store.pointGroups.splice(
    0,
    store.pointGroups.length,
    ...(definitions.point_groups||[]).map((id:string)=>({
      id,
      name:id,
    })),
  )

  store.systemInfo.siteId=settings.site_id||store.systemInfo.siteId
  store.systemInfo.siteName=settings.site_name||store.systemInfo.siteName
  store.systemInfo.apiHost=settings.api?.host||store.systemInfo.apiHost
  store.systemInfo.apiPort=settings.api?.port||store.systemInfo.apiPort
  store.systemInfo.ads.local_ip=settings.ads?.local_ip||store.systemInfo.ads.local_ip
  store.systemInfo.ads.local_ams_net_id=
    settings.ads?.local_ams_net_id||store.systemInfo.ads.local_ams_net_id
  store.systemInfo.ads.username=settings.ads?.username||store.systemInfo.ads.username
  store.systemInfo.ads.password=''

  refreshTaskValidity()
}
