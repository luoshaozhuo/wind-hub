<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { store } from '../mock/data'
import { CONFIG_FILES, updateMockAdsYaml, updateMockSiteYaml, yamlDiffs, yamlFiles } from '../mock/yaml'

type ReviewLine = { type: 'same' | 'add' | 'remove'; text: string }

const file = ref('devices.yaml')
const editorMode = ref<'edit' | 'review'>('review')
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

const adsEditing = ref(false)
const adsDraft = reactive({
  local_ip: store.systemInfo.ads.local_ip,
  local_ams_net_id: store.systemInfo.ads.local_ams_net_id,
  route_repair_enabled: store.systemInfo.ads.route_repair.enabled,
  route_name: store.systemInfo.ads.route_repair.route_name,
  username: store.systemInfo.ads.route_repair.username,
  password: store.systemInfo.ads.route_repair.password,
})

function editAds() {
  adsDraft.local_ip = store.systemInfo.ads.local_ip
  adsDraft.local_ams_net_id = store.systemInfo.ads.local_ams_net_id
  adsDraft.route_repair_enabled = store.systemInfo.ads.route_repair.enabled
  adsDraft.route_name = store.systemInfo.ads.route_repair.route_name
  adsDraft.username = store.systemInfo.ads.route_repair.username
  adsDraft.password = store.systemInfo.ads.route_repair.password
  adsEditing.value = true
}

function cancelAds() {
  adsEditing.value = false
}

async function updateAds() {
  if (!adsDraft.local_ip.trim() || !adsDraft.local_ams_net_id.trim()) {
    ElMessage.error('Local IP and Local AMS Net ID are required')
    return
  }
  const parts = adsDraft.local_ams_net_id.split('.')
  if (parts.length !== 6 || parts.some(p => !/^\d+$/.test(p) || Number(p) > 255)) {
    ElMessage.error('Local AMS Net ID must contain 6 numeric octets')
    return
  }

  const adsDevices = store.devices.filter(d =>
    store.deviceModels.find(m => m.id === d.model)?.protocol === 'ads',
  )
  const runningTasks = store.tasks.filter(t => t.runtime === 'RUNNING' && (
    (t.device && adsDevices.some(d => d.device_id === t.device)) ||
    (t.device_group && adsDevices.some(d => d.device_group === t.device_group))
  ))

  if (adsDevices.length) {
    await ElMessageBox.confirm(
      '<b>Global ADS Settings Change Impact</b><br><br>' +
      adsDevices.length + ' ADS Device(s) affected.<br>' +
      runningTasks.length + ' running Task(s) affected.<br><br>' +
      'ADS local identity is process-global. Applying this change requires ADS router and connection reinitialization.',
      'Update Global ADS Settings',
      { type: 'warning', confirmButtonText: 'Update', dangerouslyUseHTMLString: true },
    )
  }

  store.systemInfo.ads.local_ip = adsDraft.local_ip.trim()
  store.systemInfo.ads.local_ams_net_id = adsDraft.local_ams_net_id.trim()
  Object.assign(store.systemInfo.ads.route_repair, {
    enabled: adsDraft.route_repair_enabled,
    route_name: adsDraft.route_name.trim(),
    username: adsDraft.username.trim(),
    password: adsDraft.password,
  })
  updateMockAdsYaml(store.systemInfo.ads)
  dirtyMap['system.yaml'] = true
  adsEditing.value = false
  ElMessage.success('Global ADS settings updated — system.yaml pending apply (mock)')
}

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

async function saveApply() {
  if (!yamlFiles[file.value].trim()) {
    ElMessage.error(`${file.value} is empty`)
    return
  }
  if (yamlFiles[file.value] !== appliedSnapshot[file.value]) {
    await ElMessageBox.confirm(
      'The configuration will be validated, saved and applied. Affected running tasks may be stopped and restored according to the change impact.',
      'Save & Apply',
      { type: 'warning', confirmButtonText: 'Apply Changes' },
    )
  }
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
async function upApply() {
  await ElMessageBox.confirm(
    'Apply the uploaded configuration? The backend must recalculate change impact before applying and reject stale previews.',
    'Apply Uploaded Configuration',
    { type: 'warning', confirmButtonText: 'Apply Changes' },
  )
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
      <div>
        <h1>Config</h1>
        <p>现场身份、全局通信参数与 YAML 配置生命周期</p>
      </div>
    </div>

    <section class="config-section-block">
      <div class="section-heading">
        <div><h2>Instance Settings</h2><p>少量全局参数直接管理；修改后进入 Pending Apply。</p></div>
      </div>
      <div class="settings-grid">
        <el-card shadow="never" class="setting-card">
          <div class="setting-head"><div><h3>Site</h3><p>Wind Hub 实例对应的现场身份</p></div><el-button v-if="!siteEditing" @click="editSite">Edit</el-button></div>
          <template v-if="!siteEditing">
            <el-descriptions :column="1">
              <el-descriptions-item label="Site ID">{{ store.systemInfo.siteId }}</el-descriptions-item>
              <el-descriptions-item label="Site Name">{{ store.systemInfo.siteName }}</el-descriptions-item>
            </el-descriptions>
          </template>
          <el-form v-else label-position="top">
            <el-form-item label="Site ID"><el-input v-model="siteDraft.siteId" /></el-form-item>
            <el-form-item label="Site Name"><el-input v-model="siteDraft.siteName" /></el-form-item>
            <div class="inline-actions"><el-button @click="cancelSite">Cancel</el-button><el-button type="primary" @click="updateSite">Update</el-button></div>
          </el-form>
        </el-card>

        <el-card shadow="never" class="setting-card">
          <div class="setting-head"><div><h3>Global ADS Settings</h3><p>进程唯一的本机 ADS 身份</p></div><el-button v-if="!adsEditing" @click="editAds">Edit</el-button></div>
          <template v-if="!adsEditing">
            <el-descriptions :column="1">
              <el-descriptions-item label="Local IP">{{ store.systemInfo.ads.local_ip }}</el-descriptions-item>
              <el-descriptions-item label="Local AMS Net ID">{{ store.systemInfo.ads.local_ams_net_id }}</el-descriptions-item>
              <el-descriptions-item label="Route Repair">{{ store.systemInfo.ads.route_repair.enabled ? 'Enabled' : 'Disabled' }}</el-descriptions-item>
            </el-descriptions>
          </template>
          <el-form v-else label-position="top">
            <div class="two-col">
              <el-form-item label="Local IP"><el-input v-model="adsDraft.local_ip" /></el-form-item>
              <el-form-item label="Local AMS Net ID"><el-input v-model="adsDraft.local_ams_net_id" /></el-form-item>
              <el-form-item label="Route Repair"><el-switch v-model="adsDraft.route_repair_enabled" /></el-form-item>
              <el-form-item label="Route Name"><el-input v-model="adsDraft.route_name" :disabled="!adsDraft.route_repair_enabled" /></el-form-item>
              <el-form-item label="Username"><el-input v-model="adsDraft.username" :disabled="!adsDraft.route_repair_enabled" /></el-form-item>
              <el-form-item label="Password"><el-input v-model="adsDraft.password" type="password" show-password :disabled="!adsDraft.route_repair_enabled" /></el-form-item>
            </div>
            <div class="inline-actions"><el-button @click="cancelAds">Cancel</el-button><el-button type="primary" @click="updateAds">Update</el-button></div>
          </el-form>
        </el-card>
      </div>
    </section>

    <section class="config-section-block">
      <div class="section-heading">
        <div><h2>Configuration Workspace</h2><p>选择配置文件，编辑或评审 Applied → Working Copy 的差异。</p></div>
        <div class="workspace-status"><el-tag :type="dirtyMap[file] ? 'warning' : 'success'">{{ dirtyMap[file] ? 'Pending Apply' : 'Applied' }}</el-tag></div>
      </div>

      <el-card shadow="never">
        <div class="config-editor-layout">
          <aside class="config-nav">
            <div class="config-nav-group"><span>Runtime</span>
              <button type="button" :class="{active:file==='system.yaml'}" @click="selectFile('system.yaml')">system.yaml<el-badge v-if="dirtyMap['system.yaml']" is-dot type="warning"/></button>
            </div>
            <div class="config-nav-group"><span>Definitions</span>
              <button v-for="name in ['units.yaml','device_models.yaml','points.yaml']" :key="name" type="button" :class="{active:file===name}" @click="selectFile(name)">{{name}}<el-badge v-if="dirtyMap[name]" is-dot type="warning"/></button>
            </div>
            <div class="config-nav-group"><span>Acquisition</span>
              <button v-for="name in ['devices.yaml','tasks.yaml']" :key="name" type="button" :class="{active:file===name}" @click="selectFile(name)">{{name}}<el-badge v-if="dirtyMap[name]" is-dot type="warning"/></button>
            </div>
            <div class="config-nav-group"><span>Reporting</span>
              <button type="button" :class="{active:file==='reporting.yaml'}" @click="selectFile('reporting.yaml')">reporting.yaml<el-badge v-if="dirtyMap['reporting.yaml']" is-dot type="warning"/></button>
            </div>
          </aside>

          <div class="yaml-workspace">
            <div class="yaml-toolbar">
              <div class="yaml-title"><b>{{ file }}</b><span>{{ dirtyMap[file] ? 'Working copy differs from applied revision' : 'Matches applied revision' }}</span></div>
              <el-segmented v-model="editorMode" :options="editorModeOptions" />
            </div>
            <el-input v-if="editorMode==='edit'" v-model="yamlFiles[file]" class="yaml-input" type="textarea" :rows="25" @input="markDirty" />
            <div v-else class="review-editor">
              <div v-for="(line,index) in reviewLines" :key="index" :class="['review-line',line.type]">
                <span class="review-gutter">{{ line.type==='add' ? '+' : line.type==='remove' ? '−' : '' }}</span><code>{{ line.text || ' ' }}</code>
              </div>
            </div>
            <div class="yaml-actions">
              <el-button @click="validate">Validate</el-button>
              <el-button @click="save">Save Draft</el-button>
              <el-button type="primary" @click="saveApply">Save & Apply</el-button>
            </div>
          </div>
        </div>
      </el-card>
    </section>

    <section class="config-section-block">
      <div class="section-heading">
        <div><h2>Import Configuration</h2><p>上传只进入临时比较流程，不会直接覆盖当前 Applied 配置。</p></div>
      </div>
      <el-card shadow="never">
        <div class="import-layout">
          <div class="import-input">
            <el-form label-position="top">
              <el-form-item label="Target File"><el-select v-model="up.file" style="width:100%"><el-option v-for="name in CONFIG_FILES" :key="name" :label="name" :value="name"/></el-select></el-form-item>
              <el-upload drag action="#" :auto-upload="false" :limit="1" :on-change="onUploadChange"><div>Drop YAML here or click to select</div><small>Validate & Compare 后才能保存或应用</small></el-upload>
            </el-form>
            <div class="inline-actions"><el-button type="primary" @click="upValidate">Validate & Compare</el-button></div>
          </div>
          <div class="import-review">
            <el-empty v-if="up.state!=='compared'" description="Upload and validate a YAML file to preview changes"/>
            <template v-else>
              <h3>Structured Diff</h3>
              <el-table :data="upDiff" size="small"><el-table-column prop="o" label="Object"/><el-table-column prop="a" label="Added"/><el-table-column prop="r" label="Removed"/><el-table-column prop="u" label="Updated"/></el-table>
              <pre>{{ yamlDiffs[up.file] }}</pre>
              <div class="inline-actions"><el-button @click="upCancel">Cancel</el-button><el-button @click="upSave">Save Draft</el-button><el-button type="primary" @click="upApply">Save & Apply</el-button></div>
            </template>
          </div>
        </div>
      </el-card>
    </section>
  </div>
</template>

<style scoped>
.config-section-block{margin-bottom:var(--app-space-5)}.section-heading{display:flex;align-items:flex-end;justify-content:space-between;gap:var(--app-space-4);margin-bottom:var(--app-space-3)}.section-heading h2,.setting-head h3{margin:0}.section-heading p,.setting-head p,.yaml-title span{margin:4px 0 0;color:var(--app-text-muted);font-size:var(--app-font-caption)}.settings-grid{display:grid;grid-template-columns:minmax(0,.75fr) minmax(0,1.25fr);gap:var(--app-space-4)}.setting-head{display:flex;justify-content:space-between;align-items:flex-start;gap:var(--app-space-3);margin-bottom:var(--app-space-3)}.two-col{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:0 var(--app-space-3)}.inline-actions{display:flex;justify-content:flex-end;gap:var(--app-space-2);margin-top:var(--app-space-3)}.config-editor-layout{display:grid;grid-template-columns:210px minmax(0,1fr);gap:var(--app-space-4)}.config-nav{padding-right:var(--app-space-3);border-right:1px solid var(--app-border-soft)}.config-nav-group{display:flex;flex-direction:column;gap:4px;margin-bottom:var(--app-space-3)}.config-nav-group>span{padding:0 8px;color:var(--app-text-muted);font-size:var(--app-font-caption);font-weight:600;text-transform:uppercase}.config-nav button{display:flex;align-items:center;justify-content:space-between;width:100%;padding:8px 10px;border:1px solid transparent;border-radius:var(--app-radius-control);background:transparent;color:var(--app-text-primary);text-align:left;cursor:pointer}.config-nav button:hover{background:var(--el-fill-color-light)}.config-nav button.active{background:var(--el-color-primary-light-9);border-color:var(--el-color-primary-light-7);color:var(--el-color-primary)}.yaml-workspace{min-width:0}.yaml-toolbar,.yaml-actions,.yaml-title{display:flex;align-items:center}.yaml-toolbar{justify-content:space-between;gap:var(--app-space-3);margin-bottom:var(--app-space-3)}.yaml-title{align-items:flex-start;flex-direction:column;gap:2px}.yaml-input :deep(textarea){font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;line-height:1.55}.yaml-actions{justify-content:flex-end;gap:var(--app-space-2);margin-top:var(--app-space-3)}.review-editor{min-height:565px;max-height:66vh;overflow:auto;border:1px solid var(--app-border-soft);border-radius:var(--app-radius-control);background:var(--el-bg-color);padding:8px 0;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;line-height:1.55}.review-line{display:grid;grid-template-columns:26px minmax(0,1fr);border-left:3px solid transparent}.review-line code{padding:1px 10px;white-space:pre-wrap;overflow-wrap:anywhere}.review-gutter{text-align:center;color:var(--app-text-muted)}.review-line.add{background:var(--el-color-success-light-9);border-left-color:var(--el-color-success)}.review-line.remove{background:var(--el-color-danger-light-9);border-left-color:var(--el-color-danger)}.import-layout{display:grid;grid-template-columns:minmax(320px,.7fr) minmax(0,1.3fr);gap:var(--app-space-5)}.import-review pre{max-height:260px;overflow:auto}.workspace-status{flex:0 0 auto}
@media(max-width:1000px){.settings-grid,.import-layout{grid-template-columns:1fr}.config-editor-layout{grid-template-columns:1fr}.config-nav{display:flex;overflow:auto;border-right:0;border-bottom:1px solid var(--app-border-soft);padding:0 0 var(--app-space-3)}.config-nav-group{min-width:180px;margin:0 var(--app-space-3) 0 0}}
@media(max-width:767px){.two-col{grid-template-columns:1fr}.section-heading,.yaml-toolbar{align-items:flex-start;flex-direction:column}.yaml-actions{flex-wrap:wrap}.yaml-actions .el-button{flex:1;margin-left:0!important}.review-editor{min-height:420px}}
</style>
