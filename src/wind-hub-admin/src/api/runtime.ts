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

export type MockLogEntry = RuntimeLogEntry

interface LogPage {
  items:Array<{timestamp:string;level:string;source:string;object:string;message:string}>
  page:{page:number;page_size:number;total:number}
}

export const deviceDataTick=reactive<Record<string,number>>({})
export const logStore=reactive<RuntimeLogEntry[]>([])

export async function loadLogs(limit=200){
  const result=await api<LogPage>(`/logs?page=1&page_size=${Math.min(200,limit)}&level=All`)
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
