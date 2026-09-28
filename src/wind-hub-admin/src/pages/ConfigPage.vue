<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { store } from '../mock/data'
import { CONFIG_FILES, updateMockSiteYaml, yamlDiffs, yamlFiles } from '../mock/yaml'

type ReviewLine = { type: 'same' | 'add' | 'remove'; text: string }

const configTab = ref('YAML Editor')
const file = ref('devices.yaml')
const editorMode = ref<'edit' | 'review'>('edit')
const editorModeOptions = [
  { label: 'Edit', value: 'edit' },
  { label: 'Review', value: 'review' },
]
const appliedSnapshot = reactive<Record<string, string>>(
  Object.fromEntries(CONFIG_FILES.map(name => [name, yamlFiles[name]])),
)
const savedSnapshot = reactive<Record<string, string>>(
  Object.fromEntries(CONFIG_FILES.map(name => [name, yamlFiles[name]])),
)

const dirtyMap = reactive<Record<string, boolean>>({})

const siteEditing = ref(false)
const siteDraft = reactive({
  siteId: store.systemInfo.siteId,
  siteName: store.systemInfo.siteName,
})

function editSite() {
  siteDraft.siteId = store.systemInfo.siteId
  siteDraft.siteName = store.systemInfo.siteName
  siteEditing.value = true
}

function cancelSite() {
  siteDraft.siteId = store.systemInfo.siteId
  siteDraft.siteName = store.systemInfo.siteName
  siteEditing.value = false
}

function updateSite() {
  const siteId = siteDraft.siteId.trim()
  const siteName = siteDraft.siteName.trim()
  if (!siteId || !siteName) {
    ElMessage.error('Site ID and Site Name are required')
    return
  }
  store.systemInfo.siteId = siteId
  store.systemInfo.siteName = siteName
  updateMockSiteYaml(siteId, siteName)
  dirtyMap['system.yaml'] = true
  siteEditing.value = false
  ElMessage.success('Site information updated — system.yaml pending apply (mock)')
}

function selectFile(name: string) {
  file.value = name
}

function markDirty() {
  dirtyMap[file.value] = yamlFiles[file.value] !== appliedSnapshot[file.value]
}

function validate() {
  if (!yamlFiles[file.value].trim()) {
    ElMessage.error(`${file.value} is empty`)
    return
  }
  ElMessage.success(`${file.value} validation passed (mock)`)
}

function save() {
  savedSnapshot[file.value] = yamlFiles[file.value]
  dirtyMap[file.value] = yamlFiles[file.value] !== appliedSnapshot[file.value]
  ElMessage.success(dirtyMap[file.value]
    ? `${file.value} saved — pending apply (mock)`
    : `${file.value} saved (mock)`)
}

function saveApply() {
  savedSnapshot[file.value] = yamlFiles[file.value]
  appliedSnapshot[file.value] = yamlFiles[file.value]
  dirtyMap[file.value] = false
  ElMessage.success(`${file.value} saved & reload applied (mock)`)
}

function buildReview(before: string, after: string): ReviewLine[] {
  const a = before.split('\n')
  const b = after.split('\n')
  const dp = Array.from({ length: a.length + 1 }, () => Array<number>(b.length + 1).fill(0))
  for (let i = a.length - 1; i >= 0; i--) {
    for (let j = b.length - 1; j >= 0; j--) {
      dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1])
    }
  }
  const lines: ReviewLine[] = []
  let i = 0
  let j = 0
  while (i < a.length || j < b.length) {
    if (i < a.length && j < b.length && a[i] === b[j]) {
      lines.push({ type: 'same', text: a[i] })
      i++; j++
    } else if (j < b.length && (i === a.length || dp[i][j + 1] >= dp[i + 1][j])) {
      lines.push({ type: 'add', text: b[j] })
      j++
    } else {
      lines.push({ type: 'remove', text: a[i] })
      i++
    }
  }
  return lines
}

const reviewLines = computed(() => buildReview(appliedSnapshot[file.value], yamlFiles[file.value]))

const up = reactive({
  file: 'devices.yaml',
  name: '',
  state: 'idle' as 'idle' | 'compared',
})
const upDiff = [
  { o: 'Devices', a: '+2', r: '-1', u: '~3' },
  { o: 'Tasks', a: '+1', r: '', u: '~2' },
  { o: 'Point Tables', a: '', r: '', u: '~1' },
]
function onUploadChange(f: { name?: string }) { up.name = f?.name || ''; up.state = 'idle' }
function upValidate() {
  if (!up.name) { ElMessage.warning('Select a YAML file first'); return }
  up.state = 'compared'
  ElMessage.success(`${up.name} validation passed — diff ready (mock)`)
}
function upCancel() { up.state = 'idle'; up.name = '' }
function upSave() {
  dirtyMap[up.file] = true
  up.state = 'idle'; up.name = ''
  ElMessage.success(`${up.file} updated from upload — pending apply (mock)`)
}
function upApply() {
  appliedSnapshot[up.file] = yamlFiles[up.file]
  savedSnapshot[up.file] = yamlFiles[up.file]
  dirtyMap[up.file] = false
  up.state = 'idle'; up.name = ''
  ElMessage.success(`${up.file} saved & reload applied (mock)`)
}

</script>

<template>
  <div class="config-page">
    <div class="head">
      <div><h1>Config</h1><p>直接编辑 YAML、上传配置、评审修改并下发生效</p></div>
    </div>

    <el-card shadow="never" class="site-card">
      <div class="site-row">
        <div class="site-heading">
          <h3>Site</h3>
          <p>当前 Wind Hub 实例对应的现场身份</p>
        </div>

        <template v-if="!siteEditing">
          <div class="site-info">
            <div><span>Site ID</span><b>{{ store.systemInfo.siteId }}</b></div>
            <div><span>Site Name</span><b>{{ store.systemInfo.siteName }}</b></div>
          </div>
          <el-button @click="editSite">Edit</el-button>
        </template>

        <template v-else>
          <div class="site-edit">
            <el-form label-position="top">
              <div class="site-edit-grid">
                <el-form-item label="Site ID"><el-input v-model="siteDraft.siteId" /></el-form-item>
                <el-form-item label="Site Name"><el-input v-model="siteDraft.siteName" /></el-form-item>
              </div>
            </el-form>
          </div>
          <div class="site-actions">
            <el-button @click="cancelSite">Cancel</el-button>
            <el-button type="primary" @click="updateSite">Update</el-button>
          </div>
        </template>
      </div>
    </el-card>

    <el-card shadow="never">
      <el-tabs v-model="configTab">
        <el-tab-pane label="YAML Editor" name="YAML Editor">
          <div class="config-editor-layout">
            <el-menu :default-active="file" class="config-file-menu" @select="selectFile">
              <el-menu-item v-for="name in CONFIG_FILES" :key="name" :index="name">
                <span>{{ name }}</span>
                <el-badge v-if="dirtyMap[name]" is-dot type="warning" />
              </el-menu-item>
            </el-menu>

            <div class="yaml-workspace">
              <div class="yaml-toolbar">
                <div class="yaml-title">
                  <b>{{ file }}</b>
                  <span :class="['apply-state', { pending: dirtyMap[file] }]">
                    {{ dirtyMap[file] ? 'Pending Apply' : 'Applied' }}
                  </span>
                </div>
                <el-segmented v-model="editorMode" :options="editorModeOptions" />
              </div>

              <el-input
                v-if="editorMode === 'edit'"
                v-model="yamlFiles[file]"
                class="yaml-input"
                type="textarea"
                :rows="24"
                @input="markDirty"
              />

              <div v-else class="review-editor">
                <div v-for="(line, index) in reviewLines" :key="index" :class="['review-line', line.type]">
                  <span class="review-gutter">{{ line.type === 'add' ? '+' : line.type === 'remove' ? '−' : '' }}</span>
                  <code>{{ line.text || ' ' }}</code>
                </div>
              </div>

              <div v-if="editorMode === 'edit'" class="yaml-actions">
                <el-button @click="validate">Validate</el-button>
                <el-button @click="save">Save</el-button>
                <el-button type="primary" @click="saveApply">Save & Apply</el-button>
              </div>
            </div>
          </div>
        </el-tab-pane>

        <el-tab-pane label="Upload" name="Upload">
          <div class="upload">
            <section>
              <h3>Upload Configuration File</h3>
              <el-select v-model="up.file" style="width:100%">
                <el-option v-for="name in CONFIG_FILES" :key="name" :label="name" :value="name" />
              </el-select>
              <el-upload drag action="#" :auto-upload="false" :limit="1" :on-change="onUploadChange">
                <div>Drop YAML here or click to select</div><small>上传不会立即覆盖正式配置</small>
              </el-upload>
              <p v-if="up.name">已选择：{{ up.name }}</p>
              <el-button type="primary" @click="upValidate">Validate & Compare</el-button>
            </section>
            <section v-if="up.state === 'compared'">
              <h3>Structured Diff</h3>
              <el-table :data="upDiff">
                <el-table-column prop="o" label="Object" /><el-table-column prop="a" label="Added" />
                <el-table-column prop="r" label="Removed" /><el-table-column prop="u" label="Updated" />
              </el-table>
              <pre>{{ yamlDiffs[up.file] }}</pre>
              <div class="right"><el-button @click="upCancel">Cancel</el-button><el-button @click="upSave">Save</el-button><el-button type="primary" @click="upApply">Save & Apply</el-button></div>
            </section>
          </div>
        </el-tab-pane>
</el-tabs>
    </el-card>
  </div>
</template>

<style scoped>
.site-card{margin-bottom:16px}.site-row{min-height:72px;display:flex;align-items:center;gap:24px}.site-heading{width:210px;flex:0 0 auto}.site-heading h3{margin:0;font-size:var(--font-size-section-lg)}.site-heading p{margin:4px 0 0;color:#8a94a3;font-size:var(--font-size-body)}.site-info{flex:1;display:flex;gap:48px}.site-info>div{min-width:180px}.site-info span{display:block;margin-bottom:5px;color:#8a94a3;font-size:var(--font-size-label)}.site-info b{color:#2b3646;font-size:var(--font-size-subsection)}.site-edit{flex:1}.site-edit-grid{display:grid;grid-template-columns:minmax(180px,1fr) minmax(240px,1.4fr);gap:14px}.site-edit :deep(.el-form-item){margin-bottom:0}.site-actions{display:flex;gap:8px}
.config-editor-layout{display:grid;grid-template-columns:190px minmax(0,1fr);gap:18px}.config-file-menu{border-right:0!important;background:transparent}.config-file-menu .el-menu-item{height:38px;line-height:38px;margin-bottom:4px;border:1px solid var(--app-border);border-radius:7px;padding:0 10px!important;display:flex;justify-content:space-between}.config-file-menu .el-menu-item.is-active{background:#eef4ff;border-color:#b8cdf5;color:#244a86}.yaml-workspace{min-width:0}.yaml-toolbar,.yaml-actions,.yaml-title{display:flex;align-items:center}.yaml-toolbar{justify-content:space-between;gap:16px;margin-bottom:10px}.yaml-title{gap:9px}.apply-state{color:#667085;font-size:var(--font-size-label)}.apply-state.pending{color:#b7791f}.yaml-input :deep(textarea){font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:var(--font-size-body);line-height:1.55}.yaml-actions{justify-content:flex-end;gap:8px;margin-top:12px}.review-editor{min-height:558px;max-height:65vh;overflow:auto;border:1px solid #dfe4ea;border-radius:6px;background:#fff;padding:8px 0;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:var(--font-size-body);line-height:1.55}.review-line{display:grid;grid-template-columns:26px minmax(0,1fr);min-height:20px;border-left:3px solid transparent}.review-line code{padding:1px 10px;white-space:pre-wrap;overflow-wrap:anywhere;color:#344054}.review-gutter{text-align:center;color:#a8b0bc;user-select:none}.review-line.add{background:#f1f9f4;border-left-color:#2f8f52}.review-line.add code{color:#25683c}.review-line.remove{background:#fff4f2;border-left-color:#d94d45}.review-line.remove code{color:#a43d38;text-decoration:line-through}.package-upload{max-width:520px}.package-table{max-width:720px}.overall-title{margin-top:18px}
@media(max-width:900px){.site-row{align-items:stretch;flex-direction:column}.site-heading{width:auto}.site-info{gap:24px;flex-wrap:wrap}.site-edit-grid{grid-template-columns:1fr}.site-actions{justify-content:flex-end}.config-editor-layout{grid-template-columns:1fr}.config-file-menu{display:flex;overflow:auto;border-bottom:0}.config-file-menu .el-menu-item{flex:0 0 auto;white-space:nowrap;margin-right:4px}.upload{grid-template-columns:1fr}}
@media(max-width:767px){.site-info{display:grid;grid-template-columns:1fr}.site-info>div{min-width:0}.site-actions{width:100%}.site-actions .el-button{flex:1}.yaml-toolbar{align-items:flex-start;flex-direction:column}.yaml-actions{flex-wrap:wrap}.yaml-actions .el-button{flex:1;margin-left:0!important}.review-editor{min-height:420px}.package-upload,.package-table{max-width:100%}}
</style>
