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
  const result=await api<LogPage>(`/logs?page=1&page_size=${Math.min(200,limit)}&level=All`)
  logStore.splice(0,logStore.length,...result.items.map(row=>({
    time:row.timestamp.replace('T',' ').replace('Z','').slice(0,19),
    level:(row.level==='WARNING'?'WARN':row.level) as RuntimeLogEntry['level'],
    source:row.source,
    object:row.object,
    message:row.message,
  })))
}
