import { store } from './data'
import { reactive } from 'vue'
import { api } from './client'

export interface RuntimeLogEntry {
  time:string
  level:'ERROR'|'WARN'|'INFO'
  source:string
  object:string
  message:string
}

interface LogPage {
  items:Array<{timestamp:string;level:string;source:string;object:string;message:string}>
  page:{page:number;page_size:number;total:number}
}

export const deviceDataTick=reactive<Record<string,number>>({})
export const logStore=reactive<RuntimeLogEntry[]>([])

export async function loadLogs(limit=200){
  const result=await api<LogPage>(
    `/logs?page=1&page_size=${Math.min(200,limit)}&level=All`,
  )
  logStore.splice(0,logStore.length,...result.items.map(row=>({
    time:row.timestamp.replace('T',' ').replace('Z','').slice(0,19),
    level:(row.level==='WARNING'?'WARN':row.level) as RuntimeLogEntry['level'],
    source:row.source,
    object:row.object,
    message:row.message,
  })))
}


export function deviceRuntimeState(deviceId:string, enabled=true){
  const device=store.devices.find(row=>row.device_id===deviceId)
  if(!enabled||!device?.enabled){
    return {network:'disabled' as const,protocolError:null,failingPointIndexes:[],commandRejected:false,degradation:null}
  }
  if(!device.online){
    return {
      network:'unreachable' as const,
      protocolError:{code:'CONNECTION_UNAVAILABLE',message:'Device is not connected'},
      failingPointIndexes:[],
      commandRejected:false,
      degradation:null,
    }
  }
  return {network:'ok' as const,protocolError:null,failingPointIndexes:[],commandRejected:false,degradation:null}
}


export async function queryLogs(options:{
  page:number;pageSize:number;level?:string;source?:string;keyword?:string
}){
  const params=new URLSearchParams({
    page:String(options.page),
    page_size:String(options.pageSize),
  })
  if(options.level&&options.level!=='All')params.set('level',options.level)
  if(options.source&&options.source!=='All')params.set('source',options.source)
  if(options.keyword)params.set('keyword',options.keyword)
  return await api<LogPage>('/logs?'+params.toString())
}
