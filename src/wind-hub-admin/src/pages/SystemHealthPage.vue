<script setup lang="ts">
import * as echarts from 'echarts'
import { nextTick, onBeforeUnmount, onMounted, ref } from 'vue'

const range=ref<'1 h'|'24 h'|'7 d'|'30 d'>('24 h')
const memoryEl=ref<HTMLElement|null>(null)
const diskEl=ref<HTMLElement|null>(null)
const cpuEl=ref<HTMLElement|null>(null)
 const charts:echarts.ECharts[]=[]
let resizeObserver:ResizeObserver|null=null

type TagType='success'|'warning'|'danger'
type Risk={name:string;state:string;type:TagType;summary:string;detail:string}

const risks:Risk[]=[
  {name:'Memory Growth',state:'Warning',type:'warning',summary:'RSS +1.1 GB / 6 h',detail:'Continuous growth · projected process limit ~14 h'},
  {name:'Storage Capacity',state:'Critical',type:'danger',summary:'38 GB free',detail:'14.2 GB / 24 h consumption · estimated full in 2.7 days'},
  {name:'CPU / Thermal',state:'Warning',type:'warning',summary:'87% / 86°C',detail:'10 min CPU average · high-load duration 13 min'},
  {name:'Runtime',state:'Normal',type:'success',summary:'3d 14h uptime',detail:'0 unexpected restarts · event loop stable'},
]

const details=[
  {group:'Memory',items:[['Host used','62%'],['Host available','12.1 GB'],['wind-hub RSS','3.8 GB'],['RSS growth / 6h','+1.1 GB'],['Open FD','184'],['Asyncio tasks','67']]},
  {group:'CPU / Thermal',items:[['Host CPU','91%'],['wind-hub CPU','72%'],['Load avg 1/5/15','7.8 / 7.2 / 6.4'],['CPU temp','86°C'],['High load','13 min'],['Throttling','No']]},
]

const storageMounts=[
  {mount:'/',used:'28 GB / 100 GB',free:'72 GB',usage:'28%',growth:'+0.4 GB / 24 h',estimated:'> 30 days'},
  {mount:'/data',used:'162 GB / 200 GB',free:'38 GB',usage:'81%',growth:'+9.2 GB / 24 h',estimated:'2.7 days'},
  {mount:'/var',used:'19 GB / 30 GB',free:'11 GB',usage:'63%',growth:'+3.1 GB / 24 h',estimated:'8.4 days'},
]

function timeAxis(){
  if(range.value==='1 h'){
    return Array.from({length:13},(_,i)=>String((60-(12-i)*5+60)%60).padStart(2,'0')+'m')
  }
  if(range.value==='7 d'){
    return Array.from({length:14},(_,i)=>'D-'+String(13-i))
  }
  if(range.value==='30 d'){
    return Array.from({length:15},(_,i)=>'D-'+String((14-i)*2))
  }
  return Array.from({length:24},(_,i)=>String((i+1)%24).padStart(2,'0')+':00')
}
function rangeScale(){
  if(range.value==='1 h')return 0.12
  if(range.value==='7 d')return 4.2
  if(range.value==='30 d')return 5
  return 1
}
function css(name:string,fallback:string){
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim()||fallback
}
function baseOption(){
  return {
    animation:false,
    textStyle:{fontFamily:'Inter,system-ui,sans-serif'},
    grid:{left:46,right:18,top:38,bottom:34},
    tooltip:{trigger:'axis'},
    xAxis:{type:'category',boundaryGap:false,axisLine:{lineStyle:{color:css('--app-border','#e5e9ef')}},axisLabel:{color:css('--app-text-muted','#98a2b3'),fontSize:10}},
    yAxis:{type:'value',axisLabel:{color:css('--app-text-muted','#98a2b3'),fontSize:10},splitLine:{lineStyle:{color:css('--app-border-soft','#eef0f3')}}},
  }
}
function initChart(el:HTMLElement|null,option:any){
  if(!el)return
  const chart=echarts.init(el)
  chart.setOption(option)
  charts.push(chart)
  resizeObserver?.observe(el)
}
function renderCharts(){
  charts.splice(0).forEach(c=>c.dispose())
  resizeObserver?.disconnect()
  resizeObserver=new ResizeObserver(()=>charts.forEach(c=>c.resize()))
  const axis=timeAxis()
  const scale=rangeScale()
  const base=baseOption()
  const stressStart=Math.floor(axis.length*0.7)
  initChart(memoryEl.value,{...base,legend:{top:4,right:8,textStyle:{fontSize:10}},xAxis:{...base.xAxis,data:axis},yAxis:{...base.yAxis,name:'GB',nameTextStyle:{fontSize:10}},series:[
    {name:'Host used',type:'line',showSymbol:false,data:axis.map((_,i)=>Number((9.4+i*0.05*scale+Math.sin(i/3)*0.25).toFixed(2)))},
    {name:'wind-hub RSS',type:'line',showSymbol:false,data:axis.map((_,i)=>Number((2.1+i*0.071*scale+Math.sin(i/4)*0.05).toFixed(2)))},
  ]})
  const diskBase=axis.map((_,i)=>Number((52.5-i*0.6*scale).toFixed(1)))
  const forecastStart=Math.max(1,Math.floor(axis.length*0.75))
  initChart(diskEl.value,{...base,legend:{top:4,right:8,textStyle:{fontSize:10}},xAxis:{...base.xAxis,data:axis},yAxis:{...base.yAxis,name:'GB',nameTextStyle:{fontSize:10}},series:[
    {name:'Actual free',type:'line',showSymbol:false,data:diskBase},
    {name:'Forecast',type:'line',showSymbol:false,lineStyle:{type:'dashed'},data:axis.map((_,i)=>i<forecastStart?null:Number((diskBase[forecastStart]-((i-forecastStart)*0.6*scale)).toFixed(1)))},
  ]})
  initChart(cpuEl.value,{...base,legend:{top:4,right:8,textStyle:{fontSize:10}},xAxis:{...base.xAxis,data:axis},yAxis:[
    {...base.yAxis,min:0,max:100,name:'CPU %',nameTextStyle:{fontSize:10}},
    {type:'value',min:40,max:100,name:'°C',position:'right',axisLabel:{color:css('--app-text-muted','#98a2b3'),fontSize:10},splitLine:{show:false},nameTextStyle:{fontSize:10}},
  ],series:[
    {name:'Host CPU',type:'line',showSymbol:false,yAxisIndex:0,data:axis.map((_,i)=>Number((42+Math.sin(i/2)*12+(i>stressStart?27:0)).toFixed(1)))},
    {name:'wind-hub CPU',type:'line',showSymbol:false,yAxisIndex:0,data:axis.map((_,i)=>Number((28+Math.sin(i/2.3)*8+(i>stressStart?35:0)).toFixed(1)))},
    {name:'CPU temperature',type:'line',showSymbol:false,yAxisIndex:1,data:axis.map((_,i)=>Number((58+Math.sin(i/3)*4+(i>stressStart?20:0)).toFixed(1)))},
  ]})
}
onMounted(()=>nextTick(renderCharts))
onBeforeUnmount(()=>{resizeObserver?.disconnect();charts.forEach(c=>c.dispose())})
</script>

<template>
  <div class="standard-page system-health-page">
    <div class="head">
      <div><h1>System Health</h1><p>宿主机与 wind-hub 进程资源健康，重点关注趋势、持续退化和容量耗尽风险。</p></div>
    </div>

    <section class="health-section">
      <div class="section-title"><div><h2>Current Risks</h2><p>当前最需要处理的资源风险。</p></div></div>
      <div class="risk-grid">
        <el-card v-for="r in risks" :key="r.name" shadow="never">
          <div class="risk-head"><b>{{r.name}}</b><el-tag :type="r.type" size="small">{{r.state}}</el-tag></div>
          <strong>{{r.summary}}</strong>
          <span class="risk-detail">{{r.detail}}</span>
        </el-card>
      </div>
    </section>

    <section class="health-section">
      <div class="section-title trends-title">
        <div><h2>Resource Trends</h2><p>趋势比单个瞬时值更重要；图表为当前前端 mock。</p></div>
        <el-segmented v-model="range" :options="['1 h','24 h','7 d','30 d']" @change="renderCharts"/>
      </div>
      <div class="chart-grid">
        <el-card shadow="never"><div class="chart-head"><b>Memory</b><span>Host used / wind-hub RSS</span></div><div ref="memoryEl" class="health-chart"/></el-card>
        <el-card shadow="never"><div class="chart-head"><b>CPU / Thermal</b><span>CPU usage + temperature</span></div><div ref="cpuEl" class="health-chart"/></el-card>
        <el-card shadow="never"><div class="chart-head"><b>Storage</b><span>Free space + forecast</span></div><div ref="diskEl" class="health-chart"/></el-card>
      </div>
    </section>

    <section class="health-section">
      <div class="section-title"><div><h2>Resource Details</h2><p>当前值用于确认风险背景，不替代趋势判断。</p></div></div>
      <div class="detail-grid">
        <el-card v-for="group in details" :key="group.group" shadow="never">
          <h3>{{group.group}}</h3>
          <div class="detail-list"><div v-for="item in group.items" :key="item[0]"><span>{{item[0]}}</span><b>{{item[1]}}</b></div></div>
        </el-card>
        <el-card shadow="never" class="storage-detail-card">
          <h3>Storage</h3>
          <el-table :data="storageMounts" size="small" table-layout="fixed">
            <el-table-column prop="mount" label="Mount" width="70"/>
            <el-table-column prop="used" label="Used / Total" min-width="120"/>
            <el-table-column prop="free" label="Free" width="80"/>
            <el-table-column prop="usage" label="Usage" width="70"/>
            <el-table-column prop="growth" label="24 h Growth" min-width="110"/>
            <el-table-column prop="estimated" label="Estimated Full" min-width="105"/>
          </el-table>
        </el-card>
      </div>
    </section>
  </div>
</template>

<style scoped>
.health-section{margin-top:var(--app-space-6)}.section-title{margin-bottom:var(--app-space-3)}.trends-title{display:flex;align-items:flex-end;justify-content:space-between;gap:var(--app-space-4)}.section-title h2{margin:0;font-size:var(--app-font-section-title);font-weight:var(--app-font-weight-semibold)}.section-title p{margin:4px 0 0;color:var(--app-text-muted);font-size:var(--app-font-caption)}
.risk-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:var(--app-space-3)}.risk-head{display:flex;align-items:center;justify-content:space-between;gap:var(--app-space-2)}.risk-head b{font-size:var(--app-font-panel-title)}.risk-grid strong{display:block;margin-top:var(--app-space-3);font-size:var(--app-font-panel-title);font-weight:var(--app-font-weight-semibold)}.risk-detail{display:block;margin-top:4px;color:var(--app-text-muted);font-size:var(--app-font-caption)}
.chart-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:var(--app-space-4)}.chart-head{display:flex;align-items:baseline;justify-content:space-between;gap:var(--app-space-3)}.chart-head b{font-size:var(--app-font-panel-title)}.chart-head span{color:var(--app-text-muted);font-size:var(--app-font-caption)}.health-chart{height:250px}
.detail-grid{display:grid;grid-template-columns:minmax(220px,.8fr) minmax(220px,.8fr) minmax(0,1.8fr);gap:var(--app-space-3)}.detail-grid h3{margin:0 0 var(--app-space-2);font-size:var(--app-font-panel-title)}.detail-list{display:grid}.detail-list>div{display:flex;align-items:center;justify-content:space-between;gap:var(--app-space-3);padding:var(--app-space-2) 0;border-bottom:1px solid var(--app-border-soft)}.detail-list>div:last-child{border-bottom:0}.detail-list span{color:var(--app-text-muted);font-size:var(--app-font-caption)}.detail-list b{font-size:var(--app-font-body);font-weight:var(--app-font-weight-semibold)}.storage-detail-card{min-width:0}
@media(max-width:1199px){.risk-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.chart-grid{grid-template-columns:1fr}.detail-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.storage-detail-card{grid-column:1/-1}}
@media(max-width:767px){.risk-grid,.detail-grid,.chart-grid{grid-template-columns:1fr}.storage-detail-card{grid-column:auto}.trends-title{align-items:flex-start;flex-direction:column}.health-chart{height:230px}}
</style>
