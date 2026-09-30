<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { CONFIG_FILES, yamlDiffs, yamlFiles } from '../mock/yaml'

type ReviewLine={type:'same'|'add'|'remove';text:string}

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
const savedSnapshot=reactive<Record<string,string>>(
  Object.fromEntries(CONFIG_FILES.map(name=>[name,yamlFiles[name]])),
)
const dirtyMap=reactive<Record<string,boolean>>({})

function markDirty(){dirtyMap[file.value]=yamlFiles[file.value]!==appliedSnapshot[file.value]}
async function validate(){
  if(validating.value||applying.value)return false
  validating.value=true
  try{
    await new Promise(resolve=>setTimeout(resolve,200))
    if(!yamlFiles[file.value].trim()){ElMessage.error(file.value+' is empty');return false}
    ElMessage.success(file.value+' validation passed (mock)')
    return true
  }finally{validating.value=false}
}
function saveDraft(){
  savedSnapshot[file.value]=yamlFiles[file.value]
  dirtyMap[file.value]=yamlFiles[file.value]!==appliedSnapshot[file.value]
  ElMessage.success(dirtyMap[file.value]?file.value+' saved — pending apply (mock)':file.value+' saved (mock)')
}
async function apply(){
  if(applying.value||validating.value||!dirtyMap[file.value])return
  if(!await validate())return
  if(yamlFiles[file.value]!==appliedSnapshot[file.value]){
    await ElMessageBox.confirm(
      'The backend will validate, build diff and impact, then apply only affected runtime objects.',
      'Apply Configuration',
      {type:'warning',confirmButtonText:'Apply'},
    )
  }
  applying.value=true
  try{
    await new Promise(resolve=>setTimeout(resolve,350))
    savedSnapshot[file.value]=yamlFiles[file.value]
    appliedSnapshot[file.value]=yamlFiles[file.value]
    dirtyMap[file.value]=false
    revision.value+=1
    ElMessage.success('Revision '+revision.value+' applied (mock)')
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

const importState=reactive({target:'devices.yaml',name:'',validated:false})
function onImportChange(upload:{name?:string}){importState.name=upload.name||'';importState.validated=false}
async function validateImport(){
  if(importValidating.value||importApplying.value)return
  if(!importState.name){ElMessage.warning('Select a YAML file first');return}
  importValidating.value=true
  try{
    await new Promise(resolve=>setTimeout(resolve,200))
    importState.validated=true
    ElMessage.success('Import validated — diff ready (mock)')
  }finally{importValidating.value=false}
}
function closeImport(){importOpen.value=false;importState.name='';importState.validated=false}
async function applyImport(){
  if(!importState.validated||importApplying.value||importValidating.value)return
  await ElMessageBox.confirm(
    'Apply the validated import as a new configuration revision?',
    'Apply Import',
    {type:'warning',confirmButtonText:'Apply Import'},
  )
  importApplying.value=true
  try{
    await new Promise(resolve=>setTimeout(resolve,350))
    revision.value+=1
    closeImport()
    ElMessage.success('Imported as revision '+revision.value+' (mock)')
  }finally{importApplying.value=false}
}

const history=ref([
  {revision:42,time:'2026-09-30 10:12:04',source:'Apply',comment:'Current applied configuration',status:'Applied'},
  {revision:41,time:'2026-09-29 23:18:51',source:'Edit',comment:'Previous working revision',status:'Archived'},
  {revision:40,time:'2026-09-29 18:06:33',source:'Import',comment:'ADS site configuration',status:'Archived'},
  {revision:39,time:'2026-09-28 15:42:10',source:'Apply',comment:'Before point table update',status:'Archived'},
])
async function restore(row:{revision:number}){
  if(restoring.value||applying.value||importApplying.value)return
  await ElMessageBox.confirm(
    'Compare revision '+row.revision+' with current, validate impact and apply it as a new revision?',
    'Restore Revision',
    {type:'warning',confirmButtonText:'Review & Restore'},
  )
  restoring.value=true
  try{
    await new Promise(resolve=>setTimeout(resolve,350))
    revision.value+=1
    ElMessage.success('Restored as new revision '+revision.value+' (mock)')
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

  return new Blob([...locals,centralBytes,end],{type:'application/zip'})
}
function createBackup(){
  const stamp=new Date().toISOString().replace(/[-:]/g,'').replace('T','_').slice(0,15)
  const archive=createZip(CONFIG_FILES.map(name=>({name,content:yamlFiles[name]})))
  downloadBlob('wind-hub-config-backup_'+stamp+'.zip',archive)
  ElMessage.success('Configuration backup downloaded')
}
function handleAction(command:string){
  if(command==='import')importOpen.value=true
  else if(command==='download-current')downloadCurrent()
  else if(command==='backup')createBackup()
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
                <el-button :disabled="applying" @click="saveDraft">Save Draft</el-button>
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

    <el-drawer v-model="importOpen" title="Import Configuration" size="560px" @closed="closeImport">
      <el-form label-position="top">
        <el-form-item label="Target File">
          <el-select v-model="importState.target" class="app-full-width">
            <el-option v-for="name in CONFIG_FILES" :key="name" :label="name" :value="name"/>
          </el-select>
        </el-form-item>
        <el-upload drag action="#" :auto-upload="false" :limit="1" :on-change="onImportChange">
          <div>Drop YAML here or click to select</div>
          <small>Upload is staged only; current Applied configuration is unchanged.</small>
        </el-upload>
      </el-form>

      <div class="drawer-section">
        <div class="drawer-section-head"><h3>Validation</h3><el-button type="primary" :loading="importValidating" :disabled="!importState.name || importApplying" @click="validateImport">Validate & Compare</el-button></div>
        <el-empty v-if="!importState.validated" description="Validate the uploaded YAML to continue"/>
        <template v-else>
          <el-alert type="success" :closable="false" title="Syntax, schema and reference validation passed (mock)"/>
          <h3>Changes</h3>
          <pre class="diff-preview">{{yamlDiffs[importState.target]}}</pre>
          <h3>Impact</h3>
          <el-descriptions :column="1" border>
            <el-descriptions-item label="Target">{{importState.target}}</el-descriptions-item>
            <el-descriptions-item label="Runtime">Related objects only</el-descriptions-item>
            <el-descriptions-item label="Running Tasks">Re-evaluated before Apply</el-descriptions-item>
          </el-descriptions>
        </template>
      </div>

      <template #footer>
        <div class="drawer-footer">
          <el-button :disabled="importApplying" @click="importOpen=false">Cancel</el-button>
          <el-button type="primary" :loading="importApplying" :disabled="!importState.validated || importValidating || importApplying" @click="applyImport">Apply Import</el-button>
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
.drawer-section{margin-top:var(--app-space-6)}.drawer-section-head{display:flex;align-items:center;justify-content:space-between;gap:var(--app-space-3)}.drawer-section h3{margin:var(--app-space-4) 0 var(--app-space-2);font-size:var(--app-font-panel-title);font-weight:var(--app-font-weight-semibold)}.diff-preview{white-space:pre-wrap;padding:var(--app-space-3);border:1px solid var(--app-border-soft);border-radius:var(--app-control-radius);background:var(--el-fill-color-extra-light);font-family:monospace}.drawer-footer{display:flex;justify-content:flex-end;gap:var(--app-space-2)}
@media(max-width:1199px){.config-editor-layout{grid-template-columns:1fr}.config-nav{flex-direction:row;overflow-x:auto;border-right:0;border-bottom:1px solid var(--app-border-soft);padding:0 0 var(--app-space-3)}.config-nav-title{display:none}.config-nav-button{min-width:max-content}}
@media(max-width:767px){.section-heading,.yaml-toolbar{align-items:flex-start;flex-direction:column}.yaml-actions{flex-wrap:wrap}.yaml-actions .el-button{flex:1;margin-left:0!important}.head-actions{width:100%;justify-content:space-between}}
</style>