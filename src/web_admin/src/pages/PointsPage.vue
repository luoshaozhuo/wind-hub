<script setup lang="ts">
// Points 页：Point Table 浏览编排（选择/搜索/分页）与各 feature 宿主。
// 浏览状态在 usePointTableBrowser；点编辑/删除在 usePointEditor；
// 元数据管理与连通性测试分别为独立 feature 组件。
import { ref } from 'vue'
import { ElMessage } from 'element-plus'
import PointEditorDrawer from '../components/points/PointEditorDrawer.vue'
import PointMetadataManager from '../components/points/PointMetadataManager.vue'
import { usePointEditor } from '../composables/usePointEditor'
import { usePointTableBrowser } from '../composables/usePointTableBrowser'
import { useViewport } from '../composables/useViewport'
import { useConfigStore } from '../stores/config'
import type { PointDef } from '../domain/types'

const configStore = useConfigStore()
const { isMobile, isTablet } = useViewport()

const pointTable = ref('beckhoff_wtg_v1')
const browser = usePointTableBrowser(pointTable)
const {
  protocol,
  tableDef,
  rows,
  pointPage,
  pointPageSize,
  pointSearch,
  filteredRows,
  pagedRows,
  addrLabel,
  addressOf,
  originOf,
  unitSymbolOf,
  currentTableIsSystem,
} = browser

const editor = usePointEditor(pointTable, protocol)
const { delPoint, resetOverride } = editor

const manageOpen = ref(false)
const pointEdit = ref(false)

function openAdd() {
  if (currentTableIsSystem.value) {
    ElMessage.warning('Default Point Tables are placeholders and cannot contain points')
    return
  }
  editor.startAdd()
  pointEdit.value = true
}

function openEdit(p: PointDef) {
  editor.startEdit(p)
  pointEdit.value = true
}
</script>

<template>
  <div class="points-page">
    <div class="head">
      <div>
        <h1>Points</h1>
        <p>Point Table、Point Group 与具体点定义</p>
      </div>
    </div>

    <el-card shadow="never">
      <div class="point-table-toolbar">
        <div class="table-select-block">
          <div class="table-label">Point Table</div>
          <div class="table-line">
            <el-select v-model="pointTable" class="point-table-select">
              <el-option
                v-for="t in configStore.pointTables"
                :key="t.id"
                :label="t.id"
                :value="t.id"
              />
            </el-select>
            <span class="table-meta">{{ protocol.toUpperCase() }}</span>
            <span v-if="tableDef?.extends" class="table-meta">extends {{ tableDef.extends }}</span>
            <span class="muted"
              >{{ pointSearch ? filteredRows.length + ' / ' : '' }}{{ rows.length }} points</span
            >
          </div>
        </div>

        <div class="table-actions">
          <el-input
            v-model="pointSearch"
            class="point-search"
            clearable
            placeholder="Search point / variable / address / group"
            aria-label="Search points"
          />
          <el-button type="primary" :disabled="currentTableIsSystem" @click="openAdd"
            >+ Add Point</el-button
          >
          <el-dropdown trigger="click">
            <el-button>Actions</el-button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item @click="manageOpen = true">Manage Metadata</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
      </div>

      <el-table :data="pagedRows" height="590">
        <el-table-column label="Point" min-width="140"
          ><template #default="s"
            ><el-button link @click="openEdit(s.row)"
              ><b>{{ s.row.point_id }}</b></el-button
            ></template
          ></el-table-column
        >
        <el-table-column prop="variable_name" label="Variable" />
        <el-table-column :label="addrLabel"
          ><template #default="s">{{ addressOf(s.row) }}</template></el-table-column
        >
        <el-table-column v-if="!isMobile" label="Source" width="105"
          ><template #default="s"
            ><el-tag
              size="small"
              :type="
                originOf(s.row) === 'inherited'
                  ? 'info'
                  : originOf(s.row) === 'override'
                    ? 'warning'
                    : undefined
              "
              >{{ originOf(s.row) }}</el-tag
            ></template
          ></el-table-column
        >
        <el-table-column v-if="!isMobile" prop="data_type" label="Data Type" />
        <el-table-column v-if="!isMobile" label="Groups"
          ><template #default="s"
            ><el-tag v-for="g in s.row.point_groups" :key="g" class="group-tag">{{
              g
            }}</el-tag></template
          ></el-table-column
        >
        <el-table-column v-if="!isTablet" prop="scale" label="Scale" />
        <el-table-column v-if="!isTablet" prop="offset" label="Offset" />
        <el-table-column v-if="!isMobile" label="Unit"
          ><template #default="s">{{
            unitSymbolOf(s.row.unit) || s.row.unit
          }}</template></el-table-column
        >
        <el-table-column label="Operation" :width="isMobile ? 108 : 170">
          <template #default="s">
            <el-button
              v-if="originOf(s.row) === 'override'"
              size="small"
              @click="resetOverride(s.row)"
              >Reset to Parent</el-button
            >
            <el-button size="small" type="danger" plain @click="delPoint(s.row)">Delete</el-button>
          </template>
        </el-table-column>
      </el-table>
      <div class="pagination">
        <el-pagination
          v-model:current-page="pointPage"
          v-model:page-size="pointPageSize"
          :page-sizes="[20, 50, 100]"
          :total="filteredRows.length"
          :layout="isMobile ? 'prev, pager, next' : 'total, sizes, prev, pager, next'"
        />
      </div>
    </el-card>

    <PointMetadataManager
      v-model="manageOpen"
      :selected-table="pointTable"
      @select-table="pointTable = $event"
    />

    <PointEditorDrawer v-model="pointEdit" :editor="editor" :protocol="protocol" />
  </div>
</template>

<style scoped>
.point-table-toolbar {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: var(--app-toolbar-gap);
  margin-bottom: var(--app-space-3);
}
.table-label {
  margin-bottom: var(--app-space-2);
  color: var(--app-text-secondary);
  font-size: var(--app-font-body);
  font-weight: var(--app-font-weight-semibold);
}
.table-line,
.table-actions {
  display: flex;
  align-items: center;
  gap: var(--app-space-2);
}
.table-actions {
  flex-wrap: wrap;
  justify-content: flex-end;
}
.table-meta {
  color: var(--app-text-regular);
  font-size: var(--app-font-label);
}
.muted {
  color: var(--app-text-muted);
  font-size: var(--app-font-label);
}
.group-tag {
  margin-right: var(--app-space-1);
  margin-bottom: var(--app-space-1);
}
.point-table-select {
  width: var(--app-field-width-lg);
}
.point-search {
  width: var(--app-field-width-lg);
}
@media (max-width: 1199px) {
  .point-table-toolbar {
    align-items: flex-start;
    flex-direction: column;
  }
  .table-actions {
    justify-content: flex-start;
  }
}
@media (max-width: 767px) {
  .point-search {
    width: 100%;
  }
}
</style>
