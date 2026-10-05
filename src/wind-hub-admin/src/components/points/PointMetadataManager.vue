<script setup lang="ts">
// Point 元数据管理 Drawer：Point Table / Point Group 的列表、新建、编辑与删除。
// 草稿与脏跟踪为本组件 UI/编辑状态；影响面确认与运行 Task 停恢复走
// usePointTableImpact；持久化一律经 configStore.mutate。
import { computed, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { usePointTableImpact } from '../../composables/usePointTableImpact'
import { useViewport } from '../../composables/useViewport'
import {
  descendantTableIds,
  isDefaultPointGroup,
  isDefaultPointTable,
  pointsOfTable,
} from '../../domain/points'
import { useConfigStore } from '../../stores/config'
import { PROTOCOLS } from '../../domain/types'
import type { Protocol } from '../../domain/types'

const props = defineProps<{
  /** 页面当前选中的 Point Table：新建/删除后可能需要切换。 */
  selectedTable: string
}>()

const emit = defineEmits<{
  'select-table': [id: string]
}>()

const open = defineModel<boolean>({ required: true })

const configStore = useConfigStore()
const { isMobile } = useViewport()
const { confirmTableImpact, withAffectedTasksStopped } = usePointTableImpact()

type ManageSection = 'table' | 'group'
const manageSection = ref<ManageSection>('table')
const tableEditingId = ref('')
const groupEditingId = ref('')
const tableSnapshot = ref('')
const groupSnapshot = ref('')

const tableDraft = reactive({ id: '', protocol: 'modbus' as Protocol, extends: '' })
const groupDraft = reactive({ id: '', name: '' })

const parentTables = computed(() => {
  const blocked = new Set(
    tableEditingId.value
      ? [tableEditingId.value, ...descendantTableIds(configStore, tableEditingId.value)]
      : [],
  )
  return configStore.pointTables.filter(
    (t) => !t.system && t.protocol === tableDraft.protocol && !blocked.has(t.id),
  )
})

const editingTable = computed(() =>
  configStore.pointTables.find((t) => t.id === tableEditingId.value),
)
const tableDirty = computed(
  () => !!tableEditingId.value && JSON.stringify(tableDraft) !== tableSnapshot.value,
)
const groupDirty = computed(
  () => !!groupEditingId.value && JSON.stringify(groupDraft) !== groupSnapshot.value,
)
const manageDirty = computed(() =>
  manageSection.value === 'table' ? tableDirty.value : groupDirty.value,
)

async function beforeManageClose(done: () => void) {
  if (!manageDirty.value) {
    done()
    return
  }
  try {
    await ElMessageBox.confirm('Discard unsaved metadata changes?', 'Unsaved Changes', {
      type: 'warning',
      confirmButtonText: 'Discard',
    })
    done()
  } catch {}
}

const tableRows = computed(() =>
  configStore.pointTables.map((t) => {
    const modelIds = configStore.deviceModels.filter((m) => m.point_table === t.id).map((m) => m.id)
    const childTableIds = configStore.pointTables.filter((x) => x.extends === t.id).map((x) => x.id)
    const devices = configStore.devices.filter((d) => modelIds.includes(d.model)).length
    return {
      ...t,
      points: pointsOfTable(configStore, t.id).length,
      localPoints: (configStore.points[t.id] || []).length,
      inheritedPoints: t.extends
        ? pointsOfTable(configStore, t.extends).filter(
            (p) =>
              !(configStore.points[t.id] || []).some((x) => x.point_id === p.point_id) &&
              !(t.remove_points || []).includes(p.point_id),
          ).length
        : 0,
      models: modelIds.length,
      modelIds,
      devices,
      childTables: childTableIds.length,
      childTableIds,
    }
  }),
)

type TableRow = (typeof tableRows.value)[number]

function tableDeleteBlocked(row: TableRow) {
  return !!row.system || isDefaultPointTable(row.id) || row.childTables > 0 || row.models > 0
}

function tableDeleteBlockerText(row: TableRow) {
  if (row.system || isDefaultPointTable(row.id))
    return 'System default Point Tables cannot be deleted'
  const reasons: string[] = []
  if (row.childTableIds.length) reasons.push('Child Table: ' + row.childTableIds.join(', '))
  if (row.modelIds.length) reasons.push('Device Model: ' + row.modelIds.join(', '))
  return reasons.length ? 'Referenced by ' + reasons.join(' · ') : ''
}

const editingTableReferences = computed(() => {
  if (!tableEditingId.value) return { childTables: [] as string[], models: [] as string[] }
  return {
    childTables: configStore.pointTables
      .filter((t) => t.extends === tableEditingId.value)
      .map((t) => t.id),
    models: configStore.deviceModels
      .filter((m) => m.point_table === tableEditingId.value)
      .map((m) => m.id),
  }
})

const groupRows = computed(() =>
  configStore.pointGroups.map((g) => ({
    ...g,
    points: Object.values(configStore.points).reduce(
      (sum, list) => sum + list.filter((p) => p.point_groups.includes(g.id)).length,
      0,
    ),
    tasks: configStore.tasks.filter((t) => t.point_group === g.id).length,
  })),
)

// 打开时回到 table 分区并重置草稿（与原 openManage 行为一致）。
function onOpen() {
  manageSection.value = 'table'
  newTable()
}

function selectManage(next: ManageSection) {
  manageSection.value = next
  if (next === 'table') newTable()
  else newGroup()
}

function onManageTabChange(name: string | number) {
  selectManage(String(name) as ManageSection)
}

function onManageRowClick(row: { id: string }) {
  if (manageSection.value === 'table') editTable(row.id)
  else editGroup(row.id)
}

function newTable() {
  tableEditingId.value = ''
  tableDraft.id = ''
  tableDraft.protocol = 'modbus'
  tableDraft.extends = ''
}

function editTable(id: string) {
  const t = configStore.pointTables.find((x) => x.id === id)
  if (!t) return
  tableEditingId.value = t.id
  tableDraft.id = t.id
  tableDraft.protocol = t.protocol === 'generic' ? 'modbus' : t.protocol
  tableDraft.extends = t.extends || ''
  tableSnapshot.value = JSON.stringify(tableDraft)
}

async function saveTable() {
  const id = tableDraft.id.trim()
  if (!id) {
    ElMessage.error('Point Table ID is required')
    return
  }

  const duplicate = configStore.pointTables.some(
    (t) => t.id === id && t.id !== tableEditingId.value,
  )
  if (duplicate) {
    ElMessage.error('Point table "' + id + '" already exists')
    return
  }

  if (tableDraft.extends) {
    const parent = configStore.pointTables.find((t) => t.id === tableDraft.extends)
    if (!parent || parent.protocol !== tableDraft.protocol) {
      ElMessage.error('Parent table must use the same protocol')
      return
    }
    if (descendantTableIds(configStore, id).includes(tableDraft.extends)) {
      ElMessage.error('Point table inheritance cycle is not allowed')
      return
    }
  }

  if (tableEditingId.value) {
    const target = configStore.pointTables.find((t) => t.id === tableEditingId.value)
    if (!target) return
    if (target.system) {
      ElMessage.warning('System default Point Tables cannot be modified')
      return
    }
    if (id !== target.id) {
      ElMessage.warning('Point Table ID is stable after creation')
      return
    }

    const protocolChanged = target.protocol !== tableDraft.protocol
    const parentChanged = target.extends !== tableDraft.extends

    if (protocolChanged) {
      const childCount = configStore.pointTables.filter((t) => t.extends === target.id).length
      const modelCount = configStore.deviceModels.filter((m) => m.point_table === target.id).length
      if (childCount || modelCount || pointsOfTable(configStore, target.id).length) {
        ElMessage.warning(
          'Protocol cannot be changed while the table has points, child tables, or Device Model references',
        )
        return
      }
    }

    if (parentChanged && tableDraft.extends) {
      const parentIds = new Set(
        pointsOfTable(configStore, tableDraft.extends).map((p) => p.point_id),
      )
      const invalidRemoved = (target.remove_points || []).filter((pid) => !parentIds.has(pid))
      if (invalidRemoved.length) {
        ElMessage.error(
          'New parent does not contain removed point(s): ' + invalidRemoved.join(', '),
        )
        return
      }
    }

    if (protocolChanged || parentChanged) {
      const ok = await confirmTableImpact(
        target.id,
        'Point Table Change Impact',
        protocolChanged ? 'Change Point Table protocol?' : 'Change parent Point Table?',
      )
      if (!ok) return
    }

    withAffectedTasksStopped(target.id, () => {
      target.protocol = tableDraft.protocol
      target.extends = tableDraft.extends
    })
    tableSnapshot.value = JSON.stringify(tableDraft)
    ElMessage.success('Point table updated ')
  } else {
    configStore.mutate(() => {
      configStore.pointTables.push({
        id,
        protocol: tableDraft.protocol,
        extends: tableDraft.extends,
        remove_points: [],
      })
      configStore.points[id] = []
    })
    emit('select-table', id)
    ElMessage.success('Point table created ')
    newTable()
  }
}

async function deleteTable(row: {
  id: string
  protocol: Protocol
  points: number
  models: number
  devices: number
  childTables: number
  system?: boolean
}) {
  if (row.system || isDefaultPointTable(row.id)) {
    ElMessage.warning('System default Point Tables cannot be deleted')
    return
  }

  const children = configStore.pointTables.filter((t) => t.extends === row.id)
  const models = configStore.deviceModels.filter((m) => m.point_table === row.id)
  if (children.length || models.length) {
    ElMessage.warning(
      'Cannot delete: referenced by ' +
        children.length +
        ' child table(s) and ' +
        models.length +
        ' Device Model(s)',
    )
    return
  }

  try {
    await ElMessageBox.confirm(
      '<b>Delete Point Table "' +
        row.id +
        '"?</b><br><br>' +
        row.points +
        ' effective point(s) will no longer be available.<br>' +
        'No references will be migrated automatically.',
      'Delete Point Table',
      { type: 'warning', confirmButtonText: 'Delete', dangerouslyUseHTMLString: true },
    )
  } catch {
    return
  }

  configStore.mutate(() => {
    configStore.pointTables.splice(
      configStore.pointTables.findIndex((t) => t.id === row.id),
      1,
    )
    delete configStore.points[row.id]
  })
  if (props.selectedTable === row.id) {
    emit(
      'select-table',
      configStore.pointTables.find((t) => !t.system)?.id || configStore.pointTables[0]?.id || '',
    )
  }
  if (tableEditingId.value === row.id) newTable()
  ElMessage.success('Point table deleted ')
}

function newGroup() {
  groupEditingId.value = ''
  groupDraft.id = ''
  groupDraft.name = ''
}

function editGroup(id: string) {
  const g = configStore.pointGroups.find((x) => x.id === id)
  if (!g) return
  groupEditingId.value = g.id
  groupDraft.id = g.id
  groupDraft.name = g.name
  groupSnapshot.value = JSON.stringify(groupDraft)
}

function saveGroup() {
  const id = groupDraft.id.trim()
  if (!id) {
    ElMessage.error('Point Group ID is required')
    return
  }

  const duplicate = configStore.pointGroups.some(
    (g) => g.id === id && g.id !== groupEditingId.value,
  )
  if (duplicate) {
    ElMessage.error('Point group "' + id + '" already exists')
    return
  }

  if (groupEditingId.value) {
    const target = configStore.pointGroups.find((g) => g.id === groupEditingId.value)
    if (!target) return
    if (target.system) {
      ElMessage.warning('System default Point Group cannot be modified')
      return
    }
    if (id !== target.id) {
      ElMessage.warning('Point Group ID is stable after creation')
      return
    }
    configStore.mutate(() => {
      target.name = groupDraft.name.trim() || target.id
    })
    groupSnapshot.value = JSON.stringify(groupDraft)
  } else {
    configStore.mutate(() => {
      configStore.pointGroups.push({ id, name: groupDraft.name.trim() || id })
    })
    newGroup()
  }

  ElMessage.success(groupEditingId.value ? 'Point group updated ' : 'Point group created ')
}

async function deleteGroup(row: { id: string; points: number; tasks: number; system?: boolean }) {
  if (row.system || isDefaultPointGroup(row.id)) {
    ElMessage.warning('System default Point Group cannot be deleted')
    return
  }

  const referencedPoints = Object.values(configStore.points).reduce(
    (sum, list) => sum + list.filter((p) => p.point_groups.includes(row.id)).length,
    0,
  )
  const tasks = configStore.tasks.filter((t) => t.point_group === row.id)
  if (referencedPoints || tasks.length) {
    ElMessage.warning(
      'Cannot delete: referenced by ' +
        referencedPoints +
        ' local point definition(s) and ' +
        tasks.length +
        ' Task(s)',
    )
    return
  }

  try {
    await ElMessageBox.confirm(
      'Delete Point Group "' + row.id + '"? References will not be migrated automatically.',
      'Delete Point Group',
      { type: 'warning', confirmButtonText: 'Delete' },
    )
  } catch {
    return
  }
  configStore.mutate(() => {
    configStore.pointGroups.splice(
      configStore.pointGroups.findIndex((g) => g.id === row.id),
      1,
    )
  })
  if (groupEditingId.value === row.id) newGroup()
  ElMessage.success('Point group deleted ')
}
</script>

<template>
  <el-drawer
    v-model="open"
    title="Manage Point Metadata"
    direction="rtl"
    :size="isMobile ? '100%' : 'min(900px, 94vw)'"
    class="point-metadata-drawer"
    :before-close="beforeManageClose"
    append-to-body
    destroy-on-close
    @open="onOpen"
  >
    <el-tabs v-model="manageSection" class="metadata-tabs" @tab-change="onManageTabChange">
      <el-tab-pane label="Point Tables" name="table" />
      <el-tab-pane label="Point Groups" name="group" />
    </el-tabs>

    <div class="metadata-layout">
      <div class="metadata-list-pane">
        <el-table
          :data="manageSection === 'table' ? tableRows : groupRows"
          row-key="id"
          :show-header="false"
          highlight-current-row
          :current-row-key="manageSection === 'table' ? tableEditingId : groupEditingId"
          class="metadata-object-table"
          @row-click="onManageRowClick"
        >
          <el-table-column min-width="240">
            <template #default="{ row }">
              <div class="metadata-object-info">
                <template v-if="manageSection === 'table'">
                  <b>{{ row.id }}</b>
                  <small
                    >{{ row.protocol.toUpperCase()
                    }}<template v-if="row.extends"> · extends {{ row.extends }}</template></small
                  >
                  <small
                    >{{ row.points }} effective · {{ row.localPoints }} local ·
                    {{ row.inheritedPoints }} inherited</small
                  >
                  <small
                    >{{ row.models }} models · {{ row.devices }} devices ·
                    {{ row.childTables }} child tables</small
                  >
                </template>
                <template v-else>
                  <b>{{ row.name }}</b>
                  <small>{{ row.id }}</small>
                  <small>{{ row.points }} points · {{ row.tasks }} tasks</small>
                </template>
              </div>
            </template>
          </el-table-column>
          <el-table-column width="82" align="right">
            <template #default="{ row }">
              <el-tag v-if="row.system" type="info" size="small">System</el-tag>
              <template v-else-if="manageSection === 'table'">
                <el-tooltip
                  :disabled="!tableDeleteBlocked(row)"
                  :content="tableDeleteBlockerText(row)"
                  placement="left"
                >
                  <span>
                    <el-button
                      link
                      type="danger"
                      :disabled="tableDeleteBlocked(row)"
                      @click.stop="deleteTable(row)"
                      >Delete</el-button
                    >
                  </span>
                </el-tooltip>
              </template>
              <el-button v-else link type="danger" @click.stop="deleteGroup(row)">Delete</el-button>
            </template>
          </el-table-column>
        </el-table>
      </div>

      <div class="metadata-editor-main">
        <div class="metadata-editor-title">
          <h3>
            {{
              manageSection === 'table'
                ? tableEditingId
                  ? 'Edit Table'
                  : 'New Table'
                : groupEditingId
                  ? 'Edit Group'
                  : 'New Group'
            }}
          </h3>
          <p>{{ manageSection === 'table' ? tableEditingId : groupEditingId }}</p>
        </div>

        <el-form v-if="manageSection === 'table'" label-position="top">
          <el-form-item label="Table ID"
            ><el-input v-model="tableDraft.id" :disabled="!!tableEditingId"
          /></el-form-item>
          <el-form-item label="Protocol">
            <el-select
              v-model="tableDraft.protocol"
              :disabled="!!editingTable?.system"
              @change="tableDraft.extends = ''"
              class="app-full-width"
            >
              <el-option v-for="p in PROTOCOLS" :key="p" :label="p.toUpperCase()" :value="p" />
            </el-select>
            <div v-if="editingTable?.system" class="field-note">
              System default Point Tables are fixed placeholders.
            </div>
          </el-form-item>
          <el-form-item label="Extends">
            <el-select v-model="tableDraft.extends" class="app-full-width">
              <el-option label="No Base Table" value="" />
              <el-option v-for="t in parentTables" :key="t.id" :label="t.id" :value="t.id" />
            </el-select>
            <div class="field-note">
              Only protocol-compatible parents that cannot create an inheritance cycle are shown.
            </div>
          </el-form-item>
          <el-alert
            v-if="
              tableEditingId &&
              (editingTableReferences.childTables.length || editingTableReferences.models.length)
            "
            type="warning"
            :closable="false"
            class="table-reference-alert"
          >
            <template #title>Deletion blocked by references</template>
            <div v-if="editingTableReferences.childTables.length">
              Child Tables: {{ editingTableReferences.childTables.join(', ') }}
            </div>
            <div v-if="editingTableReferences.models.length">
              Device Models: {{ editingTableReferences.models.join(', ') }}
            </div>
          </el-alert>
          <div class="metadata-editor-actions">
            <el-button v-if="!tableEditingId" @click="newTable">Clear</el-button>
            <el-button
              type="primary"
              :disabled="!!tableEditingId && !tableDirty"
              @click="saveTable"
              >{{ tableEditingId ? 'Save' : 'Create' }}</el-button
            >
          </div>
        </el-form>

        <el-form v-else label-position="top">
          <el-form-item label="Group ID"
            ><el-input v-model="groupDraft.id" :disabled="!!groupEditingId"
          /></el-form-item>
          <el-form-item label="Name"><el-input v-model="groupDraft.name" /></el-form-item>
          <div class="metadata-editor-actions">
            <el-button v-if="!groupEditingId" @click="newGroup">Clear</el-button>
            <el-button
              type="primary"
              :disabled="!!groupEditingId && !groupDirty"
              @click="saveGroup"
              >{{ groupEditingId ? 'Save' : 'Create' }}</el-button
            >
          </div>
        </el-form>
      </div>
    </div>
  </el-drawer>
</template>

<style scoped>
.metadata-tabs {
  margin-top: calc(-1 * var(--app-space-2));
}
.metadata-layout {
  display: grid;
  grid-template-columns: var(--app-master-pane-width) minmax(0, 1fr);
  min-height: var(--app-master-detail-min-height);
}
.metadata-list-pane {
  min-width: 0;
  padding-right: var(--app-space-3);
  border-right: 1px solid var(--app-border-soft);
}
.metadata-editor-main {
  min-width: 0;
  padding: var(--app-space-1) var(--app-space-2) var(--app-space-1) var(--app-space-6);
}
.metadata-editor-title {
  margin-bottom: var(--app-space-4);
}
.metadata-editor-title h3 {
  margin: 0;
  font-size: var(--app-font-section-title);
  font-weight: var(--app-font-weight-semibold);
}
.metadata-editor-title p {
  margin: var(--app-space-1) 0 0;
  color: var(--app-text-muted);
  font-size: var(--app-font-caption);
}
.metadata-editor-actions {
  display: flex;
  align-items: center;
  gap: var(--app-space-2);
  justify-content: flex-end;
  margin-top: var(--app-space-2);
}
.field-note {
  color: var(--app-text-muted);
  font-size: var(--app-font-label);
}
@media (max-width: 1199px) {
  .metadata-layout {
    grid-template-columns: 1fr;
  }
  .metadata-list-pane {
    border-right: 0;
    border-bottom: 1px solid var(--app-border-soft);
    padding: 0 0 var(--app-space-3);
  }
  .metadata-editor-main {
    padding: var(--app-space-4) 0 0;
  }
}
</style>
