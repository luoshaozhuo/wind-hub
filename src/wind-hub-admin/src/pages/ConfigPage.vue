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

const appliedSnapshot=reactive<Record<string,string>>(
  Object.fromEntries(CONFIG_FILES.map(name=>[name,yamlFiles[name]])),
)
const savedSnapshot=reactive<Record<string,string>>(
  Object.fromEntries(CONFIG_FILES.map(name=>[name,yamlFiles[name]])),
)
const dirtyMap=reactive<Record<string,boolean>>({})

function markDirty(){
  dirtyMap[file.value]=yamlFiles[file.value]!==appliedSnapshot[file.value]
}
function validate(){
  if(!yamlFiles[file.value].trim()){
    ElMessage.error(file.value+' is empty')
    return false
  }
  ElMessage.success(file.value+' validation passed (mock)')
  return true
}
function saveDraft(){
  savedSnapshot[file.value]=yamlFiles[file.value]
  dirtyMap[file.value]=yamlFiles[file.value]!==appliedSnapshot[file.value]
  ElMessage.success(dirtyMap[file.value]?file.value+' saved — pending apply (mock)':file.value+' saved (mock)')
}
async function apply(){
  if(!validate())return
  if(yamlFiles[file.value]!==appliedSnapshot[file.value]){
    await ElMessageBox.confirm(
      'The backend will validate, build diff and impact, then apply only affected runtime objects.',
      'Apply Configuration',
      {type:'warning',confirmButtonText:'Apply'},
    )
  }
  savedSnapshot[file.value]=yamlFiles[file.value]
  appliedSnapshot[file.value]=yamlFiles[file.value]
  dirtyMap[file.value]=false
  revision.value+=1
  ElMessage.success('Revision '+revision.value+' applied (mock)')
}
function buildReview(before:string,after:string):ReviewLine[]{
  const a=before.split('\n')
  const b=after.split('\n')
  const dp=Array.from({length:a.length+1},()=>Array<number>(b.length+1).fill(0))
  for(let i=a.length-1;i>=0;i--){
    for(let j=b.length-1;j>=0;j--){
      dp[i][j]=a[i]===b[j]?dp[i+1][j+1]+1:Math.max(dp[i+1][j],dp[i][j+1])
    }
  }
  const lines:ReviewLine[]=[]
  let i=0,j=0
  while(i<a.length||j<b.length){
    if(i<a.length&&j<b.length&&a[i]===b[j]){
      lines.push({type:'same',text:a[i]});i++;j++
    }else if(j<b.length&&(i===a.length||dp[i][j+1]>=dp[i+1][j])){
      lines.push({type:'add',text:b[j]});j++
    }else{
      lines.push({type:'remove',text:a[i]});i++
    }
  }
  return lines
}
const reviewLines=computed(()=>buildReview(appliedSnapshot[file.value],yamlFiles[file.value]))

const importState=reactive({target:'devices.yaml',name:'',validated:false})
function onImportChange(upload:{name?:string}){
  importState.name=upload.name||''
  importState.validated=false
}
function validateImport(){
  if(!importState.name){
    ElMessage.warning('Select a YAML file first')
    return
  }
  importState.validated=true
  ElMessage.success('Import validated — diff ready (mock)')
}
function closeImport(){
  importOpen.value=false
  importState.name=''
  importState.validated=false
}
async function applyImport(){
  if(!importState.validated)return
  revision.value+=1
  closeImport()
  ElMessage.success('Imported as revision '+revision.value+' (mock)')
}

const history=ref([
  {revision:42,time:'2026-09-30 10:12:04',source:'Apply',comment:'Current applied configuration',status:'Applied'},
  {revision:41,time:'2026-09-29 23:18:51',source:'Edit',comment:'Previous working revision',status:'Archived'},
  {revision:40,time:'2026-09-29 18:06:33',source:'Import',comment:'ADS site configuration',status:'Archived'},
  {revision:39,time:'2026-09-28 15:42:10',source:'Backup',comment:'Before point table update',status:'Archived'},
])
function createBackup(){
  history.value.unshift({
    revision:revision.value,
    time:new Date().toLocaleString(),
    source:'Backup',
    comment:'Manual backup of current revision',
    status:'Archived',
  })
  ElMessage.success('Backup created (mock)')
}
async function restore(row:{revision:number}){
  await ElMessageBox.confirm(
    'Compare revision '+row.revision+' with current, validate impact and apply it as a new revision?',
    'Restore Revision',
    {type:'warning',confirmButtonText:'Review & Restore'},
  )
  revision.value+=1
  ElMessage.success('Restored as new revision '+revision.value+' (mock)')
}

function downloadText(filename:string,content:string,type='text/yaml;charset=utf-8'){
  const blob=new Blob([content],{type})
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
  downloadText(file.value,yamlFiles[file.value])
}
function downloadConfigSet(){
  const parts=CONFIG_FILES.map(name=>'# ===== '+name+' =====\n'+yamlFiles[name])
  downloadText('wind-hub-config-set.yaml',parts.join('\n\n'))
}
function handleAction(command:string){
  if(command==='import')importOpen.value=true
  else if(command==='download-current')downloadCurrent()
  else if(command==='download-set')downloadConfigSet()
  else if(command==='backup')createBackup()
}
</script>

<template>
  <div class="config-page">
    <div class="head">
      <div>
        <h1>Configuration Files</h1>
        <p>YAML 查询、修改、Import + Diff、Download、Backup 与历史版本。</p>
      </div>
      <div class="head-actions">
        <el-tag type="info">Revision {{revision}}</el-tag>
        <el-dropdown @command="handleAction">
          <el-button>Actions ▾</el-button>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item command="import">Import</el-dropdown-item>
              <el-dropdown-item command="download-current">Download Current File</el-dropdown-item>
              <el-dropdown-item command="download-set">Download Config Set</el-dropdown-item>
              <el-dropdown-item divided command="backup">Create Backup</el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
      </div>
    </div>

    <el-tabs v-model="section" class="config-tabs">
      <el-tab-pane label="Files" name="files">
        <div class="section-heading">
          <div>
            <h2>Configuration Workspace</h2>
            <p>Applied → Working Copy；编辑、Changes、Validate 与 Apply 使用统一主工作区。</p>
          </div>
          <el-tag :type="dirtyMap[file]?'warning':'success'">{{dirtyMap[file]?'Pending Apply':'Applied'}}</el-tag>
        </div>

        <el-card shadow="never">
          <div class="config-editor-layout">
            <aside class="config-nav">
              <span class="config-nav-title">Files</span>
              <button v-for="name in CONFIG_FILES" :key="name" type="button" :class="{active:file===name}" @click="file=name">
                {{name}}<el-badge v-if="dirtyMap[name]" is-dot type="warning"/>
              </button>
            </aside>

            <main class="yaml-workspace">
              <div class="yaml-toolbar">
                <div class="yaml-title">
                  <b>{{file}}</b>
                  <span>{{dirtyMap[file]?'Working copy differs from applied revision':'Matches applied revision'}}</span>
                </div>
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
                <el-button @click="validate">Validate</el-button>
                <el-button @click="saveDraft">Save Draft</el-button>
                <el-button type="primary" @click="apply">Apply</el-button>
              </div>
            </main>
          </div>
        </el-card>
      </el-tab-pane>

      <el-tab-pane label="History" name="history">
        <div class="section-heading">
          <div><h2>Configuration History</h2><p>Backup 是内部可恢复 revision；Restore 会作为新 revision 应用。</p></div>
          <el-button @click="createBackup">Create Backup</el-button>
        </div>
        <el-card shadow="never">
          <el-table :data="history">
            <el-table-column prop="revision" label="Revision" width="100"/>
            <el-table-column prop="time" label="Created At" width="180"/>
            <el-table-column prop="source" label="Source" width="110"/>
            <el-table-column prop="comment" label="Comment" min-width="220"/>
            <el-table-column prop="status" label="Status" width="110"/>
            <el-table-column label="Operation" width="140">
              <template #default="{row}"><el-button link type="primary" @click="restore(row)">Compare / Restore</el-button></template>
            </el-table-column>
          </el-table>
        </el-card>
      </el-tab-pane>
    </el-tabs>

    <el-drawer v-model="importOpen" title="Import Configuration" size="min(560px, 100%)" @closed="closeImport">
      <el-form label-position="top">
        <el-form-item label="Target File">
          <el-select v-model="importState.target" style="width:100%">
            <el-option v-for="name in CONFIG_FILES" :key="name" :label="name" :value="name"/>
          </el-select>
        </el-form-item>
        <el-upload drag action="#" :auto-upload="false" :limit="1" :on-change="onImportChange">
          <div>Drop YAML here or click to select</div>
          <small>Upload is staged only; current Applied configuration is unchanged.</small>
        </el-upload>
      </el-form>

      <div class="drawer-section">
        <div class="drawer-section-head"><h3>Validation</h3><el-button type="primary" :disabled="!importState.name" @click="validateImport">Validate & Compare</el-button></div>
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
          <el-button @click="importOpen=false">Cancel</el-button>
          <el-button type="primary" :disabled="!importState.validated" @click="applyImport">Apply Import</el-button>
        </div>
      </template>
    </el-drawer>
  </div>
</template>

<style scoped>
.head-actions{display:flex;align-items:center;gap:var(--app-space-2)}.config-tabs{margin-top:var(--app-space-2)}.section-heading{display:flex;align-items:flex-end;justify-content:space-between;gap:var(--app-space-4);margin-bottom:var(--app-space-3)}.section-heading h2{margin:0;font-size:var(--app-font-section-title)}.section-heading p,.yaml-title span{margin:var(--app-space-1) 0 0;color:var(--app-text-muted);font-size:var(--app-font-caption)}
.config-editor-layout{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,5fr);gap:var(--app-space-4)}.config-nav{display:flex;flex-direction:column;gap:var(--app-space-1);padding-right:var(--app-space-3);border-right:1px solid var(--app-border-soft)}.config-nav-title{padding:0 var(--app-space-2);color:var(--app-text-muted);font-size:var(--app-font-caption);font-weight:var(--app-font-weight-semibold);text-transform:uppercase}.config-nav button{display:flex;align-items:center;justify-content:space-between;width:100%;padding:var(--app-space-2);border:1px solid transparent;border-radius:var(--app-control-radius);background:transparent;color:var(--app-text-primary);text-align:left;cursor:pointer}.config-nav button:hover{background:var(--el-fill-color-light)}.config-nav button.active{background:var(--el-color-primary-light-9);border-color:var(--el-color-primary-light-7);color:var(--el-color-primary)}
.yaml-workspace{min-width:0}.yaml-toolbar,.yaml-actions,.yaml-title{display:flex;align-items:center}.yaml-toolbar{justify-content:space-between;gap:var(--app-space-3);margin-bottom:var(--app-space-3)}.yaml-title{align-items:flex-start;flex-direction:column;gap:var(--app-space-1)}.yaml-input :deep(textarea){font-family:monospace;line-height:var(--app-line-height-body)}.yaml-actions{justify-content:flex-end;gap:var(--app-space-2);margin-top:var(--app-space-3)}
.review-editor{border:1px solid var(--app-border-soft);border-radius:var(--app-control-radius);background:var(--el-bg-color);padding:var(--app-space-2) 0;font-family:monospace;line-height:var(--app-line-height-body)}.review-line{display:grid;grid-template-columns:var(--app-space-6) minmax(0,1fr);border-left:3px solid transparent}.review-line code{padding:var(--app-space-1) var(--app-space-2);white-space:pre-wrap;overflow-wrap:anywhere}.review-gutter{text-align:center;color:var(--app-text-muted)}.review-line.add{background:var(--el-color-success-light-9);border-left-color:var(--el-color-success)}.review-line.remove{background:var(--el-color-danger-light-9);border-left-color:var(--el-color-danger)}
.drawer-section{margin-top:var(--app-space-5)}.drawer-section-head{display:flex;align-items:center;justify-content:space-between;gap:var(--app-space-3)}.drawer-section h3{margin:var(--app-space-4) 0 var(--app-space-2);font-size:var(--app-font-panel-title)}.diff-preview{white-space:pre-wrap;padding:var(--app-space-3);border:1px solid var(--app-border-soft);border-radius:var(--app-control-radius);background:var(--el-fill-color-extra-light);font-family:monospace}.drawer-footer{display:flex;justify-content:flex-end;gap:var(--app-space-2)}
@media(max-width:1000px){.config-editor-layout{grid-template-columns:1fr}.config-nav{display:flex;flex-direction:row;overflow-x:auto;border-right:0;border-bottom:1px solid var(--app-border-soft);padding:0 0 var(--app-space-3)}.config-nav-title{display:none}.config-nav button{min-width:max-content}}
@media(max-width:767px){.section-heading,.yaml-toolbar{align-items:flex-start;flex-direction:column}.yaml-actions{flex-wrap:wrap}.yaml-actions .el-button{flex:1;margin-left:0!important}.head-actions{width:100%;justify-content:space-between}}
</style>