<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useMutation, useQuery } from '@tanstack/vue-query'
import {
  applyConfig,
  downloadConfigBackup,
  fetchConfigFiles,
  fetchConfigFile,
  fetchConfigHistory,
  importConfig,
  restoreConfig,
  validateConfig,
} from '../api/config'
import { queryClient } from '../api/queryClient'
import { CONFIG_QUERY_KEYS, qk } from '../api/queryKeys'
import { useConfigFilesStore } from '../stores/configFiles'

type ReviewLine = { type: 'same' | 'add' | 'remove'; text: string }
interface HistoryEntry {
  revision: number
  time: string
  source: string
  comment: string
  status: string
}

const filesStore = useConfigFilesStore()

const section = ref<'files' | 'history'>('files')
const file = ref('devices.yaml')
const editorMode = ref<'edit' | 'review'>('review')
const importOpen = ref(false)

// -- Server State：文件清单 / 内容 / 历史 ------------------------------------
const filesQuery = useQuery({ queryKey: qk.configFiles, queryFn: fetchConfigFiles })
const contentsQuery = useQuery({
  queryKey: qk.configContents,
  enabled: computed(() => filesQuery.isSuccess.value),
  queryFn: async () => {
    const existing = (filesQuery.data.value || []).filter((f) => f.exists)
    const rows = await Promise.all(existing.map((f) => fetchConfigFile(f.name)))
    return Object.fromEntries(rows.map((row) => [row.name, row.content])) as Record<string, string>
  },
})
const historyQuery = useQuery({ queryKey: qk.configHistory, queryFn: fetchConfigHistory })

const CONFIG_FILES = computed(() =>
  (filesQuery.data.value || []).filter((f) => f.exists || !f.optional).map((f) => f.name),
)
const yamlFiles = computed(() => filesStore.working)
const dirtyMap = computed(() => filesStore.dirtyMap)

watch(
  () => [CONFIG_FILES.value, contentsQuery.data.value] as const,
  ([names, contents]) => {
    if (!names.length || !contents) return
    filesStore.syncFromServer(names, contents)
    if (!names.includes(file.value)) file.value = names[0] || ''
  },
  { immediate: true },
)

const revision = computed(() => historyQuery.data.value?.[0]?.revision || 0)
const history = computed<HistoryEntry[]>(() =>
  (historyQuery.data.value || []).map((row, index) => ({
    revision: row.revision,
    time: row.created_at.replace('T', ' ').slice(0, 19),
    source: row.source,
    comment: row.comment,
    status: index === 0 ? 'Applied' : 'Archived',
  })),
)

/** Apply / Import / Restore 后：工作区、历史与全部配置类查询统一失效。 */
async function invalidateConfigState() {
  await Promise.all([
    queryClient.invalidateQueries({ queryKey: qk.configFiles }),
    queryClient.invalidateQueries({ queryKey: qk.configContents }),
    queryClient.invalidateQueries({ queryKey: qk.configHistory }),
    ...CONFIG_QUERY_KEYS.map((key) => queryClient.invalidateQueries({ queryKey: key })),
  ])
}

// -- Validate / Apply ---------------------------------------------------------
const validateMutation = useMutation({
  mutationFn: async (payload: { name: string; content: string }) => {
    const result = await validateConfig(payload.name, payload.content)
    if (!result.valid) {
      ElMessage.error(
        result.errors[0] + (result.errors.length > 1 ? ` (+${result.errors.length - 1} more)` : ''),
      )
      return false
    }
    ElMessage.success(payload.name + ' validation passed')
    return true
  },
  onError: (error) => {
    ElMessage.error(error instanceof Error ? error.message : String(error))
  },
})
const validating = computed(() => validateMutation.isPending.value)

const applyMutation = useMutation({
  mutationFn: async (payload: { name: string; content: string }) => {
    const result = await applyConfig(
      payload.name,
      payload.content,
      payload.name + ' applied from workspace',
    )
    if (!result.success) throw new Error(result.errors.join('; ') || 'Apply failed')
    return result
  },
  onSuccess: async (result, payload) => {
    filesStore.markApplied(payload.name)
    await invalidateConfigState()
    ElMessage.success('Revision ' + (result.revision ?? revision.value) + ' applied')
  },
  onError: (error) => {
    ElMessage.error(error instanceof Error ? error.message : String(error))
  },
})
const applying = computed(() => applyMutation.isPending.value)

async function validate(): Promise<boolean> {
  if (validating.value || applying.value) return false
  return await validateMutation.mutateAsync({
    name: file.value,
    content: yamlFiles.value[file.value] || '',
  })
}

function configApplyImpact(name: string) {
  return ['Validate full configuration', 'Apply ' + name, 'Incremental runtime reconfigure']
}

async function apply() {
  if (applying.value || validating.value || !dirtyMap.value[file.value]) return
  if (!(await validate())) return
  try {
    await ElMessageBox.confirm(
      'Apply affected runtime objects. Impact: ' + configApplyImpact(file.value).join('; ') + '.',
      'Apply Configuration',
      { type: 'warning', confirmButtonText: 'Apply' },
    )
  } catch {
    return
  }
  applyMutation.mutate({ name: file.value, content: yamlFiles.value[file.value] || '' })
}

function buildReview(before: string, after: string): ReviewLine[] {
  const a = before.split('\n'),
    b = after.split('\n')
  const dp = Array.from({ length: a.length + 1 }, () => Array<number>(b.length + 1).fill(0))
  for (let i = a.length - 1; i >= 0; i--)
    for (let j = b.length - 1; j >= 0; j--)
      dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1])
  const lines: ReviewLine[] = []
  let i = 0,
    j = 0
  while (i < a.length || j < b.length) {
    if (i < a.length && j < b.length && a[i] === b[j]) {
      lines.push({ type: 'same', text: a[i] })
      i++
      j++
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
const reviewLines = computed(() =>
  buildReview(filesStore.applied[file.value] || '', yamlFiles.value[file.value] || ''),
)

// -- Import -------------------------------------------------------------------
const importState = reactive<{
  target: string
  name: string
  text: string
  validated: boolean
  errors: string[]
  raw: File | null
}>({ target: 'devices.yaml', name: '', text: '', validated: false, errors: [], raw: null })

function onImportChange(upload: { name?: string; raw?: File }) {
  importState.name = upload.name || ''
  importState.raw = upload.raw || null
  importState.text = ''
  importState.validated = false
  importState.errors = []
}
watch(
  () => importState.target,
  () => {
    importState.validated = false
    importState.errors = []
  },
)
const importNoChanges = computed(
  () => importState.validated && importState.text === (yamlFiles.value[importState.target] || ''),
)
const importReviewLines = computed(() =>
  importState.validated
    ? buildReview(yamlFiles.value[importState.target] || '', importState.text)
    : [],
)
const importImpacts = computed(() => configApplyImpact(importState.target))

const importValidateMutation = useMutation({
  mutationFn: async () => {
    if (!importState.raw) throw new Error('Select a YAML file first')
    importState.text = await importState.raw.text()
    if (!importState.text.trim()) {
      importState.errors = [importState.target + ': uploaded file is empty']
      importState.validated = false
      return
    }
    const result = await validateConfig(importState.target, importState.text)
    importState.errors = result.errors
    importState.validated = result.valid
    if (!result.valid) {
      ElMessage.error('Import validation failed')
      return
    }
    ElMessage.success(
      importNoChanges.value ? 'Import validated — no changes' : 'Import validated — diff ready',
    )
  },
  onError: (error) => {
    ElMessage.error(error instanceof Error ? error.message : String(error))
  },
})
const importValidating = computed(() => importValidateMutation.isPending.value)

function validateImport() {
  if (importValidating.value || importApplying.value) return
  if (!importState.name || !importState.raw) {
    ElMessage.warning('Select a YAML file first')
    return
  }
  importValidateMutation.mutate()
}

function closeImport() {
  importOpen.value = false
  importState.name = ''
  importState.text = ''
  importState.validated = false
  importState.errors = []
  importState.raw = null
}

const importApplyMutation = useMutation({
  mutationFn: async () => {
    const result = await importConfig(
      importState.target,
      importState.text,
      'Imported ' + importState.name,
    )
    if (!result.success) throw new Error(result.errors.join('; ') || 'Import failed')
    return result
  },
  onSuccess: async (result) => {
    await invalidateConfigState()
    closeImport()
    ElMessage.success('Imported as revision ' + (result.revision ?? revision.value))
  },
  onError: (error) => {
    ElMessage.error(error instanceof Error ? error.message : String(error))
  },
})
const importApplying = computed(() => importApplyMutation.isPending.value)

async function applyImport() {
  if (
    !importState.validated ||
    importNoChanges.value ||
    importApplying.value ||
    importValidating.value
  )
    return
  try {
    await ElMessageBox.confirm(
      'Apply the validated import as a new configuration revision?',
      'Apply Import',
      { type: 'warning', confirmButtonText: 'Apply Import' },
    )
  } catch {
    return
  }
  importApplyMutation.mutate()
}

// -- Restore ------------------------------------------------------------------
const restoreMutation = useMutation({
  mutationFn: async (row: HistoryEntry) => {
    const result = await restoreConfig(row.revision)
    if (!result.success) throw new Error(result.errors.join('; ') || 'Restore failed')
    return result
  },
  onSuccess: async (result) => {
    await invalidateConfigState()
    ElMessage.success('Restored as revision ' + (result.revision ?? revision.value))
  },
  onError: (error) => {
    ElMessage.error(error instanceof Error ? error.message : String(error))
  },
})
const restoring = computed(() => restoreMutation.isPending.value)

async function restore(row: HistoryEntry) {
  if (restoring.value || applying.value || importApplying.value) return
  try {
    await ElMessageBox.confirm(
      'Restore revision ' + row.revision + ' as a new revision?',
      'Restore Revision',
      { type: 'warning', confirmButtonText: 'Restore' },
    )
  } catch {
    return
  }
  restoreMutation.mutate(row)
}

// -- Download / Backup ----------------------------------------------------------
function saveBlob(filename: string, blob: Blob) {
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  URL.revokeObjectURL(url)
}
function downloadCurrent() {
  saveBlob(
    file.value,
    new Blob([yamlFiles.value[file.value] || ''], { type: 'text/yaml;charset=utf-8' }),
  )
}
async function createBackup() {
  try {
    const archive = await downloadConfigBackup()
    const stamp = new Date().toISOString().replace(/[-:]/g, '').replace('T', '_').slice(0, 15)
    saveBlob('wind-hub-config-backup_' + stamp + '.zip', archive)
    ElMessage.success('Configuration backup downloaded')
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : String(error))
  }
}
function handleAction(command: string) {
  if (command === 'import') importOpen.value = true
  else if (command === 'download-current') downloadCurrent()
  else if (command === 'backup') void createBackup()
}
</script>

<template>
  <div class="config-page">
    <div class="head">
      <div>
        <h1>Configuration Files</h1>
        <p>YAML 查询、修改、Import + Diff、单文件下载、完整配置备份与历史版本。</p>
      </div>
      <div class="head-actions">
        <el-tag type="info">Revision {{ revision }}</el-tag>
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
          <div>
            <h2>Configuration Workspace</h2>
            <p>Applied → Working Copy；编辑、Changes、Validate 与 Apply 使用统一主工作区。</p>
          </div>
          <el-tag :type="dirtyMap[file] ? 'warning' : 'success'">{{
            dirtyMap[file] ? 'Pending Apply' : 'Applied'
          }}</el-tag>
        </div>

        <el-card shadow="never">
          <div class="config-editor-layout">
            <aside class="config-nav">
              <span class="config-nav-title">Files</span>
              <el-button
                v-for="name in CONFIG_FILES"
                :key="name"
                text
                class="config-nav-button"
                :class="{ active: file === name }"
                :disabled="applying"
                @click="file = name"
              >
                {{ name }}<el-badge v-if="dirtyMap[name]" is-dot type="warning" />
              </el-button>
            </aside>

            <main class="yaml-workspace">
              <div class="yaml-toolbar">
                <div class="yaml-title">
                  <b>{{ file }}</b
                  ><span>{{
                    dirtyMap[file]
                      ? 'Working copy differs from applied revision'
                      : 'Matches applied revision'
                  }}</span>
                </div>
                <el-segmented
                  v-model="editorMode"
                  :options="[
                    { label: 'Edit', value: 'edit' },
                    { label: 'Changes', value: 'review' },
                  ]"
                />
              </div>

              <el-input
                v-if="editorMode === 'edit'"
                v-model="yamlFiles[file]"
                class="yaml-input"
                type="textarea"
                :rows="25"
              />
              <div v-else class="review-editor">
                <div
                  v-for="(line, index) in reviewLines"
                  :key="index"
                  :class="['review-line', line.type]"
                >
                  <span class="review-gutter">{{
                    line.type === 'add' ? '+' : line.type === 'remove' ? '−' : ''
                  }}</span>
                  <code>{{ line.text || ' ' }}</code>
                </div>
              </div>

              <div class="yaml-actions">
                <el-button :loading="validating" :disabled="applying" @click="validate"
                  >Validate</el-button
                >
                <el-button
                  type="primary"
                  :loading="applying"
                  :disabled="!dirtyMap[file] || validating || applying"
                  @click="apply"
                  >Apply</el-button
                >
              </div>
            </main>
          </div>
        </el-card>
      </el-tab-pane>

      <el-tab-pane label="History" name="history">
        <div class="section-heading">
          <div>
            <h2>Configuration History</h2>
            <p>
              History 记录 Apply / Import / Restore 产生的 Revision；Create Backup 不修改 Revision。
            </p>
          </div>
        </div>
        <el-card shadow="never">
          <el-table :data="history">
            <el-table-column prop="revision" label="Revision" width="100" />
            <el-table-column prop="time" label="Created At" width="180" />
            <el-table-column prop="source" label="Source" width="110" />
            <el-table-column prop="comment" label="Comment" min-width="220" />
            <el-table-column prop="status" label="Status" width="110" />
            <el-table-column label="Operation" width="140"
              ><template #default="{ row }"
                ><el-button
                  link
                  type="primary"
                  :loading="restoring"
                  :disabled="restoring || applying || importApplying"
                  @click="restore(row)"
                  >Compare / Restore</el-button
                ></template
              ></el-table-column
            >
          </el-table>
        </el-card>
      </el-tab-pane>
    </el-tabs>

    <el-drawer
      v-model="importOpen"
      title="Import Configuration"
      size="var(--app-drawer-width-sm)"
      @closed="closeImport"
    >
      <el-form label-position="top">
        <el-form-item label="Target File">
          <el-select v-model="importState.target" class="app-full-width">
            <el-option v-for="name in CONFIG_FILES" :key="name" :label="name" :value="name" />
          </el-select>
        </el-form-item>
        <!-- 不设 limit：连续选择不同文件时以最后一次选择为准（importState 只保留单个文件） -->
        <el-upload drag action="#" :auto-upload="false" :on-change="onImportChange">
          <div>Drop YAML here or click to select</div>
          <small>Upload is staged only; current Applied configuration is unchanged.</small>
        </el-upload>
      </el-form>

      <div class="drawer-section">
        <div class="drawer-section-head">
          <h3>Validation</h3>
          <el-button
            type="primary"
            :loading="importValidating"
            :disabled="!importState.name || importApplying"
            @click="validateImport"
            >Validate & Compare</el-button
          >
        </div>
        <el-empty v-if="!importState.name" description="Select a YAML file to continue" />
        <template v-else-if="importState.errors.length">
          <el-alert type="error" :closable="false" title="Import validation failed " />
          <pre class="diff-preview">{{ importState.errors.join('\n') }}</pre>
        </template>
        <el-empty
          v-else-if="!importState.validated"
          description="Validate the uploaded YAML to continue"
        />
        <el-alert
          v-else-if="importNoChanges"
          type="info"
          :closable="false"
          title="No Changes — uploaded YAML matches the current working copy"
        />
        <template v-else>
          <el-alert
            type="success"
            :closable="false"
            title="Syntax, schema and reference validation passed "
          />
          <h3>Changes</h3>
          <div class="review-editor import-review">
            <div
              v-for="(line, index) in importReviewLines"
              :key="index"
              :class="['review-line', line.type]"
            >
              <span class="review-gutter">{{
                line.type === 'add' ? '+' : line.type === 'remove' ? '−' : ''
              }}</span>
              <code>{{ line.text || ' ' }}</code>
            </div>
          </div>
          <h3>Impact</h3>
          <el-descriptions :column="1" border>
            <el-descriptions-item label="Target">{{ importState.target }}</el-descriptions-item>
            <el-descriptions-item label="Impact">{{
              importImpacts.join('; ')
            }}</el-descriptions-item>
          </el-descriptions>
        </template>
      </div>

      <template #footer>
        <div class="drawer-footer">
          <el-button :disabled="importApplying" @click="importOpen = false">Cancel</el-button>
          <el-button
            type="primary"
            :loading="importApplying"
            :disabled="
              !importState.validated || importNoChanges || importValidating || importApplying
            "
            @click="applyImport"
            >Apply Import</el-button
          >
        </div>
      </template>
    </el-drawer>
  </div>
</template>

<style scoped>
.head-actions {
  display: flex;
  align-items: center;
  gap: var(--app-space-2);
}
.config-tabs {
  margin-top: var(--app-space-2);
}
.section-heading {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: var(--app-space-4);
  margin-bottom: var(--app-space-3);
}
.section-heading h2 {
  margin: 0;
  font-size: var(--app-font-section-title);
  font-weight: var(--app-font-weight-semibold);
}
.section-heading p,
.yaml-title span {
  margin: var(--app-space-1) 0 0;
  color: var(--app-text-muted);
  font-size: var(--app-font-caption);
}
.config-editor-layout {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 5fr);
  gap: var(--app-space-4);
}
.config-nav {
  display: flex;
  flex-direction: column;
  gap: var(--app-space-1);
  padding-right: var(--app-space-3);
  border-right: 1px solid var(--app-border-soft);
}
.config-nav-title {
  padding: 0 var(--app-space-2);
  color: var(--app-text-muted);
  font-size: var(--app-font-caption);
  font-weight: var(--app-font-weight-semibold);
  text-transform: uppercase;
}
.config-nav-button {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
  padding: var(--app-space-2);
  border: 1px solid transparent;
  border-radius: var(--app-control-radius);
  background: transparent;
  color: var(--app-text-primary);
  text-align: left;
  cursor: pointer;
}
.config-nav-button:hover {
  background: var(--el-fill-color-light);
}
.config-nav-button.active {
  background: var(--el-color-primary-light-9);
  border-color: var(--el-color-primary-light-7);
  color: var(--el-color-primary);
}
.yaml-workspace {
  min-width: 0;
}
.yaml-toolbar,
.yaml-actions,
.yaml-title {
  display: flex;
  align-items: center;
}
.yaml-toolbar {
  justify-content: space-between;
  gap: var(--app-space-3);
  margin-bottom: var(--app-space-3);
}
.yaml-title {
  align-items: flex-start;
  flex-direction: column;
  gap: var(--app-space-1);
}
.yaml-input :deep(textarea) {
  font-family: monospace;
  line-height: var(--app-line-height-body);
}
.yaml-actions {
  justify-content: flex-end;
  gap: var(--app-space-2);
  margin-top: var(--app-space-3);
}
.review-editor {
  border: 1px solid var(--app-border-soft);
  border-radius: var(--app-control-radius);
  background: var(--el-bg-color);
  padding: var(--app-space-2) 0;
  font-family: monospace;
  line-height: var(--app-line-height-body);
}
.review-line {
  display: grid;
  grid-template-columns: var(--app-space-6) minmax(0, 1fr);
  border-left: 3px solid transparent;
}
.review-line code {
  padding: var(--app-space-1) var(--app-space-2);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.review-gutter {
  text-align: center;
  color: var(--app-text-muted);
}
.review-line.add {
  background: var(--el-color-success-light-9);
  border-left-color: var(--el-color-success);
}
.review-line.remove {
  background: var(--el-color-danger-light-9);
  border-left-color: var(--el-color-danger);
}
.import-review {
  max-height: 320px;
  overflow: auto;
}
.drawer-section {
  margin-top: var(--app-space-6);
}
.drawer-section-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--app-space-3);
}
.drawer-section h3 {
  margin: var(--app-space-4) 0 var(--app-space-2);
  font-size: var(--app-font-panel-title);
  font-weight: var(--app-font-weight-semibold);
}
.diff-preview {
  white-space: pre-wrap;
  padding: var(--app-space-3);
  border: 1px solid var(--app-border-soft);
  border-radius: var(--app-control-radius);
  background: var(--el-fill-color-extra-light);
  font-family: monospace;
}
.drawer-footer {
  display: flex;
  justify-content: flex-end;
  gap: var(--app-space-2);
}
@media (max-width: 1199px) {
  .config-editor-layout {
    grid-template-columns: 1fr;
  }
  .config-nav {
    flex-direction: row;
    overflow-x: auto;
    border-right: 0;
    border-bottom: 1px solid var(--app-border-soft);
    padding: 0 0 var(--app-space-3);
  }
  .config-nav-title {
    display: none;
  }
  .config-nav-button {
    min-width: max-content;
  }
}
@media (max-width: 767px) {
  .section-heading,
  .yaml-toolbar {
    align-items: flex-start;
    flex-direction: column;
  }
  .yaml-actions {
    flex-wrap: wrap;
  }
  .yaml-actions .el-button {
    flex: 1;
    margin-left: 0 !important;
  }
  .head-actions {
    width: 100%;
    justify-content: space-between;
  }
}
</style>
