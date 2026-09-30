<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  DEFAULT_POINT_GROUP_ID,
  devicesForTask,
  isDefaultPointTable,
  modelOf,
  pointsOfTable,
  refreshTaskValidity,
  store,
  tableOfDevice,
} from '../mock/data'
import { useViewport } from '../composables/useViewport'
import type { DeviceInst, TaskDef } from '../mock/types'

const createDialog = ref(false)
const detailOpen = ref(false)
const detailTab = ref('Summary')
const selectedTaskId = ref('')
const selectedDeviceId = ref('')
const taskSnapshot = ref('')
const taskLogLimit = ref(20)
const { isMobile } = useViewport()
const drawerSize = computed(() => isMobile.value ? '100%' : 'min(1080px, 86vw)')

const form=reactive({
  task_id:'', scope:'device_group' as 'device'|'device_group',
  device:'',device_group:'',point_group:'',interval:1 as number|null,
  sinks:[] as string[],enabled:true,
})
const validPointGroups=computed(()=>store.pointGroups.filter(g=>!g.system))
const selectedTask=computed(()=>store.tasks.find(t=>t.task_id===selectedTaskId.value))
const taskDevices=computed(()=>selectedTask.value?devicesForTask(selectedTask.value):[])
const selectedDevice=computed(()=>taskDevices.value.find(d=>d.device_id===selectedDeviceId.value) || taskDevices.value[0])
const selectedDevicePoints=computed(()=>{
  const task=selectedTask.value, device=selectedDevice.value
  if(!task||!device) return []
  return pointsOfTable(tableOfDevice(device)).filter(p=>p.point_groups.includes(task.point_group))
})
const totalPointBindings=computed(()=>taskDevices.value.reduce((sum,d)=>
  sum+pointsOfTable(tableOfDevice(d)).filter(p=>selectedTask.value && p.point_groups.includes(selectedTask.value.point_group)).length,0))
const taskFormState=computed(()=>JSON.stringify({
  scope:form.scope,device:form.device,device_group:form.device_group,point_group:form.point_group,
  interval:form.interval,sinks:[...form.sinks],enabled:form.enabled,
}))
const taskDirty=computed(()=>!!selectedTask.value && taskFormState.value!==taskSnapshot.value)

const taskLogs=computed(()=>{
  const id=selectedTask.value?.task_id || 'task'
  const templates=[
    {level:'INFO',message:`${id} cycle completed · 0 errors`},
    {level:'INFO',message:`${taskDevices.value.length} device instance(s) scheduled`},
    {level:'WARN',message:'One collection cycle exceeded expected interval by 42 ms'},
    {level:'INFO',message:'Sink delivery completed'},
    {level:'INFO',message:'Point batch read completed'},
    {level:'INFO',message:'Runtime heartbeat OK'},
  ]
  return Array.from({length:120},(_,i)=>{
    const totalSeconds=12*3600+42*60+31-i*7
    const normalized=((totalSeconds%86400)+86400)%86400
    const h=String(Math.floor(normalized/3600)).padStart(2,'0')
    const m=String(Math.floor((normalized%3600)/60)).padStart(2,'0')
    const s=String(normalized%60).padStart(2,'0')
    const base=templates[i%templates.length]
    return {time:`${h}:${m}:${s}`,level:base.level,message:base.message}
  })
})
const visibleTaskLogs=computed(()=>taskLogs.value.slice(0,taskLogLimit.value))

const deviceUsesDefaultTable=(deviceId:string)=>{
  const d=store.devices.find(x=>x.device_id===deviceId)
  return !!d && isDefaultPointTable(modelOf(d)?.point_table||'')
}
const groupUsesDefaultTable=(groupId:string)=>store.devices.some(d=>d.device_group===groupId && isDefaultPointTable(modelOf(d)?.point_table||''))

refreshTaskValidity()

function loadForm(t?:TaskDef){
  if(t){
    form.task_id=t.task_id; form.scope=t.device?'device':'device_group'; form.device=t.device
    form.device_group=t.device_group; form.point_group=t.point_group; form.interval=t.interval
    form.sinks=[...t.sinks]; form.enabled=t.enabled
    taskSnapshot.value=JSON.stringify({
      scope:form.scope,device:form.device,device_group:form.device_group,point_group:form.point_group,
      interval:form.interval,sinks:[...form.sinks],enabled:form.enabled,
    })
  }else{
    form.task_id=''; form.scope='device_group'; form.device=''; form.device_group=''
    form.point_group=validPointGroups.value[0]?.id||''; form.interval=1
    form.sinks=store.sinks.filter(s=>s.enabled).slice(0,1).map(s=>s.name); form.enabled=true
  }
}
function openNew(){ loadForm(); createDialog.value=true }
function openDetail(t:TaskDef){
  selectedTaskId.value=t.task_id; selectedDeviceId.value=devicesForTask(t)[0]?.device_id||''
  detailTab.value='Summary'; loadForm(t); detailOpen.value=true
}
async function beforeTaskClose(done:()=>void){
  if(!taskDirty.value){done();return}
  try{
    await ElMessageBox.confirm('Discard unsaved Task changes?','Unsaved Changes',{type:'warning',confirmButtonText:'Discard'})
    done()
  }catch{ /* keep drawer open */ }
}

function validateForm(){
  const id=form.task_id.trim()
  if(!id) return 'Task ID is required'
  const device=form.scope==='device'?form.device:''
  const group=form.scope==='device_group'?form.device_group:''
  if(!device&&!group) return "Exactly one of device / device_group is required"
  if(!form.point_group||form.point_group===DEFAULT_POINT_GROUP_ID) return 'Select a valid Point Group'
  if(device&&deviceUsesDefaultTable(device)) return 'Selected device uses a default Point Table'
  if(group&&groupUsesDefaultTable(group)) return 'Selected group contains device(s) using a default Point Table'
  if(form.interval===null||form.interval<=0) return 'Interval must be > 0'
  if(!form.sinks.length) return 'At least one Sink is required'
  if(form.sinks.some(name=>!store.sinks.find(s=>s.name===name)?.enabled)) return 'All selected Sinks must be enabled'
  return ''
}
async function persistTask(existing?:TaskDef){
  const error=validateForm(); if(error){ElMessage.error(error);return false}
  const device=form.scope==='device'?form.device:''
  const device_group=form.scope==='device_group'?form.device_group:''
  if(!existing){
    const id=form.task_id.trim()
    if(store.tasks.some(t=>t.task_id===id)){ElMessage.error('Task ID already exists');return false}
    const now=nowText()
    store.tasks.push({task_id:id,device,device_group,point_group:form.point_group,interval:form.interval,sinks:[...form.sinks],enabled:form.enabled,runtime:'STOPPED',created_at:now,updated_at:now})
  }else{
    const wasRunning=existing.runtime==='RUNNING'
    const changed=existing.device!==device||existing.device_group!==device_group||existing.point_group!==form.point_group||
      existing.interval!==form.interval||JSON.stringify(existing.sinks)!==JSON.stringify(form.sinks)||existing.enabled!==form.enabled
    if(wasRunning&&changed){
      await ElMessageBox.confirm(
        'Task "'+existing.task_id+'" is running. Affected instances will stop while the definition is applied and restart only if still valid.',
        'Task Change Impact',{type:'warning',confirmButtonText:'Apply Changes'})
      existing.runtime='STOPPED'
    }
    Object.assign(existing,{device,device_group,point_group:form.point_group,interval:form.interval,sinks:[...form.sinks],enabled:form.enabled,updated_at:nowText()})
    refreshTaskValidity()
    if(wasRunning&&existing.enabled&&existing.valid!==false) existing.runtime='RUNNING'
  }
  refreshTaskValidity()
  return true
}
async function createTask(){ if(await persistTask()){createDialog.value=false;ElMessage.success('Task created (mock)')} }
async function saveTaskEdit(){
  if(!selectedTask.value) return
  if(await persistTask(selectedTask.value)){loadForm(selectedTask.value); selectedDeviceId.value=taskDevices.value[0]?.device_id||''; ElMessage.success('Task updated (mock)')}
}
async function changeEnabled(t:TaskDef,enabled:boolean){
  if(!enabled&&t.runtime==='RUNNING'){
    try{await ElMessageBox.confirm('Disabling "'+t.task_id+'" will stop its running instances.','Disable Task',{type:'warning',confirmButtonText:'Disable'});t.runtime='STOPPED'}
    catch{t.enabled=true;return}
  }
  refreshTaskValidity()
}
function toggle(t:TaskDef){
  refreshTaskValidity()
  if(t.valid===false){ElMessage.error(t.invalid_reason||'Task is invalid');return}
  if(t.runtime!=='RUNNING'&&!t.enabled){ElMessage.warning('Task is disabled');return}
  t.runtime=t.runtime==='RUNNING'?'STOPPED':'RUNNING'
  ElMessage.success(`Task ${t.task_id} ${t.runtime==='RUNNING'?'started':'stopped'} (mock)`)
}
async function del(t:TaskDef){
  await ElMessageBox.confirm(
    t.runtime==='RUNNING'?'Task "'+t.task_id+'" is running and will be stopped before deletion.':'Delete task "'+t.task_id+'"?',
    'Delete Task',{type:'warning',confirmButtonText:'Delete'})
  t.runtime='STOPPED'; store.tasks=store.tasks.filter(x=>x!==t)
  if(selectedTaskId.value===t.task_id) detailOpen.value=false
  ElMessage.success('Task deleted (mock)')
}
function nowText(){
  const d=new Date(); const p=(n:number)=>String(n).padStart(2,'0')
  return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}
function targetText(t:TaskDef){return t.device?`device: ${t.device}`:`group: ${t.device_group}`}
function devicePointCount(d:DeviceInst){return selectedTask.value?pointsOfTable(tableOfDevice(d)).filter(p=>p.point_groups.includes(selectedTask.value!.point_group)).length:0}
function chooseDevice(d:DeviceInst){selectedDeviceId.value=d.device_id}
</script>

<template>
  <div class="standard-page">
    <div class="head"><div><h1>Tasks</h1><p>采集任务定义、运行控制与实例详情</p></div><el-button type="primary" @click="openNew">+ New Task</el-button></div>

    <el-card shadow="never">
      <el-table :data="store.tasks" row-key="task_id">
        <el-table-column label="Task" min-width="190">
          <template #default="{row}"><el-link :underline="false" @click="openDetail(row)"><b>{{row.task_id}}</b></el-link></template>
        </el-table-column>
        <el-table-column label="Target" min-width="180"><template #default="{row}">{{targetText(row)}}</template></el-table-column>
        <el-table-column v-if="!isMobile" prop="point_group" label="Point Group" min-width="130"/>
        <el-table-column v-if="!isMobile" label="Instances" width="100" align="right"><template #default="{row}">{{devicesForTask(row).length}}</template></el-table-column>
        <el-table-column v-if="!isMobile" prop="created_at" label="Created" min-width="150"/>
        <el-table-column prop="updated_at" label="Updated" min-width="150"/>
        <el-table-column label="Runtime" width="110">
          <template #default="{row}">
            <el-tooltip v-if="row.valid===false" :content="row.invalid_reason" placement="top"><el-tag type="danger">INVALID</el-tag></el-tooltip>
            <el-tag v-else :type="row.runtime==='RUNNING'?'success':'info'">{{row.runtime}}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="Enabled" width="100"><template #default="{row}"><el-switch v-model="row.enabled" @change="changeEnabled(row,!!$event)"/></template></el-table-column>
        <el-table-column label="Operation" width="150">
          <template #default="{row}">
            <el-button size="small" :disabled="row.valid===false" @click="toggle(row)">{{row.runtime==='RUNNING'?'Stop':'Start'}}</el-button>
            <el-dropdown trigger="click">
              <el-button size="small">•••</el-button>
              <template #dropdown><el-dropdown-menu><el-dropdown-item style="color:var(--app-status-fault)" @click="del(row)">Delete</el-dropdown-item></el-dropdown-menu></template>
            </el-dropdown>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-drawer v-model="detailOpen" :title="selectedTask?.task_id || 'Task'" direction="rtl" :size="drawerSize" append-to-body destroy-on-close :before-close="beforeTaskClose">
      <template v-if="selectedTask">
        <div class="task-drawer-head">
          <div>
            <el-tag v-if="selectedTask.valid===false" type="danger">INVALID</el-tag>
            <el-tag v-else :type="selectedTask.runtime==='RUNNING'?'success':'info'">{{selectedTask.runtime}}</el-tag>
            <span>{{taskDevices.length}} device instance(s) · {{totalPointBindings}} point binding(s)</span>
          </div>
          <el-button @click="toggle(selectedTask)">{{selectedTask.runtime==='RUNNING'?'Stop':'Start'}}</el-button>
        </div>

        <el-tabs v-model="detailTab">
          <el-tab-pane label="Summary" name="Summary">
            <div class="task-summary-grid">
              <section class="task-summary-card">
                <div class="summary-card-head">
                  <div>
                    <h3>Definition</h3>
                    <p>任务定义与运行参数放在同一工作区，修改后直接 Save。</p>
                  </div>
                  <div class="task-config-actions">
                    <el-button type="primary" :disabled="!taskDirty" @click="saveTaskEdit">Save</el-button>
                  </div>
                </div>

                <el-form label-position="top">
                  <div class="task-form-grid">
                    <el-form-item label="Task ID"><el-input v-model="form.task_id" disabled/></el-form-item>
                    <el-form-item label="Scope">
                      <el-select v-model="form.scope" style="width:100%">
                        <el-option label="Device Group" value="device_group"/>
                        <el-option label="Single Device" value="device"/>
                      </el-select>
                    </el-form-item>
                    <el-form-item v-if="form.scope==='device_group'" label="Device Group">
                      <el-select v-model="form.device_group" style="width:100%">
                        <el-option v-for="g in store.deviceGroups" :key="g.id" :label="g.id" :value="g.id" :disabled="groupUsesDefaultTable(g.id)"/>
                      </el-select>
                    </el-form-item>
                    <el-form-item v-else label="Device">
                      <el-select v-model="form.device" filterable style="width:100%">
                        <el-option v-for="d in store.devices" :key="d.device_id" :label="d.device_id" :value="d.device_id" :disabled="deviceUsesDefaultTable(d.device_id)"/>
                      </el-select>
                    </el-form-item>
                    <el-form-item label="Point Group">
                      <el-select v-model="form.point_group" style="width:100%">
                        <el-option v-for="g in validPointGroups" :key="g.id" :label="g.id" :value="g.id"/>
                      </el-select>
                    </el-form-item>
                    <el-form-item label="Interval (s)">
                      <el-input-number v-model="form.interval" :min="0.1" :step="0.5" style="width:100%"/>
                    </el-form-item>
                    <el-form-item label="Target Sinks">
                      <el-select v-model="form.sinks" multiple style="width:100%">
                        <el-option v-for="s in store.sinks" :key="s.name" :label="s.name" :value="s.name" :disabled="!s.enabled"/>
                      </el-select>
                    </el-form-item>
                    <el-form-item label="Enabled"><el-switch v-model="form.enabled"/></el-form-item>
                  </div>
                </el-form>
              </section>

              <aside class="task-runtime-card">
                <div class="summary-card-head">
                  <div><h3>Runtime</h3><p>当前运行事实，不与 Definition 重复。</p></div>
                </div>
                <div class="runtime-status-line">
                  <el-tag v-if="selectedTask.valid===false" type="danger">INVALID</el-tag>
                  <el-tag v-else :type="selectedTask.runtime==='RUNNING'?'success':'info'">{{selectedTask.runtime}}</el-tag>
                  <span>{{ selectedTask.enabled ? 'Enabled' : 'Disabled' }}</span>
                </div>
                <div class="runtime-metrics">
                  <div><span>Instances</span><b>{{taskDevices.length}}</b></div>
                  <div><span>Point Bindings</span><b>{{totalPointBindings}}</b></div>
                  <div><span>Target</span><b>{{targetText(selectedTask)}}</b></div>
                  <div><span>Point Group</span><b>{{selectedTask.point_group}}</b></div>
                  <div><span>Sinks</span><b>{{selectedTask.sinks.join(', ')}}</b></div>
                  <div><span>Created</span><b>{{selectedTask.created_at}}</b></div>
                  <div><span>Last Modified</span><b>{{selectedTask.updated_at}}</b></div>
                </div>
                <el-alert v-if="selectedTask.valid===false" type="error" :closable="false" :title="selectedTask.invalid_reason" />
              </aside>
            </div>
          </el-tab-pane>

          <el-tab-pane label="Devices & Points" name="Coverage">
            <div class="coverage-layout">
              <div class="coverage-devices">
                <div class="pane-title"><b>Devices</b><span>{{taskDevices.length}}</span></div>
                <el-table :data="taskDevices" row-key="device_id" highlight-current-row :current-row-key="selectedDevice?.device_id" max-height="560" @row-click="chooseDevice">
                  <el-table-column prop="device_id" label="Device" min-width="130"/>
                  <el-table-column prop="host" label="Host" min-width="145"/>
                  <el-table-column label="Points" width="78" align="right"><template #default="{row}">{{devicePointCount(row)}}</template></el-table-column>
                </el-table>
              </div>
              <div class="coverage-points">
                <div class="pane-title">
                  <div><b>{{selectedDevice?.device_id || 'Select a device'}}</b><span v-if="selectedDevice"> · {{modelOf(selectedDevice)?.point_table}}</span></div>
                  <span>{{selectedDevicePoints.length}} points</span>
                </div>
                <el-table v-if="selectedDevice" :data="selectedDevicePoints" max-height="560" size="small">
                  <el-table-column prop="point_id" label="Point" min-width="160"/>
                  <el-table-column prop="variable_name" label="Variable" min-width="160"/>
                  <el-table-column prop="data_type" label="Type" width="100"/>
                  <el-table-column prop="unit" label="Unit" width="90"/>
                </el-table>
                <el-empty v-else description="No target device"/>
              </div>
            </div>
          </el-tab-pane>

          <el-tab-pane label="Logs" name="Logs">
            <div class="task-log-toolbar">
              <div><b>Recent Task Logs</b><span>Latest {{ taskLogLimit }} records</span></div>
              <el-select v-model="taskLogLimit" style="width:120px">
                <el-option :value="20" label="Latest 20"/>
                <el-option :value="50" label="Latest 50"/>
                <el-option :value="100" label="Latest 100"/>
              </el-select>
            </div>
            <el-timeline>
              <el-timeline-item v-for="log in visibleTaskLogs" :key="log.time+log.message" :timestamp="log.time" placement="top" :type="log.level==='WARN'?'warning':'primary'">
                <b>{{log.level}}</b> · {{log.message}}
              </el-timeline-item>
            </el-timeline>
          </el-tab-pane>


        </el-tabs>
      </template>
    </el-drawer>

    <el-dialog v-model="createDialog" title="New Task" width="680px">
      <el-form label-position="top">
        <div class="task-form-grid">
          <el-form-item label="Task ID"><el-input v-model="form.task_id"/></el-form-item>
          <el-form-item label="Scope"><el-select v-model="form.scope" style="width:100%"><el-option label="Device Group" value="device_group"/><el-option label="Single Device" value="device"/></el-select></el-form-item>
          <el-form-item v-if="form.scope==='device_group'" label="Device Group"><el-select v-model="form.device_group" style="width:100%"><el-option v-for="g in store.deviceGroups" :key="g.id" :label="g.id" :value="g.id" :disabled="groupUsesDefaultTable(g.id)"/></el-select></el-form-item>
          <el-form-item v-else label="Device"><el-select v-model="form.device" filterable style="width:100%"><el-option v-for="d in store.devices" :key="d.device_id" :label="d.device_id" :value="d.device_id" :disabled="deviceUsesDefaultTable(d.device_id)"/></el-select></el-form-item>
          <el-form-item label="Point Group"><el-select v-model="form.point_group" style="width:100%"><el-option v-for="g in validPointGroups" :key="g.id" :label="g.id" :value="g.id"/></el-select></el-form-item>
          <el-form-item label="Interval (s)"><el-input-number v-model="form.interval" :min="0.1" :step="0.5" style="width:100%"/></el-form-item>
          <el-form-item label="Target Sinks"><el-select v-model="form.sinks" multiple style="width:100%"><el-option v-for="s in store.sinks" :key="s.name" :label="s.name" :value="s.name" :disabled="!s.enabled"/></el-select></el-form-item>
          <el-form-item label="Enabled"><el-switch v-model="form.enabled"/></el-form-item>
        </div>
      </el-form>
      <template #footer><el-button @click="createDialog=false">Cancel</el-button><el-button type="primary" @click="createTask">Create</el-button></template>
    </el-dialog>
  </div>
</template>

<style scoped>
.task-drawer-head{display:flex;align-items:center;justify-content:space-between;gap:var(--app-space-3);margin-bottom:var(--app-space-3)}
.task-drawer-head>div{display:flex;align-items:center;gap:var(--app-space-2);color:var(--app-text-muted);font-size:var(--app-font-caption)}
.coverage-layout{display:grid;grid-template-columns:minmax(300px,.8fr) minmax(420px,1.2fr);gap:var(--app-space-4)}
.coverage-devices,.coverage-points{min-width:0;border:1px solid var(--app-border-soft);border-radius:var(--app-card-radius);padding:var(--app-space-3)}
.pane-title{display:flex;align-items:center;justify-content:space-between;gap:var(--app-space-2);margin-bottom:var(--app-space-2);color:var(--app-text-muted);font-size:var(--app-font-caption)}
.pane-title b{color:var(--app-text-primary);font-size:var(--app-font-body)}
.task-summary-grid{display:grid;grid-template-columns:minmax(0,1.45fr) minmax(300px,.55fr);gap:var(--app-space-4)}
.task-summary-card,.task-runtime-card{border:1px solid var(--app-border-soft);border-radius:var(--app-card-radius);padding:var(--app-space-4);min-width:0}
.summary-card-head{display:flex;align-items:flex-start;justify-content:space-between;gap:var(--app-space-3);margin-bottom:var(--app-space-4)}
.summary-card-head h3{margin:0}.summary-card-head p{margin:4px 0 0;color:var(--app-text-muted);font-size:var(--app-font-caption)}
.task-config-actions{display:flex;justify-content:flex-end;gap:var(--app-space-2)}
.runtime-status-line{display:flex;align-items:center;gap:var(--app-space-2);margin-bottom:var(--app-space-4);color:var(--app-text-muted)}
.runtime-metrics{display:grid;gap:var(--app-space-3);margin-bottom:var(--app-space-4)}
.runtime-metrics>div{display:flex;justify-content:space-between;gap:var(--app-space-3);padding-bottom:var(--app-space-2);border-bottom:1px solid var(--app-border-soft)}
.runtime-metrics span{color:var(--app-text-muted)}.runtime-metrics b{text-align:right;overflow-wrap:anywhere}
.task-form-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:0 var(--app-space-4)}
.task-log-toolbar{display:flex;align-items:center;justify-content:space-between;gap:var(--app-space-3);margin-bottom:var(--app-space-4)}
.task-log-toolbar>div{display:flex;align-items:baseline;gap:var(--app-space-2)}
.task-log-toolbar span{color:var(--app-text-muted);font-size:var(--app-font-caption)}
@media(max-width:1199px){.coverage-layout,.task-form-grid,.task-summary-grid{grid-template-columns:1fr}.task-drawer-head{align-items:flex-start;flex-direction:column}}
</style>
