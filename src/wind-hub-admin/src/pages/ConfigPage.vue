<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  LATENCY,
  applyDevicesYamlToState,
  applySystemYamlToState,
  configApplyImpact,
  logConfigApplied,
  sleep,
  validateConfig,
  validateConfigRemote,
  applyConfigRemote,
  downloadConfigBackup,
  loadConfigHistory,
  restoreConfigRevision,
} from '../services/backend'
import { CONFIG_FILES, yamlFiles } from '../mock/yaml'
import { nowText } from '../utils/format'

type ReviewLine={type:'same'|'add'|'remove';text:string}
interface HistoryEntry {
  revision:number
  time:string
  source:string
  comment:string
  status:string
  // Revision 快照（§18 三层：Working Copy / Applied / Revision History）：
  // Restore 以快照为事实源真正回写 yamlFiles 与 mock state。
  snapshot:Record<string,string>
}

const section=ref<'files'|'history'>('files')
const file=ref('devices.yaml')
const editorMode=ref<'edit'|'review'>('review')
const revision=ref(42)
const importOpen=ref(false)
const validating=ref(false)
const applying=ref(false)
const importValidating=ref(false)
const importApplying=ref(false)
const restoring=ref(false)

const appliedSnapshot=reactive<Record<string,string>>(
  Object.fromEntries(CONFIG_FILES.map(name=>[name,yamlFiles[name]])),
)
const dirtyMap=reactive<Record<string,boolean>>({})

function snapshotOfApplied(){
  return Object.fromEntries(CONFIG_FILES.map(name=>[name,appliedSnapshot[name]]))
}

// Apply / Import / Restore 统一走同一 revision 推进逻辑：
// revision+1、历史头部插入 Applied 记录（含全量快照）、旧 Applied 记录归档为 Archived。
// Backup 不产生 revision。
function pushRevision(source:string,comment:string){
  revision.value+=1
  for(const entry of history.value)if(entry.status==='Applied')entry.status='Archived'
  history.value.unshift({revision:revision.value,time:nowText(),source,comment,status:'Applied',snapshot:snapshotOfApplied()})
}

// 配置文本应用后同步结构化 mock state（§18：Devices / Settings / Overview 立即反映）
function applyTextToState(name:string,text:string){
  if(name==='system.yaml')applySystemYamlToState(text)
  if(name==='devices.yaml')applyDevicesYamlToState(text)
}

function markDirty(){dirtyMap[file.value]=yamlFiles[file.value]!==appliedSnapshot[file.value]}
// Validate（§20）：确定性规则 —— 空文档 / Tab 缩进 / 重复 device_id / 未知引用失败，其余通过。
async function validate(){
  if(validating.value||applying.value)return false
  validating.value=true
  try{
    await sleep(LATENCY.uiLocal)
    const result=await validateConfigRemote(file.value,yamlFiles[file.value])
    if(!result.ok){
      ElMessage.error(result.errors[0]+(result.errors.length>1?` (+${result.errors.length-1} more)`:'')+'')
      return false
    }
    ElMessage.success(file.value+' validation passed')
    return true
  }finally{validating.value=false}
}
// Apply（§22）：confirm 展示按 target file 生成的影响范围；成功后回写 mock state + 记录日志。
async function apply(){
  if(applying.value||validating.value||!dirtyMap[file.value])return
  if(!await validate())return
  try{
    await ElMessageBox.confirm(
      'Apply only affected runtime objects. Impact: '+configApplyImpact(file.value).join('; ')+'.',
      'Apply Configuration',
      {type:'warning',confirmButtonText:'Apply'},
    )
  }catch{return}
  applying.value=true
  try{
    const result:any=await applyConfigRemote(file.value,yamlFiles[file.value])
    applyTextToState(file.value,yamlFiles[file.value])
    appliedSnapshot[file.value]=yamlFiles[file.value]
    dirtyMap[file.value]=false
    pushRevision('Apply',file.value+' applied from workspace')
    revision.value=Number(result.revision||revision.value)
    logConfigApplied('Apply',file.value,revision.value)
    ElMessage.success('Revision '+revision.value+' applied')
  }finally{applying.value=false}
}

function buildReview(before:string,after:string):ReviewLine[]{
  const a=before.split('\n'),b=after.split('\n')
  const dp=Array.from({length:a.length+1},()=>Array<number>(b.length+1).fill(0))
  for(let i=a.length-1;i>=0;i--)for(let j=b.length-1;j>=0;j--)dp[i][j]=a[i]===b[j]?dp[i+1][j+1]+1:Math.max(dp[i+1][j],dp[i][j+1])
  const lines:ReviewLine[]=[]
  let i=0,j=0
  while(i<a.length||j<b.length){
    if(i<a.length&&j<b.length&&a[i]===b[j]){lines.push({type:'same',text:a[i]});i++;j++}
    else if(j<b.length&&(i===a.length||dp[i][j+1]>=dp[i+1][j])){lines.push({type:'add',text:b[j]});j++}
    else{lines.push({type:'remove',text:a[i]});i++}
  }
  return lines
}
const reviewLines=computed(()=>buildReview(appliedSnapshot[file.value],yamlFiles[file.value]))

// Import（§21）：真正读取上传文件文本；空文件失败；与当前 Working Copy 相同 → No Changes，
// 不同 → 复用 buildReview 生成真实 diff；Apply 后回写 yamlFiles + mock state + revision。
const importState=reactive<{target:string;name:string;text:string;validated:boolean;errors:string[];raw:File|null}>({
  target:'devices.yaml',name:'',text:'',validated:false,errors:[],raw:null,
})
function onImportChange(upload:{name?:string;raw?:File}){
  importState.name=upload.name||''
  importState.raw=upload.raw||null
  importState.text=''
  importState.validated=false
  importState.errors=[]
}
watch(()=>importState.target,()=>{importState.validated=false;importState.errors=[]})
const importNoChanges=computed(()=>importState.validated&&importState.text===yamlFiles[importState.target])
const importReviewLines=computed(()=>importState.validated?buildReview(yamlFiles[importState.target],importState.text):[])
const importImpacts=computed(()=>configApplyImpact(importState.target))
async function validateImport(){
  if(importValidating.value||importApplying.value)return
  if(!importState.name||!importState.raw){ElMessage.warning('Select a YAML file first');return}
  importValidating.value=true
  try{
    await sleep(LATENCY.uiLocal)
    const text=await importState.raw.text()
    importState.text=text
    if(!text.trim()){
      importState.errors=[importState.target+': uploaded file is empty']
      importState.validated=false
      ElMessage.error('Uploaded file is empty')
      return
    }
    const result=await validateConfigRemote(importState.target,text)
    importState.errors=result.errors
    importState.validated=result.ok
    if(!result.ok){ElMessage.error('Import validation failed');return}
    ElMessage.success(text===yamlFiles[importState.target]?'Import validated — no changes':'Import validated — diff ready')
  }finally{importValidating.value=false}
}
function closeImport(){importOpen.value=false;importState.name='';importState.text='';importState.validated=false;importState.errors=[];importState.raw=null}
async function applyImport(){
  if(!importState.validated||importNoChanges.value||importApplying.value||importValidating.value)return
  try{
    await ElMessageBox.confirm(
      'Apply the validated import as a new configuration revision? Impact: '+importImpacts.value.join('; ')+'.',
      'Apply Import',
      {type:'warning',confirmButtonText:'Apply Import'},
    )
  }catch{return}
  importApplying.value=true
  try{
    const result:any=await applyConfigRemote(importState.target,importState.text)
    yamlFiles[importState.target]=importState.text
    applyTextToState(importState.target,importState.text)
    appliedSnapshot[importState.target]=importState.text
    dirtyMap[importState.target]=false
    pushRevision('Import','Imported '+(importState.name||'uploaded YAML')+' → '+importState.target)
    revision.value=Number(result.revision||revision.value)
    logConfigApplied('Import',importState.target,revision.value)
    closeImport()
    ElMessage.success('Imported as revision '+revision.value)
  }finally{importApplying.value=false}
}

// Revision History（§18）：每个 revision 携带全量快照；Restore 以快照真正回写。
const history=ref<HistoryEntry[]>([])
{
  const base=snapshotOfApplied()
  // 旧版本快照：devices.yaml 中 wtg-025 还是旧 host，Restore 后可在 Devices 页看到真实变化
  const older=snapshotOfApplied()
  older['devices.yaml']=older['devices.yaml'].replace('host: "192.168.151.25"','host: "192.168.151.26"')
  history.value=[
    {revision:42,time:'2026-09-30 10:12:04',source:'Apply',comment:'Current applied configuration',status:'Applied',snapshot:base},
    {revision:41,time:'2026-09-29 23:18:51',source:'Edit',comment:'Previous working revision',status:'Archived',snapshot:older},
    {revision:40,time:'2026-09-29 18:06:33',source:'Import',comment:'ADS site configuration',status:'Archived',snapshot:{...older}},
    {revision:39,time:'2026-09-28 15:42:10',source:'Apply',comment:'Before point table update',status:'Archived',snapshot:{...older}},
  ]
}
async function restore(row:HistoryEntry){
  if(restoring.value||applying.value||importApplying.value)return
  try{
    await ElMessageBox.confirm(
      'Compare revision '+row.revision+' with current, validate impact and apply it as a new revision?',
      'Restore Revision',
      {type:'warning',confirmButtonText:'Review & Restore'},
    )
  }catch{return}
  restoring.value=true
  try{
    await restoreConfigRevision(row.revision)
    ElMessage.success('Revision '+row.revision+' restored')
    await refreshBackendHistory()
  }finally{restoring.value=false}
}

function downloadBlob(filename:string,blob:Blob){
  const url=URL.createObjectURL(blob)
  const anchor=document.createElement('a')
  anchor.href=url
  anchor.download=filename
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  URL.revokeObjectURL(url)
}
function downloadCurrent(){
  downloadBlob(file.value,new Blob([yamlFiles[file.value]],{type:'text/yaml;charset=utf-8'}))
}

function crc32(bytes:Uint8Array){
  let crc=0xffffffff
  for(const byte of bytes){
    crc^=byte
    for(let bit=0;bit<8;bit++)crc=(crc>>>1)^((crc&1)?0xedb88320:0)
  }
  return (crc^0xffffffff)>>>0
}
function concatBytes(parts:Uint8Array[]){
  const size=parts.reduce((sum,part)=>sum+part.length,0)
  const result=new Uint8Array(size)
  let offset=0
  for(const part of parts){result.set(part,offset);offset+=part.length}
  return result
}
function zipHeader(size:number){
  return new Uint8Array(size)
}
function createZip(files:Array<{name:string;content:string}>){
  const encoder=new TextEncoder()
  const locals:Uint8Array[]=[]
  const centrals:Uint8Array[]=[]
  let offset=0

  for(const fileEntry of files){
    const name=encoder.encode(fileEntry.name)
    const data=encoder.encode(fileEntry.content)
    const crc=crc32(data)

    const local=zipHeader(30+name.length)
    const localView=new DataView(local.buffer)
    localView.setUint32(0,0x04034b50,true)
    localView.setUint16(4,20,true)
    localView.setUint16(6,0x0800,true)
    localView.setUint16(8,0,true)
    localView.setUint32(14,crc,true)
    localView.setUint32(18,data.length,true)
    localView.setUint32(22,data.length,true)
    localView.setUint16(26,name.length,true)
    local.set(name,30)
    locals.push(local,data)

    const central=zipHeader(46+name.length)
    const centralView=new DataView(central.buffer)
    centralView.setUint32(0,0x02014b50,true)
    centralView.setUint16(4,20,true)
    centralView.setUint16(6,20,true)
    centralView.setUint16(8,0x0800,true)
    centralView.setUint16(10,0,true)
    centralView.setUint32(16,crc,true)
    centralView.setUint32(20,data.length,true)
    centralView.setUint32(24,data.length,true)
    centralView.setUint16(28,name.length,true)
    centralView.setUint32(42,offset,true)
    central.set(name,46)
    centrals.push(central)

    offset+=local.length+data.length
  }

  const centralBytes=concatBytes(centrals)
  const end=zipHeader(22)
  const endView=new DataView(end.buffer)
  endView.setUint32(0,0x06054b50,true)
  endView.setUint16(8,files.length,true)
  endView.setUint16(10,files.length,true)
  endView.setUint32(12,centralBytes.length,true)
  endView.setUint32(16,offset,true)

  const archive=concatBytes([...locals,centralBytes,end])
  const buffer=new ArrayBuffer(archive.byteLength)
  new Uint8Array(buffer).set(archive)
  return new Blob([buffer],{type:'application/zip'})
}
async function createBackup(){
  const stamp=new Date().toISOString().replace(/[-:]/g,'').replace('T','_').slice(0,15)
  const archive=await downloadConfigBackup()
  downloadBlob('wind-hub-config-backup_'+stamp+'.zip',archive)
  ElMessage.success('Configuration backup downloaded')
}
async function refreshBackendHistory(){
  const rows=await loadConfigHistory()
  if(!rows.length)return
  revision.value=Math.max(...rows.map(row=>Number(row.revision)))
  history.value=rows.map((row,index)=>({
    revision:Number(row.revision),
    time:row.created_at,
    source:'Backend',
    comment:row.message,
    status:index===0?'Applied':'Archived',
    snapshot:snapshotOfApplied(),
  }))
}
void refreshBackendHistory()
function handleAction(command:string){
  if(command==='import')importOpen.value=true
  else if(command==='download-current')downloadCurrent()
  else if(command==='backup')void createBackup()
}
</script>

<template>
  <div class="config-page">
    <div class="head">
      <div><h1>Configuration Files</h1><p>YAML 查询、修改、Import + Diff、单文件下载、完整配置备份与历史版本。</p></div>
      <div class="head-actions">
        <el-tag type="info">Revision {{revision}}</el-tag>
        <el-dropdown @command="handleAction">
          <el-button>Actions ▾</el-button>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item command="import">Import</el-dropdown-item>
              <el-dropdown-item command="download-current">Download Current File</el-dropdown-item>
              <el-dropdown-item divided command="backup">Create Backup (.zip)</el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
      </div>
    </div>

    <el-tabs v-model="section" class="config-tabs">
      <el-tab-pane label="Files" name="files">
        <div class="section-heading">
          <div><h2>Configuration Workspace</h2><p>Applied → Working Copy；编辑、Changes、Validate 与 Apply 使用统一主工作区。</p></div>
          <el-tag :type="dirtyMap[file]?'warning':'success'">{{dirtyMap[file]?'Pending Apply':'Applied'}}</el-tag>
        </div>

        <el-card shadow="never">
          <div class="config-editor-layout">
            <aside class="config-nav">
              <span class="config-nav-title">Files</span>
              <el-button v-for="name in CONFIG_FILES" :key="name" text class="config-nav-button" :class="{active:file===name}" :disabled="applying" @click="file=name">
                {{name}}<el-badge v-if="dirtyMap[name]" is-dot type="warning"/>
              </el-button>
            </aside>

            <main class="yaml-workspace">
              <div class="yaml-toolbar">
                <div class="yaml-title"><b>{{file}}</b><span>{{dirtyMap[file]?'Working copy differs from applied revision':'Matches applied revision'}}</span></div>
                <el-segmented v-model="editorMode" :options="[{label:'Edit',value:'edit'},{label:'Changes',value:'review'}]"/>
              </div>

              <el-input v-if="editorMode==='edit'" v-model="yamlFiles[file]" class="yaml-input" type="textarea" :rows="25" @input="markDirty"/>
              <div v-else class="review-editor">
                <div v-for="(line,index) in reviewLines" :key="index" :class="['review-line',line.type]">
                  <span class="review-gutter">{{line.type==='add'?'+':line.type==='remove'?'−':''}}</span>
                  <code>{{line.text||' '}}</code>
                </div>
              </div>

              <div class="yaml-actions">
                <el-button :loading="validating" :disabled="applying" @click="validate">Validate</el-button>
                <el-button type="primary" :loading="applying" :disabled="!dirtyMap[file] || validating || applying" @click="apply">Apply</el-button>
              </div>
            </main>
          </div>
        </el-card>
      </el-tab-pane>

      <el-tab-pane label="History" name="history">
        <div class="section-heading">
          <div><h2>Configuration History</h2><p>History 记录 Apply / Import / Restore 产生的 Revision；Create Backup 不修改 Revision。</p></div>
        </div>
        <el-card shadow="never">
          <el-table :data="history">
            <el-table-column prop="revision" label="Revision" width="100"/>
            <el-table-column prop="time" label="Created At" width="180"/>
            <el-table-column prop="source" label="Source" width="110"/>
            <el-table-column prop="comment" label="Comment" min-width="220"/>
            <el-table-column prop="status" label="Status" width="110"/>
            <el-table-column label="Operation" width="140"><template #default="{row}"><el-button link type="primary" :loading="restoring" :disabled="restoring || applying || importApplying" @click="restore(row)">Compare / Restore</el-button></template></el-table-column>
          </el-table>
        </el-card>
      </el-tab-pane>
    </el-tabs>

    <el-drawer v-model="importOpen" title="Import Configuration" size="var(--app-drawer-width-sm)" @closed="closeImport">
      <el-form label-position="top">
        <el-form-item label="Target File">
          <el-select v-model="importState.target" class="app-full-width">
            <el-option v-for="name in CONFIG_FILES" :key="name" :label="name" :value="name"/>
          </el-select>
        </el-form-item>
        <!-- 不设 limit：连续选择不同文件时以最后一次选择为准（importState 只保留单个文件） -->
        <el-upload drag action="#" :auto-upload="false" :on-change="onImportChange">
          <div>Drop YAML here or click to select</div>
          <small>Upload is staged only; current Applied configuration is unchanged.</small>
        </el-upload>
      </el-form>

      <div class="drawer-section">
        <div class="drawer-section-head"><h3>Validation</h3><el-button type="primary" :loading="importValidating" :disabled="!importState.name || importApplying" @click="validateImport">Validate & Compare</el-button></div>
        <el-empty v-if="!importState.name" description="Select a YAML file to continue"/>
        <template v-else-if="importState.errors.length">
          <el-alert type="error" :closable="false" title="Import validation failed"/>
          <pre class="diff-preview">{{importState.errors.join('\n')}}</pre>
        </template>
        <el-empty v-else-if="!importState.validated" description="Validate the uploaded YAML to continue"/>
        <el-alert v-else-if="importNoChanges" type="info" :closable="false" title="No Changes — uploaded YAML matches the current working copy"/>
        <template v-else>
          <el-alert type="success" :closable="false" title="Syntax, schema and reference validation passed"/>
          <h3>Changes</h3>
          <div class="review-editor import-review">
            <div v-for="(line,index) in importReviewLines" :key="index" :class="['review-line',line.type]">
              <span class="review-gutter">{{line.type==='add'?'+':line.type==='remove'?'−':''}}</span>
              <code>{{line.text||' '}}</code>
            </div>
          </div>
          <h3>Impact</h3>
          <el-descriptions :column="1" border>
            <el-descriptions-item label="Target">{{importState.target}}</el-descriptions-item>
            <el-descriptions-item label="Impact">{{importImpacts.join('; ')}}</el-descriptions-item>
          </el-descriptions>
        </template>
      </div>

      <template #footer>
        <div class="drawer-footer">
          <el-button :disabled="importApplying" @click="importOpen=false">Cancel</el-button>
          <el-button type="primary" :loading="importApplying" :disabled="!importState.validated || importNoChanges || importValidating || importApplying" @click="applyImport">Apply Import</el-button>
        </div>
      </template>
    </el-drawer>
  </div>
</template>

<style scoped>
.head-actions{display:flex;align-items:center;gap:var(--app-space-2)}.config-tabs{margin-top:var(--app-space-2)}.section-heading{display:flex;align-items:flex-end;justify-content:space-between;gap:var(--app-space-4);margin-bottom:var(--app-space-3)}.section-heading h2{margin:0;font-size:var(--app-font-section-title);font-weight:var(--app-font-weight-semibold)}.section-heading p,.yaml-title span{margin:var(--app-space-1) 0 0;color:var(--app-text-muted);font-size:var(--app-font-caption)}
.config-editor-layout{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,5fr);gap:var(--app-space-4)}.config-nav{display:flex;flex-direction:column;gap:var(--app-space-1);padding-right:var(--app-space-3);border-right:1px solid var(--app-border-soft)}.config-nav-title{padding:0 var(--app-space-2);color:var(--app-text-muted);font-size:var(--app-font-caption);font-weight:var(--app-font-weight-semibold);text-transform:uppercase}.config-nav-button{display:flex;align-items:center;justify-content:space-between;width:100%;padding:var(--app-space-2);border:1px solid transparent;border-radius:var(--app-control-radius);background:transparent;color:var(--app-text-primary);text-align:left;cursor:pointer}.config-nav-button:hover{background:var(--el-fill-color-light)}.config-nav-button.active{background:var(--el-color-primary-light-9);border-color:var(--el-color-primary-light-7);color:var(--el-color-primary)}
.yaml-workspace{min-width:0}.yaml-toolbar,.yaml-actions,.yaml-title{display:flex;align-items:center}.yaml-toolbar{justify-content:space-between;gap:var(--app-space-3);margin-bottom:var(--app-space-3)}.yaml-title{align-items:flex-start;flex-direction:column;gap:var(--app-space-1)}.yaml-input :deep(textarea){font-family:monospace;line-height:var(--app-line-height-body)}.yaml-actions{justify-content:flex-end;gap:var(--app-space-2);margin-top:var(--app-space-3)}
.review-editor{border:1px solid var(--app-border-soft);border-radius:var(--app-control-radius);background:var(--el-bg-color);padding:var(--app-space-2) 0;font-family:monospace;line-height:var(--app-line-height-body)}.review-line{display:grid;grid-template-columns:var(--app-space-6) minmax(0,1fr);border-left:3px solid transparent}.review-line code{padding:var(--app-space-1) var(--app-space-2);white-space:pre-wrap;overflow-wrap:anywhere}.review-gutter{text-align:center;color:var(--app-text-muted)}.review-line.add{background:var(--el-color-success-light-9);border-left-color:var(--el-color-success)}.review-line.remove{background:var(--el-color-danger-light-9);border-left-color:var(--el-color-danger)}
.import-review{max-height:320px;overflow:auto}
.drawer-section{margin-top:var(--app-space-6)}.drawer-section-head{display:flex;align-items:center;justify-content:space-between;gap:var(--app-space-3)}.drawer-section h3{margin:var(--app-space-4) 0 var(--app-space-2);font-size:var(--app-font-panel-title);font-weight:var(--app-font-weight-semibold)}.diff-preview{white-space:pre-wrap;padding:var(--app-space-3);border:1px solid var(--app-border-soft);border-radius:var(--app-control-radius);background:var(--el-fill-color-extra-light);font-family:monospace}.drawer-footer{display:flex;justify-content:flex-end;gap:var(--app-space-2)}
@media(max-width:1199px){.config-editor-layout{grid-template-columns:1fr}.config-nav{flex-direction:row;overflow-x:auto;border-right:0;border-bottom:1px solid var(--app-border-soft);padding:0 0 var(--app-space-3)}.config-nav-title{display:none}.config-nav-button{min-width:max-content}}
@media(max-width:767px){.section-heading,.yaml-toolbar{align-items:flex-start;flex-direction:column}.yaml-actions{flex-wrap:wrap}.yaml-actions .el-button{flex:1;margin-left:0!important}.head-actions{width:100%;justify-content:space-between}}
</style>