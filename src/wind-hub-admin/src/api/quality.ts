import { reactive } from 'vue'
import { api } from './client'

export type QualityWindow='1 h'|'24 h'|'7 d'
export interface CommunicationEvent {id:number;time:string;object:string;protocol:string;event:string;state:'Active'|'Recovered';error:string;duration:string;target:string}
export interface QualityProblem {object:string;kind:'Task'|'Device'|'Point'|'Sink';metric:string;expected:string;state:'Warning'|'Fault';error:string}
export interface QualityDimensionRow {key:'continuity'|'timeliness'|'completeness'|'validity'|'delivery';dimension:string;status:'Normal'|'Warning'|'Fault';metric:string;detail:string}
export interface QualityIssue {level:'Fault'|'Warning';object:string;kind:'Task'|'Device'|'Point'|'Sink';dimension:string;issue:string;duration:string;error:string}
interface ChannelRow {object:string;source:'Acquisition'|'Delivery';protocol:string;state:'Healthy'|'Degraded'|'Interrupted'|'Disabled';target:string;last:string;latency:string;timeouts:number;reconnects:number;issue:string}
interface BackendQuality {
  acquisition_channels:Array<any>;delivery_channels:Array<any>;channel_summary:Array<any>;data_metrics:Array<any>;
  dimensions:Array<any>;issues:Array<any>;events:Array<any>
}
export interface QualityWindowData {
  channelSummary:Array<{key:string;label:string;value:number;tone:string}>
  communicationEvents:CommunicationEvent[]
  dataMetrics:Array<{key:string;label:string;value:number;hint:string;tone:string}>
  dimensions:QualityDimensionRow[]
  issues:QualityIssue[]
}
const cache=reactive<Record<QualityWindow,QualityWindowData>>({
  '1 h':{channelSummary:[],communicationEvents:[],dataMetrics:[],dimensions:[],issues:[]},
  '24 h':{channelSummary:[],communicationEvents:[],dataMetrics:[],dimensions:[],issues:[]},
  '7 d':{channelSummary:[],communicationEvents:[],dataMetrics:[],dimensions:[],issues:[]},
})
const channelCache=reactive<Record<QualityWindow,{acquisition:ChannelRow[];delivery:ChannelRow[]}>>({
  '1 h':{acquisition:[],delivery:[]},'24 h':{acquisition:[],delivery:[]},'7 d':{acquisition:[],delivery:[]},
})
function apiWindow(window:QualityWindow){return window==='1 h'?'1h':window==='24 h'?'24h':'7d'}
function channel(row:any):ChannelRow{return {object:String(row.object),source:row.source,protocol:String(row.protocol),state:row.state,target:String(row.target),last:'—',latency:row.latency_ms==null?'—':Math.round(row.latency_ms)+' ms',timeouts:Number(row.timeouts||0),reconnects:Number(row.reconnects||0),issue:String(row.issue||'—')}}
export async function loadQualityWindow(window:QualityWindow,check=false){
  const row=await api<BackendQuality>('/quality'+(check?'/check':'')+'?window='+apiWindow(window),check?{method:'POST'}:{})
  channelCache[window]={acquisition:row.acquisition_channels.map(channel),delivery:row.delivery_channels.map(channel)}
  cache[window]={
    channelSummary:row.channel_summary.map(x=>({key:x.key,label:x.label,value:Number(x.value),tone:x.status})),
    communicationEvents:row.events.map((x,i)=>({id:i,time:String(x.timestamp).replace('T',' ').slice(0,19),object:x.object,protocol:'—',event:x.event,state:x.state,error:x.evidence,duration:'—',target:'—'})),
    dataMetrics:row.data_metrics.map(x=>({key:x.key,label:x.label,value:Number(x.value),hint:x.hint,tone:x.status})),
    dimensions:row.dimensions,
    issues:row.issues.map(x=>({...x,duration:x.duration_seconds==null?'—':Math.round(x.duration_seconds)+' s',error:x.error||'—'})),
  }
}
export function qualityWindowData(window:QualityWindow){return cache[window]}
export function acquisitionChannels(window:QualityWindow='24 h'){return channelCache[window].acquisition}
export function deliveryChannels(window:QualityWindow='24 h'){return channelCache[window].delivery}
export function qualityMetricDetail(key:string,window:QualityWindow){
  const data=cache[window];const issues=data.issues.filter(i=>key==='dropped'?i.dimension.includes('Delivery'):true)
  return {tasks:issues.filter(i=>i.kind==='Task').map(i=>({Task:i.object,State:i.level,Error:i.error})),devices:issues.filter(i=>i.kind==='Device'||i.kind==='Point').map(i=>({Device:i.object,State:i.level,Error:i.error})),errors:issues.map(i=>({Object:i.object,Error:i.error}))}
}
export function qualityDimensionDetail(key:string,window:QualityWindow){
  const row=cache[window].dimensions.find(x=>x.key===key);const issues=cache[window].issues.filter(i=>i.dimension.toLowerCase().startsWith((row?.dimension||key).toLowerCase().split(' ')[0]))
  return {distribution:[{name:row?.status||'Normal',value:1}],problems:issues.map(i=>({object:i.object,kind:i.kind,metric:i.issue,expected:'See threshold',state:i.level,error:i.error})) as QualityProblem[]}
}
export function qualityChannelMetricDetail(_key:string,window:QualityWindow){
  const rows=[...channelCache[window].acquisition,...channelCache[window].delivery]
  return {distribution:rows.reduce<Array<{name:string;value:number}>>((acc,row)=>{const hit=acc.find(x=>x.name===row.state);if(hit)hit.value++;else acc.push({name:row.state,value:1});return acc},[]),devices:rows.map(r=>({Device:r.object,Protocol:r.protocol,State:r.state,Error:r.issue})),errors:cache[window].communicationEvents.map(e=>({Time:e.time,Object:e.object,Event:e.event,Error:e.error}))}
}
