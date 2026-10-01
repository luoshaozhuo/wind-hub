import { reactive } from 'vue'
import { api } from './client'
export type HealthRange='1 h'|'24 h'|'7 d'|'30 d'
export interface HealthSeries {axis:string[];memoryHost:number[];memoryRss:number[];cpuHost:number[];cpuProcess:number[];cpuTemp:number[];diskFree:number[];diskForecast:Array<number|null>;stressStart:number;forecastStart:number}
interface BackendHealth {uptime_seconds:number;cpu_count:number;load_average:[number,number,number]|null;risks:Array<{name:string;state:string;summary:string;detail:string}>;mounts:Array<any>;series:{timestamps:string[];memory_host_gb:Array<number|null>;memory_rss_gb:Array<number|null>;cpu_host_pct:Array<number|null>;cpu_process_pct:Array<number|null>;cpu_temp_c:Array<number|null>;disk_free_gb:Array<number|null>};current:Record<string,number|null>}
const state=reactive<{data:BackendHealth|null}>({data:null})
function apiRange(range:HealthRange){return range.replace(' ','')}
export async function loadHealth(range:HealthRange){
  state.data=await api<BackendHealth>('/system-health?range='+apiRange(range))
}
function nums(values:Array<number|null>){return values.map(v=>v??0)}
export function healthSeries(_range:HealthRange):HealthSeries{
  const d=state.data
  if(!d)return {axis:[],memoryHost:[],memoryRss:[],cpuHost:[],cpuProcess:[],cpuTemp:[],diskFree:[],diskForecast:[],stressStart:0,forecastStart:0}
  return {axis:d.series.timestamps.map(x=>x.replace('T',' ').slice(5,16)),memoryHost:nums(d.series.memory_host_gb),memoryRss:nums(d.series.memory_rss_gb),cpuHost:nums(d.series.cpu_host_pct),cpuProcess:nums(d.series.cpu_process_pct),cpuTemp:nums(d.series.cpu_temp_c),diskFree:nums(d.series.disk_free_gb),diskForecast:d.series.disk_free_gb.map(()=>null),stressStart:0,forecastStart:d.series.timestamps.length}
}
export const healthRisks=reactive<Array<{name:string;state:string;type:'success'|'warning'|'danger'|'info';summary:string;detail:string}>>([])
export const healthDetails=reactive<Array<{group:string;items:Array<[string,string]>}>>([])
export const storageMounts=reactive<Array<{mount:string;used:string;free:string;usage:string;growth:string;estimated:string}>>([])
export function syncHealthPresentation(){
  const d=state.data;if(!d)return
  healthRisks.splice(0,healthRisks.length,...d.risks.map(r=>({name:r.name,state:r.state,type:r.state==='Fault'?'danger':r.state==='Warning'?'warning':r.state==='Healthy'?'success':'info',summary:r.summary,detail:r.detail})))
  const c=d.current
  healthDetails.splice(0,healthDetails.length,
    {group:'Memory',items:[['Host used',c.memory_used_gb==null?'—':c.memory_used_gb.toFixed(2)+' GB'],['Host total',c.memory_total_gb==null?'—':c.memory_total_gb.toFixed(2)+' GB'],['wind-hub RSS',c.process_rss_gb==null?'—':c.process_rss_gb.toFixed(2)+' GB']]},
    {group:'CPU / Thermal',items:[['Host CPU',c.cpu_host_pct==null?'—':c.cpu_host_pct.toFixed(1)+'%'],['wind-hub CPU',c.cpu_process_pct==null?'—':c.cpu_process_pct.toFixed(1)+'%'],['CPU temp',c.cpu_temp_c==null?'—':c.cpu_temp_c.toFixed(1)+'°C'],['Uptime',Math.round(d.uptime_seconds)+' s']]},
  )
  storageMounts.splice(0,storageMounts.length,...d.mounts.map(m=>({mount:m.mount,used:m.used_gb.toFixed(1)+' GB / '+m.total_gb.toFixed(1)+' GB',free:m.free_gb.toFixed(1)+' GB',usage:m.usage_pct.toFixed(1)+'%',growth:m.growth_24h_gb==null?'—':m.growth_24h_gb.toFixed(2)+' GB / 24 h',estimated:m.estimated_full_days==null?'—':m.estimated_full_days.toFixed(1)+' days'})))
}
export function hostCurrent(){const c=state.data?.current;return {cpu:c?.cpu_host_pct??0,memory:c?.memory_total_gb&&c?.memory_used_gb?c.memory_used_gb/c.memory_total_gb*100:0,diskFree:c?.disk_free_gb??0,healthCheck:'live'}}
