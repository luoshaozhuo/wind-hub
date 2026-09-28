<script setup lang="ts">
import { reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { store } from '../mock/data'
import { CONFIG_FILES, updateMockSiteYaml, yamlDiffs, yamlFiles } from '../mock/yaml'

const configTab = ref('YAML Editor')

// ---- Site ----
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

function updateSite() {
  const siteId = siteDraft.siteId.trim()
  const siteName = siteDraft.siteName.trim()

  if (!siteId) {
    ElMessage.error('Site ID is required')
    return
  }
  if (!siteName) {
    ElMessage.error('Site Name is required')
    return
  }

  store.systemInfo.siteId = siteId
  store.systemInfo.siteName = siteName
  updateMockSiteYaml(siteId, siteName)
  dirtyMap['system.yaml'] = true
  siteEditing.value = false
  ElMessage.success('Site information updated — pending apply (mock)')
}

// ---- A. YAML Editor ----
const file = ref('devices.yaml')
const dirtyMap = reactive<Record<string, boolean>>({ 'devices.yaml': true })

function markDirty() {
  dirtyMap[file.value] = true
}

function validate() {
  if (!yamlFiles[file.value].trim()) {
    ElMessage.error(`${file.value} is empty`)
    return
  }
  ElMessage.success(`${file.value} validation passed (mock)`)
}

function save() {
  dirtyMap[file.value] = false
  ElMessage.success(`${file.value} saved (mock)`)
}

function saveApply() {
  dirtyMap[file.value] = false
  ElMessage.success(`${file.value} saved & reload applied (mock)`)
}

// ---- B. Upload Single File ----
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

function onUploadChange(f: { name?: string }) {
  up.name = f?.name || ''
  up.state = 'idle'
}

function upValidate() {
  if (!up.name) {
    ElMessage.warning('Select a YAML file first')
    return
  }
  up.state = 'compared'
  ElMessage.success(`${up.name} validation passed — diff ready (mock)`)
}

function upCancel() {
  up.state = 'idle'
  up.name = ''
}

function upSave() {
  dirtyMap[up.file] = true
  up.state = 'idle'
  up.name = ''
  ElMessage.success(`${up.file} updated from upload — pending apply (mock)`)
}

function upApply() {
  dirtyMap[up.file] = false
  up.state = 'idle'
  up.name = ''
  ElMessage.success(`${up.file} saved & reload applied (mock)`)
}

// ---- C. Import Complete Configuration ----
const pkg = reactive({
  name: '',
  state: 'idle' as 'idle' | 'compared',
})

const pkgDiff = [
  { o: 'Devices', a: '+6', r: '-2', u: '~11' },
  { o: 'Device Models', a: '+1', r: '', u: '~2' },
  { o: 'Tasks', a: '+2', r: '-1', u: '~3' },
  { o: 'Point Tables', a: '+1', r: '', u: '~4' },
  { o: 'Units', a: '', r: '', u: '~1' },
]

function onPkgChange(f: { name?: string }) {
  pkg.name = f?.name || ''
  pkg.state = 'idle'
}

function pkgValidate() {
  if (!pkg.name) {
    ElMessage.warning('Select a configuration ZIP first')
    return
  }
  pkg.state = 'compared'
  ElMessage.success(`${pkg.name} validation passed — overall diff ready (mock)`)
}

function pkgCancel() {
  pkg.state = 'idle'
  pkg.name = ''
}

function pkgApply() {
  for (const f of CONFIG_FILES) dirtyMap[f] = false
  pkg.state = 'idle'
  pkg.name = ''
  ElMessage.success('Configuration package applied & reloaded (mock)')
}
</script>

<template>
  <div>
    <div class="head">
      <div>
        <h1>Config</h1>
        <p>直接编辑 YAML、上传配置、Diff 与下发生效</p>
      </div>
    </div>

    <el-card shadow="never" class="site-card">
      <div class="site-row">
        <div class="site-heading">
          <h3>Site</h3>
          <p>当前 Wind Hub 实例对应的现场身份</p>
        </div>

        <template v-if="!siteEditing">
          <div class="site-info">
            <div>
              <span>Site ID</span>
              <b>{{ store.systemInfo.siteId }}</b>
            </div>
            <div>
              <span>Site Name</span>
              <b>{{ store.systemInfo.siteName }}</b>
            </div>
          </div>
          <el-button @click="editSite">Edit</el-button>
        </template>

        <template v-else>
          <div class="site-edit">
            <el-form label-position="top">
              <div class="site-edit-grid">
                <el-form-item label="Site ID">
                  <el-input v-model="siteDraft.siteId" />
                </el-form-item>
                <el-form-item label="Site Name">
                  <el-input v-model="siteDraft.siteName" />
                </el-form-item>
              </div>
            </el-form>
          </div>
          <el-button type="primary" @click="updateSite">Update</el-button>
        </template>
      </div>
    </el-card>

    <el-card shadow="never">
      <el-tabs v-model="configTab">
        <el-tab-pane label="YAML Editor" name="YAML Editor">
          <div class="config">
            <div class="files">
              <button
                v-for="f in CONFIG_FILES"
                :key="f"
                :class="{ on: file === f }"
                @click="file = f"
              >
                {{ f }}
              </button>
            </div>

            <div>
              <div class="toolbar">
                <b>{{ file }}</b>
                <el-tag :type="dirtyMap[file] ? 'warning' : 'success'">
                  {{ dirtyMap[file] ? 'Pending Apply' : 'Applied' }}
                </el-tag>
              </div>

              <el-input
                v-model="yamlFiles[file]"
                type="textarea"
                :rows="21"
                @input="markDirty"
              />

              <div class="right">
                <el-button @click="validate">Validate</el-button>
                <el-button @click="save">Save</el-button>
                <el-button type="primary" @click="saveApply">Save & Apply</el-button>
              </div>
            </div>

            <div>
              <h3>Diff</h3>
              <pre>{{ yamlDiffs[file] }}</pre>
            </div>
          </div>
        </el-tab-pane>

        <el-tab-pane label="Upload" name="Upload">
          <div class="upload">
            <section>
              <h3>Upload Configuration File</h3>

              <el-select v-model="up.file" style="width: 100%">
                <el-option v-for="f in CONFIG_FILES" :key="f" :label="f" :value="f" />
              </el-select>

              <el-upload
                drag
                action="#"
                :auto-upload="false"
                :limit="1"
                :on-change="onUploadChange"
              >
                <div>Drop YAML here or click to select</div>
                <small>上传不会立即覆盖正式配置</small>
              </el-upload>

              <p v-if="up.name">已选择：{{ up.name }}</p>
              <el-button type="primary" @click="upValidate">Validate & Compare</el-button>
            </section>

            <section v-if="up.state === 'compared'">
              <h3>Structured Diff</h3>
              <el-table :data="upDiff">
                <el-table-column prop="o" label="Object" />
                <el-table-column prop="a" label="Added" />
                <el-table-column prop="r" label="Removed" />
                <el-table-column prop="u" label="Updated" />
              </el-table>

              <pre>{{ yamlDiffs[up.file] }}</pre>

              <div class="right">
                <el-button @click="upCancel">Cancel</el-button>
                <el-button @click="upSave">Save</el-button>
                <el-button type="primary" @click="upApply">Save & Apply</el-button>
              </div>
            </section>
          </div>
        </el-tab-pane>

        <el-tab-pane label="Import Package" name="Import Package">
          <h3>Import Complete Configuration</h3>
          <p>上传完整配置包，临时校验、整体 Diff 后再替换当前配置。</p>

          <el-upload
            drag
            action="#"
            :auto-upload="false"
            :limit="1"
            :on-change="onPkgChange"
            style="max-width: 520px"
          >
            <div>Drop configuration ZIP here</div>
          </el-upload>

          <p v-if="pkg.name">已选择：{{ pkg.name }}</p>
          <el-button type="primary" @click="pkgValidate">Validate Package & Compare</el-button>

          <template v-if="pkg.state === 'compared'">
            <h3 style="margin-top: 18px">Overall Diff</h3>
            <el-table :data="pkgDiff" style="max-width: 720px">
              <el-table-column prop="o" label="Object" />
              <el-table-column prop="a" label="Added" />
              <el-table-column prop="r" label="Removed" />
              <el-table-column prop="u" label="Updated" />
            </el-table>

            <div class="right" style="max-width: 720px">
              <el-button @click="pkgCancel">Cancel</el-button>
              <el-button type="primary" @click="pkgApply">Apply Package</el-button>
            </div>
          </template>
        </el-tab-pane>
      </el-tabs>
    </el-card>
  </div>
</template>

<style scoped>
.site-card {
  margin-bottom: 16px;
}
.site-row {
  min-height: 72px;
  display: flex;
  align-items: center;
  gap: 24px;
}
.site-heading {
  width: 210px;
  flex: 0 0 auto;
}
.site-heading h3 {
  margin: 0;
  font-size: 15px;
}
.site-heading p {
  margin: 4px 0 0;
  color: #8a94a3;
  font-size: 12px;
}
.site-info {
  flex: 1;
  display: flex;
  gap: 48px;
}
.site-info > div {
  min-width: 180px;
}
.site-info span {
  display: block;
  margin-bottom: 5px;
  color: #8a94a3;
  font-size: 11px;
}
.site-info b {
  color: #2b3646;
  font-size: 13px;
}
.site-edit {
  flex: 1;
}
.site-edit-grid {
  display: grid;
  grid-template-columns: minmax(180px, 1fr) minmax(240px, 1.4fr);
  gap: 14px;
}
.site-edit :deep(.el-form-item) {
  margin-bottom: 0;
}
@media (max-width: 900px) {
  .site-row {
    align-items: stretch;
    flex-direction: column;
  }
  .site-heading {
    width: auto;
  }
  .site-edit-grid {
    grid-template-columns: 1fr;
  }
}
</style>
