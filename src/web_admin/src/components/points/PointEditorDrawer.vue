<script setup lang="ts">
// Point 编辑 Drawer：点定义表单 + 连通性测试的展示组合。
// 草稿/保存编排在 usePointEditor（由页面持有并传入），本组件只做
// 脏守卫、布局与保存结果处理。
import { computed, ref, watch } from 'vue'
import { ElMessageBox } from 'element-plus'
import PointReadTest from './PointReadTest.vue'
import { useViewport } from '../../composables/useViewport'
import { useConfigStore } from '../../stores/config'
import { DATA_TYPES, MODBUS_REGISTER_TYPES } from '../../domain/types'
import type { PointEditor } from '../../composables/usePointEditor'
import type { Protocol } from '../../domain/types'

const props = defineProps<{
  editor: PointEditor
  protocol: Protocol
}>()

const open = defineModel<boolean>({ required: true })

const configStore = useConfigStore()
const { isMobile, isTablet } = useViewport()

const { editing, draft, dirty, draftAddress, testRequest, savePoint } = props.editor
const editorProtocol = computed(() => props.protocol)

const testAddress = computed(() => draftAddress())
// 每次打开抽屉重置一次连通性测试。
const testResetKey = ref(0)
watch(open, (value) => {
  if (value) testResetKey.value += 1
})

async function confirmDiscard(): Promise<boolean> {
  try {
    await ElMessageBox.confirm('Discard unsaved Point changes?', 'Unsaved Changes', {
      type: 'warning',
      confirmButtonText: 'Discard',
    })
    return true
  } catch {
    return false
  }
}

async function beforeClose(done: () => void) {
  if (!dirty.value || (await confirmDiscard())) done()
}

async function closeEditor() {
  if (!dirty.value || (await confirmDiscard())) open.value = false
}

async function onSave() {
  if (await savePoint()) open.value = false
}
</script>

<template>
  <el-drawer
    v-model="open"
    :title="editing ? 'Edit Point' : 'Add Point'"
    direction="rtl"
    :size="isMobile ? '100%' : isTablet ? '92%' : 'min(1120px, 86vw)'"
    append-to-body
    destroy-on-close
    class="point-editor-drawer"
    :before-close="beforeClose"
  >
    <el-row :gutter="24">
      <el-col :xs="24" :sm="24" :md="24" :lg="14">
        <section class="point-editor-section">
          <div class="point-editor-heading">
            <h3>Point Definition</h3>
            <p>Edit the point definition. Unsaved address changes are used by the test.</p>
          </div>
          <el-form label-position="top">
            <div class="point-definition-grid">
              <el-form-item label="Point ID"
                ><el-input v-model="draft.point_id" :disabled="!!editing"
              /></el-form-item>
              <el-form-item label="Variable Name"
                ><el-input v-model="draft.variable_name"
              /></el-form-item>

              <template v-if="editorProtocol === 'ads'">
                <el-form-item label="Symbol" class="span-2"
                  ><el-input v-model="draft.symbol" placeholder="MAIN.rotorSpeed"
                /></el-form-item>
                <el-form-item label="Index Group"
                  ><el-input v-model="draft.index_group" placeholder="0x4020"
                /></el-form-item>
                <el-form-item label="Index Offset"
                  ><el-input v-model="draft.index_offset" placeholder="0x1234"
                /></el-form-item>
              </template>
              <template v-else-if="editorProtocol === 'modbus'">
                <el-form-item label="Register Type"
                  ><el-select v-model="draft.register_type" class="app-full-width"
                    ><el-option
                      v-for="r in MODBUS_REGISTER_TYPES"
                      :key="r"
                      :label="r"
                      :value="r" /></el-select
                ></el-form-item>
                <el-form-item label="Address (0-based)"
                  ><el-input-number
                    v-model="draft.address"
                    :min="0"
                    :controls="false"
                    class="app-full-width"
                /></el-form-item>
              </template>
              <template v-else>
                <el-form-item label="IOA"
                  ><el-input-number
                    v-model="draft.ioa"
                    :min="0"
                    :max="16777215"
                    :controls="false"
                    class="app-full-width"
                /></el-form-item>
                <el-form-item label="ASDU Type"><el-input v-model="draft.ioa_type" /></el-form-item>
              </template>

              <el-form-item label="Data Type"
                ><el-select v-model="draft.data_type" class="app-full-width"
                  ><el-option v-for="t in DATA_TYPES" :key="t" :label="t" :value="t" /></el-select
              ></el-form-item>
              <el-form-item label="Unit"
                ><el-select v-model="draft.unit" class="app-full-width"
                  ><el-option
                    v-for="(u, id) in configStore.units"
                    :key="id"
                    :label="id + (u.symbol ? ' (' + u.symbol + ')' : '')"
                    :value="id" /></el-select
              ></el-form-item>
              <el-form-item label="Scale"
                ><el-input-number v-model="draft.scale" class="app-full-width"
              /></el-form-item>
              <el-form-item label="Offset"
                ><el-input-number v-model="draft.offset" class="app-full-width"
              /></el-form-item>
              <el-form-item label="Point Groups" class="span-2"
                ><el-select v-model="draft.point_groups" multiple class="app-full-width"
                  ><el-option
                    v-for="g in configStore.pointGroups"
                    :key="g.id"
                    :label="g.name + ' · ' + g.id"
                    :value="g.id"
                    :disabled="!!g.system && !draft.point_groups.includes(g.id)" /></el-select
              ></el-form-item>
              <el-form-item label="Description" class="span-2"
                ><el-input v-model="draft.description"
              /></el-form-item>
            </div>
          </el-form>
        </section>
      </el-col>

      <el-col :xs="24" :sm="24" :md="24" :lg="10">
        <PointReadTest
          :protocol="editorProtocol"
          :address="testAddress"
          :request-text="testRequest"
          :reset-key="testResetKey"
        />
      </el-col>
    </el-row>
    <template #footer
      ><el-button @click="closeEditor">Cancel</el-button
      ><el-button type="primary" :disabled="!!editing && !dirty" @click="onSave"
        >Save</el-button
      ></template
    >
  </el-drawer>
</template>

<style scoped>
.point-editor-section {
  min-width: 0;
}
.point-definition-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 0 var(--app-space-4);
}
.point-definition-grid .span-2 {
  grid-column: 1/-1;
}
.point-definition-grid :deep(.el-form-item) {
  margin-bottom: var(--app-space-3);
}
.point-editor-heading {
  margin-bottom: var(--app-space-4);
}
.point-editor-heading h3 {
  margin: 0;
  color: var(--app-text-primary);
  font-size: var(--app-font-section-title);
  font-weight: var(--app-font-weight-semibold);
}
.point-editor-heading p {
  margin: var(--app-space-1) 0 0;
  color: var(--app-text-muted);
  font-size: var(--app-font-caption);
  line-height: var(--app-line-height-compact);
}
@media (max-width: 767px) {
  .point-definition-grid {
    grid-template-columns: 1fr;
  }
  .point-definition-grid .span-2 {
    grid-column: auto;
  }
}
</style>
