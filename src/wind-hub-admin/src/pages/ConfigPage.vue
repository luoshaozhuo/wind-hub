<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { CONFIG_FILES, yamlDiffs, yamlFiles } from '../mock/yaml'

type View = 'workspace' | 'import' | 'history'
type ReviewLine = { type:'same'|'add'|'remove'; text:string }

const view = ref<View>('workspace')
const file = ref('devices.yaml')
const editorMode = ref<'edit'|'review'>('review')
const revision = ref(42)

const appliedSnapshot = reactive<Record<string,string>>(
  Object.fromEntries(CONFIG_FILES.map(name => [name, yamlFiles[name]])),
)
const savedSnapshot = reactive<Record<string,string>>(
  Object.fromEntries(CONFIG_FILES.map(name => [name, yamlFiles[name]])),
)
const dirtyMap = reactive<Record<string,boolean>>({})

function selectFile(name:string) {
  file.value = name
}
function markDirty() {
  dirtyMap[file.value] = yamlFiles[file.value] !== appliedSnapshot[file.value]
}
function validate() {
  if (!yamlFiles[file.value].trim()) {
    ElMessage.error(file.value + ' is empty')
    return false
  }
  ElMessage.success(file.value + ' validation passed (mock)')
  return true
}
function saveDraft() {
  savedSnapshot[file.value] = yamlFiles[file.value]
  dirtyMap[file.value] = yamlFiles[file.value] !== appliedSnapshot[file.value]
  ElMessage.success(dirtyMap[file.value] ? file.value + ' saved — pending apply (mock)' : file.value + ' saved (mock)')
}
async function apply() {
  if (!validate()) return
  if (yamlFiles[file.value] !== appliedSnapshot[file.value]) {
    await ElMessageBox.confirm(
      'The backend will validate, build diff and impact, then apply only the affected runtime objects.',
      'Apply Configuration',
      { type:'warning', confirmButtonText:'Apply' },
    )
  }
  savedSnapshot[file.value] = yamlFiles[file.value]
  appliedSnapshot[file.value] = yamlFiles[file.value]
  dirtyMap[file.value] = false
  revision.value += 1
  ElMessage.success('Revision ' + revision.value + ' applied (mock)')
}

function buildReview(before:string, after:string):ReviewLine[] {
  const a = before.split('\n')
  const b = after.split('\n')
  const dp = Array.from({length:a.length+1}, () => Array<number>(b.length+1).fill(0))
  for (let i=a.length-1;i>=0;i--) {
    for (let j=b.length-1;j>=0;j--) {
      dp[i][j] = a[i]===b[j] ? dp[i+1][j+1]+1 : Math.max(dp[i+1][j],dp[i][j+1])
    }
  }
  const lines:ReviewLine[]=[]
  let i=0,j=0
  while(i<a.length||j<b.length) {
    if(i<a.length&&j<b.length&&a[i]===b[j]) {
      lines.push({type:'same',text:a[i]}); i++; j++
    } else if(j<b.length&&(i===a.length||dp[i][j+1]>=dp[i+1][j])) {
      lines.push({type:'add',text:b[j]}); j++
    } else {
      lines.push({type:'remove',text:a[i]}); i++
    }
  }
  return lines
}
const reviewLines = computed(() => buildReview(appliedSnapshot[file.value], yamlFiles[file.value]))

const importState = reactive({
  target:'devices.yaml',
  name:'',
  validated:false,
})
function onImportChange(f:{name?:string}) {
  importState.name = f.name || ''
  importState.validated = false
}
function validateImport() {
  if (!importState.name) {
    ElMessage.warning('Select a YAML file first')
    return
  }
  importState.validated = true
  ElMessage.success('Import validated — diff ready (mock)')
}
async function applyImport() {
  if (!importState.validated) return
  await ElMessageBox.confirm(
    'Apply imported configuration after diff and impact validation?',
    'Apply Imported Configuration',
    { type:'warning', confirmButtonText:'Apply' },
  )
  revision.value += 1
  importState.name = ''
  importState.validated = false
  view.value = 'workspace'
  ElMessage.success('Imported as revision ' + revision.value + ' (mock)')
}

const history = ref([
  { revision:42, time:'2026-09-30 10:12:04', source:'Apply', comment:'Current applied configuration', status:'Applied' },
  { revision:41, time:'2026-09-29 23:18:51', source:'Edit', comment:'Previous working revision', status:'Archived' },
  { revision:40, time:'2026-09-29 18:06:33', source:'Import', comment:'ADS site configuration', status:'Archived' },
  { revision:39, time:'2026-09-28 15:42:10', source:'Backup', comment:'Before point table update', status:'Archived' },
])
function createBackup() {
  history.value.unshift({
    revision:revision.value,
    time:new Date().toLocaleString(),
    source:'Backup',
    comment:'Manual backup of current revision',
    status:'Archived',
  })
  ElMessage.success('Backup created (mock)')
}
async function restore(row:{revision:number}) {
  await ElMessageBox.confirm(
    'Compare revision ' + row.revision + ' with current, validate impact and apply it as a new revision?',
    'Restore Revision',
    { type:'warning', confirmButtonText:'Review & Restore' },
  )
  revision.value += 1
  ElMessage.success('Restored as new revision ' + revision.value + ' (mock)')
}
function handleAction(command:string) {
  if (command==='import') view.value='import'
  else if (command==='history') view.value='history'
  else if (command==='backup') createBackup()
  else if (command==='export') ElMessage.success('Configuration export prepared (mock)')
}
</script>

<template>
  <div class="config-page">
    <div class="head">
      <div>
        <h1>Configuration Files</h1>
        <p>查询、编辑、Import + Diff、备份和历史版本；结构化系统参数请使用 System Settings。</p>
      </div>
      <div class="head-actions">
        <el-tag type="info">Revision {{ revision }}</el-tag>
        <el-dropdown @command="handleAction">
          <el-button>Actions ▾</el-button>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item command="import">Import</el-dropdown-item>
              <el-dropdown-item command="export">Export</el-dropdown-item>
              <el-dropdown-item command="backup">Create Backup</el-dropdown-item>
              <el-dropdown-item command="history">History</el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
      </div>
    </div>

    <template v-if="view==='workspace'">
      <div class="section-heading">
        <div>
          <h2>Configuration Workspace</h2>
          <p>Applied → Working Copy；编辑、评审、Validate 与 Apply 使用统一主工作区。</p>
        </div>
        <el-tag :type="dirtyMap[file] ? 'warning' : 'success'">{{ dirtyMap[file] ? 'Pending Apply' : 'Applied' }}</el-tag>
      </div>

      <el-card shadow="never">
        <div class="config-editor-layout">
          <aside class="config-nav">
            <div class="config-nav-group">
              <span>Files</span>
              <button
                v-for="name in CONFIG_FILES"
                :key="name"
                type="button"
                :class="{active:file===name}"
                @click="selectFile(name)"
              >
                {{ name }}
                <el-badge v-if="dirtyMap[name]" is-dot type="warning" />
              </button>
            </div>
          </aside>

          <main class="yaml-workspace">
            <div class="yaml-toolbar">
              <div class="yaml-title">
                <b>{{ file }}</b>
                <span>{{ dirtyMap[file] ? 'Working copy differs from applied revision' : 'Matches applied revision' }}</span>
              </div>
              <el-segmented v-model="editorMode" :options="[{label:'Edit',value:'edit'},{label:'Changes',value:'review'}]" />
            </div>

            <el-input
              v-if="editorMode==='edit'"
              v-model="yamlFiles[file]"
              class="yaml-input"
              type="textarea"
              :rows="25"
              @input="markDirty"
            />
            <div v-else class="review-editor">
              <div v-for="(line,index) in reviewLines" :key="index" :class="['review-line',line.type]">
                <span class="review-gutter">{{ line.type==='add' ? '+' : line.type==='remove' ? '−' : '' }}</span>
                <code>{{ line.text || ' ' }}</code>
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
    </template>

    <template v-else-if="view==='import'">
      <div class="subpage-back"><el-button text @click="view='workspace'">← Back to Configuration Files</el-button></div>
      <div class="section-heading">
        <div><h2>Import</h2><p>Upload → Validate → Diff → Impact → Apply。上传不会直接覆盖当前 Applied 配置。</p></div>
      </div>

      <div class="import-steps">
        <span class="active">1 Upload</span>
        <span :class="{active:importState.validated}">2 Validate</span>
        <span :class="{active:importState.validated}">3 Review Diff</span>
        <span :class="{active:importState.validated}">4 Impact</span>
        <span>5 Apply</span>
      </div>

      <el-card shadow="never">
        <div class="import-layout">
          <div>
            <el-form label-position="top">
              <el-form-item label="Target File">
                <el-select v-model="importState.target" style="width:100%">
                  <el-option v-for="name in CONFIG_FILES" :key="name" :label="name" :value="name" />
                </el-select>
              </el-form-item>
              <el-upload drag action="#" :auto-upload="false" :limit="1" :on-change="onImportChange">
                <div>Drop YAML here or click to select</div>
                <small>Only staged for validation and comparison</small>
              </el-upload>
            </el-form>
            <div class="inline-actions"><el-button type="primary" @click="validateImport">Validate & Compare</el-button></div>
          </div>

          <div class="import-review">
            <el-empty v-if="!importState.validated" description="Upload and validate a YAML file to preview changes" />
            <template v-else>
              <h3>Diff Preview</h3>
              <pre>{{ yamlDiffs[importState.target] }}</pre>
              <h3>Impact</h3>
              <el-descriptions :column="1" border>
                <el-descriptions-item label="Target">{{ importState.target }}</el-descriptions-item>
                <el-descriptions-item label="Runtime">Related objects only</el-descriptions-item>
                <el-descriptions-item label="Running Tasks">Re-evaluated before Apply</el-descriptions-item>
              </el-descriptions>
              <div class="inline-actions">
                <el-button @click="importState.validated=false">Back</el-button>
                <el-button type="primary" @click="applyImport">Apply Import</el-button>
              </div>
            </template>
          </div>
        </div>
      </el-card>
    </template>

    <template v-else>
      <div class="subpage-back"><el-button text @click="view='workspace'">← Back to Configuration Files</el-button></div>
      <div class="section-heading">
        <div><h2>Configuration History</h2><p>Restore 先比较当前 revision，并作为新 revision 应用。</p></div>
      </div>
      <el-card shadow="never">
        <el-table :data="history">
          <el-table-column prop="revision" label="Revision" width="100" />
          <el-table-column prop="time" label="Created At" width="180" />
          <el-table-column prop="source" label="Source" width="110" />
          <el-table-column prop="comment" label="Comment" min-width="220" />
          <el-table-column prop="status" label="Status" width="110" />
          <el-table-column label="Operation" width="140">
            <template #default="{row}">
              <el-button link type="primary" @click="restore(row)">Compare / Restore</el-button>
            </template>
          </el-table-column>
        </el-table>
      </el-card>
    </template>
  </div>
</template>

<style scoped>
.head-actions{display:flex;align-items:center;gap:var(--app-space-2)}.section-heading{display:flex;align-items:flex-end;justify-content:space-between;gap:var(--app-space-4);margin-bottom:var(--app-space-3)}.section-heading h2{margin:0;font-size:var(--app-font-section-title)}.section-heading p,.yaml-title span{margin:4px 0 0;color:var(--app-text-muted);font-size:var(--app-font-caption)}
.config-editor-layout{display:grid;grid-template-columns:210px minmax(0,1fr);gap:var(--app-space-4)}.config-nav{padding-right:var(--app-space-3);border-right:1px solid var(--app-border-soft)}.config-nav-group{display:flex;flex-direction:column;gap:4px}.config-nav-group>span{padding:0 8px;color:var(--app-text-muted);font-size:var(--app-font-caption);font-weight:600;text-transform:uppercase}
.config-nav button{display:flex;align-items:center;justify-content:space-between;width:100%;padding:8px 10px;border:1px solid transparent;border-radius:var(--app-radius-control);background:transparent;color:var(--app-text-primary);text-align:left;cursor:pointer}.config-nav button:hover{background:var(--el-fill-color-light)}.config-nav button.active{background:var(--el-color-primary-light-9);border-color:var(--el-color-primary-light-7);color:var(--el-color-primary)}
.yaml-workspace{min-width:0}.yaml-toolbar,.yaml-actions,.yaml-title{display:flex;align-items:center}.yaml-toolbar{justify-content:space-between;gap:var(--app-space-3);margin-bottom:var(--app-space-3)}.yaml-title{align-items:flex-start;flex-direction:column;gap:2px}.yaml-input :deep(textarea){font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;line-height:1.55}.yaml-actions{justify-content:flex-end;gap:var(--app-space-2);margin-top:var(--app-space-3)}
.review-editor{min-height:565px;border:1px solid var(--app-border-soft);border-radius:var(--app-radius-control);background:var(--el-bg-color);padding:8px 0;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;line-height:1.55}.review-line{display:grid;grid-template-columns:26px minmax(0,1fr);border-left:3px solid transparent}.review-line code{padding:1px 10px;white-space:pre-wrap;overflow-wrap:anywhere}.review-gutter{text-align:center;color:var(--app-text-muted)}.review-line.add{background:var(--el-color-success-light-9);border-left-color:var(--el-color-success)}.review-line.remove{background:var(--el-color-danger-light-9);border-left-color:var(--el-color-danger)}
.subpage-back{margin-bottom:var(--app-space-2)}.import-steps{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:1px;margin-bottom:var(--app-space-4);border-radius:var(--app-radius-control);overflow:hidden}.import-steps span{padding:10px;background:var(--el-fill-color-light);color:var(--app-text-muted);font-size:var(--app-font-caption);text-align:center}.import-steps span.active{background:var(--el-color-primary-light-9);color:var(--el-color-primary)}.import-layout{display:grid;grid-template-columns:minmax(300px,.7fr) minmax(0,1.3fr);gap:var(--app-space-5)}.import-review pre{white-space:pre-wrap}.inline-actions{display:flex;justify-content:flex-end;gap:var(--app-space-2);margin-top:var(--app-space-3)}
@media(max-width:1000px){.config-editor-layout,.import-layout{grid-template-columns:1fr}.config-nav{display:flex;overflow-x:auto;border-right:0;border-bottom:1px solid var(--app-border-soft);padding:0 0 var(--app-space-3)}.config-nav-group{flex-direction:row}.config-nav-group>span{display:none}.config-nav button{min-width:150px}}
@media(max-width:767px){.section-heading,.yaml-toolbar{align-items:flex-start;flex-direction:column}.yaml-actions{flex-wrap:wrap}.yaml-actions .el-button{flex:1;margin-left:0!important}.import-steps{grid-template-columns:1fr}.head-actions{width:100%;justify-content:space-between}}
</style>
