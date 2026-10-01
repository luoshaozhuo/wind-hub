import { api, apiBlob, jsonBody } from './client'
import {
  pointsOfTable, protocolOfDevice, refreshRuntimeState, refreshTaskValidity, store,
  tableOfDevice, unitSymbol,
} from './data'
import type {
  DeviceInst, DeviceVerification, PointDef, SinkDef, SinkVerificationCheck, TaskDef,
} from './types'
import { deviceDataTick, loadLogs } from './runtime'

export const LATENCY={uiLocal:0,verifyStep:0,command:0,taskTransition:0,qualityCheck:0,diagnostic:0,configApply:0,scanBatch:0}
export function sleep(ms:number){return new Promise<void>(resolve=>window.setTimeout(resolve,ms))}

interface Operation {
  operation_id:string; state:string; total:number; completed:number; progress:number;
  result?:Record<string,unknown>|null; error?:{code:string;message:string}|null
}
interface ApplyResult {success:boolean;revision?:number|null;errors:string[];rollback_performed:boolean}
interface DataPage {
  items:Array<{point_id:string;value:unknown;quality:string|null;timestamp:string|null}>
  page:{total:number}
}
interface CommandResponse {
  requested:unknown;success:boolean;error:string|null;sent_at:string;latency_ms:number;
  readback:unknown;readback_error:string|null
}
interface SinkTestResponse {
  success:boolean;latency_ms:number;message:string|null;
  steps:Array<Record<string,unknown>>
}

export function emptyVerification():DeviceVerification {
  return {state:'idle',network:'unknown',protocol:'unknown',points:'unknown',point_total:0,point_success:0,point_failed:0,verified_at:'',latency_ms:0,errors:[]}
}

async function pollOperation(id:string):Promise<Operation>{
  for(let attempt=0;attempt<300;attempt++){
    const row=await api<Operation>('/operations/'+encodeURIComponent(id))
    if(['success','partial','failed','cancelled'].includes(row.state))return row
    await sleep(100)
  }
  throw new Error('Operation timed out')
}

export async function verifyDevice(d:DeviceInst):Promise<DeviceVerification>{
  const target=store.deviceVerification[d.device_id]||(store.deviceVerification[d.device_id]=emptyVerification())
  Object.assign(target,{state:'running',network:'checking',protocol:'checking',points:'checking',errors:[]})
  const started=performance.now()
  try{
    const ping=await api<{reachable:boolean}>('/diagnostics/ping',{method:'POST',body:jsonBody({host:d.host,timeout:1})})
    target.network=ping.reachable?'success':'failed'
    if(!ping.reachable){
      target.protocol='failed';target.points='failed';target.state='failed'
      target.errors=[{stage:'network',target:d.host,message:'Host unreachable'}]
      return target
    }
    const check=await api<{connected:boolean}>('/diagnostics/protocol/check',{method:'POST',body:jsonBody({device_id:d.device_id})})
    target.protocol=check.connected?'success':'failed'
    if(!check.connected){
      target.points='failed';target.state='failed'
      target.errors=[{stage:'protocol',target:d.device_id,message:'Protocol connection unavailable'}]
      return target
    }
    const op=await api<Operation>('/diagnostics/point-table',{method:'POST',body:jsonBody({device_id:d.device_id})})
    const done=await pollOperation(op.operation_id)
    const rows=Array.isArray(done.result?.points)?done.result!.points as Array<Record<string,unknown>>:[]
    target.point_total=rows.length
    target.point_failed=rows.filter(row=>row.success===false).length
    target.point_success=target.point_total-target.point_failed
    target.points=done.state==='success'?'success':done.state==='partial'?'partial':'failed'
    target.state=done.state==='success'?'success':done.state==='partial'?'warning':'failed'
    target.errors=rows.filter(row=>row.success===false).map(row=>({
      stage:'points' as const,target:String(row.point_id||d.device_id),message:String(row.error||'Read failed'),
    }))
    return target
  }catch(error){
    target.state='failed'
    target.network=target.network==='checking'?'failed':target.network
    target.protocol=target.protocol==='checking'?'failed':target.protocol
    target.points=target.points==='checking'?'failed':target.points
    target.errors.push({stage:'protocol',target:d.device_id,message:error instanceof Error?error.message:String(error)})
    return target
  }finally{
    target.verified_at=new Date().toISOString().replace('T',' ').slice(0,19)
    target.latency_ms=Math.round(performance.now()-started)
  }
}

export async function verifyAllDevices(devices:DeviceInst[]){
  let failed=0,warning=0
  for(const device of devices){
    const result=await verifyDevice(device)
    if(result.state==='failed')failed++
    if(result.state==='warning')warning++
  }
  return {failed,warning}
}

const valueCache=new Map<string,{value:number|boolean|string;quality:string|null;timestamp:string|null}>()
const trendCache=new Map<string,Array<[Date,number]>>()
function pointKey(deviceId:string,pointId:string){return deviceId+'::'+pointId}

export async function refreshDeviceData(deviceId:string){
  const page=await api<DataPage>('/devices/'+encodeURIComponent(deviceId)+'/data?page=1&page_size=200')
  for(const row of page.items){
    valueCache.set(pointKey(deviceId,row.point_id),{
      value:(row.value??'—') as number|boolean|string,quality:row.quality,timestamp:row.timestamp,
    })
  }
  deviceDataTick[deviceId]=(deviceDataTick[deviceId]||0)+1
}

export interface PointValue {value:number|boolean|string;read_state:'success'|'failed';error:string}
export function readPointValue(d:DeviceInst,_index:number,pointId:string,_dataType:string):PointValue{
  const cached=valueCache.get(pointKey(d.device_id,pointId))
  if(!cached)return {value:'—',read_state:'failed',error:'No sample'}
  if(cached.quality==='bad')return {value:cached.value,read_state:'failed',error:'BAD quality'}
  return {value:cached.value,read_state:'success',error:''}
}

export async function loadTrendSeries(d:DeviceInst,pointIds:string[],durationMs:number){
  if(!pointIds.length)return
  const query=pointIds.map(id=>'point_id='+encodeURIComponent(id)).join('&')
  const seconds=Math.max(1,Math.ceil(durationMs/1000))
  const rows=await api<Array<{point_id:string;samples:Array<{timestamp:string;value:unknown;quality:string}>}>>(
    '/devices/'+encodeURIComponent(d.device_id)+'/trend?'+query+'&window_seconds='+seconds+'&limit_per_point=3600',
  )
  for(const row of rows){
    trendCache.set(pointKey(d.device_id,row.point_id),row.samples
      .filter(sample=>typeof sample.value==='number')
      .map(sample=>[new Date(sample.timestamp),Number(sample.value)]))
  }
}

export function pointTrendSeries(d:DeviceInst,pointId:string,_index:number,_durationMs:number):Array<[Date,number]>{
  return trendCache.get(pointKey(d.device_id,pointId))||[]
}

export interface PointReadResult {
  state:'success'|'failed';timestamp:string;latency_ms:number;raw_data:string;
  decoded_value:string;engineering_value:string;error_category:string;error_code:string;error_message:string
}
export async function readDevicePoint(d:DeviceInst,point:PointDef,_index:number):Promise<PointReadResult>{
  const started=performance.now()
  try{
    const row=await api<{value:unknown;quality:string;timestamp:string}>('/diagnostics/protocol/read',{
      method:'POST',body:jsonBody({device_id:d.device_id,point_id:point.point_id}),
    })
    const value=String(row.value??'—')
    return {state:row.quality==='bad'?'failed':'success',timestamp:row.timestamp,latency_ms:Math.round(performance.now()-started),
      raw_data:'Not exposed by protocol adapter',decoded_value:value,engineering_value:value,
      error_category:row.quality==='bad'?'quality':'',error_code:row.quality==='bad'?'BAD_QUALITY':'',error_message:''}
  }catch(error){
    return {state:'failed',timestamp:new Date().toISOString(),latency_ms:Math.round(performance.now()-started),
      raw_data:'—',decoded_value:'—',engineering_value:'—',error_category:'protocol',error_code:'READ_FAILED',
      error_message:error instanceof Error?error.message:String(error)}
  }
}

export interface CommandOutcome {
  requested:number|boolean;readback:string|number|boolean;sentAt:string;latency:number;
  success:boolean;error:string;error_code:string
}
export async function sendDeviceCommand(d:DeviceInst,point:PointDef,_pointIndex:number,target:number|boolean):Promise<CommandOutcome>{
  const row=await api<CommandResponse>('/devices/'+encodeURIComponent(d.device_id)+'/commands',{
    method:'POST',body:jsonBody({point_id:point.point_id,value:target}),
  })
  await refreshDeviceData(d.device_id)
  await loadLogs()
  return {requested:target,readback:(row.readback??'—') as string|number|boolean,sentAt:row.sent_at,
    latency:Math.round(row.latency_ms),success:row.success,error:row.error||row.readback_error||'',error_code:row.success?'':'COMMAND_FAILED'}
}

export type TaskStartResult={ok:true}|{ok:false;error:{code:string;message:string}}
export async function startTask(t:TaskDef):Promise<TaskStartResult>{
  t.runtime='STARTING'
  try{
    const row=await api<{runtime_state:string}>('/tasks/'+encodeURIComponent(t.task_id)+'/start',{method:'POST'})
    t.runtime=row.runtime_state.toUpperCase()
    await loadLogs()
    return {ok:true}
  }catch(error){
    t.runtime='STOPPED'
    return {ok:false,error:{code:'TASK_START_FAILED',message:error instanceof Error?error.message:String(error)}}
  }
}
export async function stopTask(t:TaskDef):Promise<void>{
  t.runtime='STOPPING'
  try{
    const row=await api<{runtime_state:string}>('/tasks/'+encodeURIComponent(t.task_id)+'/stop',{method:'POST'})
    t.runtime=row.runtime_state.toUpperCase()
  }finally{await loadLogs()}
}
export function taskInstanceState(t:TaskDef,d:DeviceInst):'RUNNING'|'WARNING'|'FAILED'|'STOPPED'{
  if(t.runtime!=='RUNNING')return'STOPPED'
  if(!d.online)return'FAILED'
  const verify=store.deviceVerification[d.device_id]
  if(verify?.state==='warning')return'WARNING'
  if(verify?.state==='failed')return'FAILED'
  return'RUNNING'
}

function sinkSteps(result:SinkTestResponse):SinkVerificationCheck[]{
  return result.steps.map((step,index)=>({
    layer:(String(step.stage||'target')==='open'?'network':'target') as SinkVerificationCheck['layer'],
    name:String(step.stage||('step '+(index+1))),
    state:(step.success===false?'failed':'passed') as SinkVerificationCheck['state'],
    target:'backend',latency_ms:0,detail:String(step.message||''),error_code:step.success===false?'SINK_TEST_FAILED':'',
  }))
}
export async function verifySink(s:SinkDef):Promise<void>{
  const row=await api<SinkTestResponse>('/sinks/'+encodeURIComponent(s.name)+'/verify',{method:'POST'})
  s.last_test_at=new Date().toISOString().replace('T',' ').slice(0,19)
  s.latency_ms=Math.round(row.latency_ms)
  s.error=row.message||''
  s.verification={state:row.success?'passed':'failed',checked_at:s.last_test_at,
    passed:row.steps.filter(x=>x.success!==false).length,total:row.steps.length,checks:sinkSteps(row)}
  s.runtime_state=!s.enabled?'disabled':row.success?'healthy':'failed'
}
export interface WriteTestOutcome {ok:boolean;title:string;detail:string;latency:number}
export async function writeTestSink(s:SinkDef):Promise<WriteTestOutcome>{
  const row=await api<SinkTestResponse>('/sinks/'+encodeURIComponent(s.name)+'/write-test',{method:'POST'})
  if(row.success){s.last_write_at=new Date().toISOString().replace('T',' ').slice(0,19);s.writes_total++}
  else{s.failures_total++}
  s.latency_ms=Math.round(row.latency_ms);s.error=row.message||'';s.runtime_state=row.success?'healthy':'failed'
  await loadLogs()
  return {ok:row.success,title:row.success?'Write test passed':'Write test failed',detail:row.message||'Backend test completed',latency:s.latency_ms}
}

export async function pingHost(host:string){
  const row=await api<{host:string;reachable:boolean;latency_ms:number}>('/diagnostics/ping',{method:'POST',body:jsonBody({host,timeout:1})})
  const known=store.devices.find(d=>d.host===host)
  return {IP:host,Reachable:row.reachable?'Yes':'No',RTT:row.reachable?Math.round(row.latency_ms)+' ms':'—',Loss:row.reachable?'0%':'100%',Object:known?.device_id||'—'}
}
export async function probePorts(host:string,ports:number[]){
  const rows=await api<Array<{port:number;state:string;latency_ms:number}>>('/diagnostics/ports',{method:'POST',body:jsonBody({host,ports,timeout:1})})
  return rows.map(row=>({IP:host,Port:row.port,Service:String(row.port),State:row.state.charAt(0).toUpperCase()+row.state.slice(1),Latency:Math.round(row.latency_ms)+' ms'}))
}
export async function scanSubnet(network:string,ports:number[],onProgress?:(completed:number,total:number)=>void){
  const op=await api<Operation>('/diagnostics/subnet-scan',{method:'POST',body:jsonBody({network,ports,timeout:.5})})
  for(;;){
    const row=await api<Operation>('/operations/'+encodeURIComponent(op.operation_id))
    onProgress?.(row.completed,row.total)
    if(['success','partial','failed','cancelled'].includes(row.state)){
      if(row.state==='failed')throw new Error(row.error?.message||'Subnet scan failed')
      const hosts=Array.isArray(row.result?.hosts)?row.result!.hosts as Array<Record<string,unknown>>:[]
      return hosts.map(host=>({IP:String(host.ip||''),Ping:host.reachable?'Yes':'No',
        ADS:Array.isArray(host.open_ports)&&(host.open_ports as unknown[]).includes(48898)?'Open':'—',
        Modbus:Array.isArray(host.open_ports)&&(host.open_ports as unknown[]).includes(502)?'Open':'—',
        IEC104:Array.isArray(host.open_ports)&&(host.open_ports as unknown[]).includes(2404)?'Open':'—',
        Object:store.devices.find(d=>d.host===String(host.ip||''))?.device_id||'—'}))
    }
    await sleep(100)
  }
}
export function subnetHostResult(ip:string,_index:number){return {IP:ip,Ping:'—',ADS:'—',Modbus:'—',IEC104:'—',Object:'—'}}

export interface DiagnosticReadRow {[key:string]:string;Target:string;Host:string;Point:string;Address:string;Raw:string;Value:string;Unit:string;Result:string;Error:string;Latency:string}
export async function runProtocolRead(devices:DeviceInst[],point:PointDef,addressText:string):Promise<DiagnosticReadRow[]>{
  return await Promise.all(devices.map(async d=>{
    const started=performance.now()
    try{
      const row=await api<{value:unknown;quality:string}>('/diagnostics/protocol/read',{method:'POST',body:jsonBody({device_id:d.device_id,point_id:point.point_id})})
      return {Target:d.device_id,Host:d.host,Point:point.point_id,Address:addressText,Raw:'—',Value:String(row.value??'—'),Unit:unitSymbol(point.unit),Result:row.quality==='bad'?'Failed':'Success',Error:row.quality==='bad'?'BAD quality':'—',Latency:Math.round(performance.now()-started)+' ms'}
    }catch(error){
      return {Target:d.device_id,Host:d.host,Point:point.point_id,Address:addressText,Raw:'—',Value:'—',Unit:unitSymbol(point.unit),Result:'Failed',Error:error instanceof Error?error.message:String(error),Latency:Math.round(performance.now()-started)+' ms'}
    }
  }))
}
export async function runProtocolWrite(d:DeviceInst,point:PointDef,valueText:string,addressText:string):Promise<Record<string,string>>{
  const parsed=valueText==='true'?true:valueText==='false'?false:Number(valueText)
  const started=performance.now()
  try{
    const row=await api<CommandResponse>('/diagnostics/protocol/write',{method:'POST',body:jsonBody({device_id:d.device_id,point_id:point.point_id,value:parsed})})
    return {Target:d.device_id,Address:addressText,Write:valueText,Result:row.success?'Success':'Failed',Readback:String(row.readback??'—'),Error:row.error||row.readback_error||'—',Latency:Math.round(performance.now()-started)+' ms'}
  }catch(error){
    return {Target:d.device_id,Address:addressText,Write:valueText,Result:'Failed',Readback:'—',Error:error instanceof Error?error.message:String(error),Latency:Math.round(performance.now()-started)+' ms'}
  }
}
export async function manualProtocolReadRow(host:string,addressText:string,_dataType:string):Promise<DiagnosticReadRow>{
  const device=store.devices.find(d=>d.host===host)
  if(!device)return {Target:host,Host:host,Point:'—',Address:addressText,Raw:'—',Value:'—',Unit:'—',Result:'Failed',Error:'Manual raw read requires a configured device',Latency:'—'}
  const point=pointsOfTable(tableOfDevice(device)).find(p=>p.address.symbol===addressText||p.point_id===addressText)
  if(!point)return {Target:host,Host:host,Point:'—',Address:addressText,Raw:'—',Value:'—',Unit:'—',Result:'Failed',Error:'Address is not mapped to a configured point',Latency:'—'}
  return (await runProtocolRead([device],point,addressText))[0]
}
export async function manualProtocolWriteRow(host:string,addressText:string,valueText:string):Promise<Record<string,string>>{
  const device=store.devices.find(d=>d.host===host)
  if(!device)return {Target:host,Address:addressText,Write:valueText,Result:'Failed',Readback:'—',Error:'Manual raw write requires a configured device',Latency:'—'}
  const point=pointsOfTable(tableOfDevice(device)).find(p=>p.address.symbol===addressText||p.point_id===addressText)
  if(!point)return {Target:host,Address:addressText,Write:valueText,Result:'Failed',Readback:'—',Error:'Address is not mapped to a configured point',Latency:'—'}
  return await runProtocolWrite(device,point,valueText,addressText)
}

export interface ConfigValidation {ok:boolean;errors:string[]}
export async function validateConfig(file:string,text:string):Promise<ConfigValidation>{
  const row=await api<{valid:boolean;errors:string[]}>('/config/validate',{method:'POST',body:jsonBody({name:file,content:text})})
  return {ok:row.valid,errors:row.errors}
}
export async function applyConfig(file:string,text:string,comment=''){
  return await api<ApplyResult>('/config/apply',{method:'POST',body:jsonBody({name:file,content:text,comment})})
}
export async function importConfig(file:string,text:string,comment=''){
  return await api<ApplyResult>('/config/import',{method:'POST',body:jsonBody({name:file,content:text,comment})})
}
export async function restoreConfig(revision:number){
  return await api<ApplyResult>('/config/history/'+revision+'/restore',{method:'POST'})
}
export async function configHistory(){
  return await api<Array<{revision:number;created_at:string;source:string;comment:string}>>('/config/history')
}
export async function configBackup(){return await apiBlob('/config/backup')}
export function configApplyImpact(file:string){return ['Validate full configuration','Apply '+file,'Incremental runtime reconfigure']}
export async function applySystemYamlToState(_text:string){await refreshRuntimeState()}
export async function applyDevicesYamlToState(_text:string){await refreshRuntimeState()}
export async function logConfigApplied(_source:string,_file:string,_revision:number){await loadLogs()}

export interface PointTestOutcome {ok:boolean;error:string;errorCode:string;latency:number;bytes?:Uint8Array}
export async function testPointRead(device:DeviceInst,_protocol:string,requestText:string):Promise<PointTestOutcome>{
  const points=pointsOfTable(tableOfDevice(device))
  const point=points.find(p=>requestText.includes(p.point_id)||requestText.includes(p.address.symbol||''))
  if(!point)return {ok:false,error:'Raw-address test is not exposed by the configured protocol adapter; select a configured point',errorCode:'RAW_READ_UNAVAILABLE',latency:0}
  const result=await readDevicePoint(device,point,points.indexOf(point))
  return {ok:result.state==='success',error:result.error_message,errorCode:result.error_code,latency:result.latency_ms}
}

export async function runQualityCheck(){return}
