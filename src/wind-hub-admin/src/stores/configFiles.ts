// Configuration Files 工作区（Pinia）：YAML working copy 草稿 + applied 基线 +
// dirty 推导。服务器内容只经 syncFromServer 流入；Apply 成功经 markApplied 推进基线。
import { defineStore } from 'pinia'

interface ConfigFilesState {
  files: string[]
  working: Record<string, string>
  applied: Record<string, string>
}

export const useConfigFilesStore = defineStore('configFiles', {
  state: (): ConfigFilesState => ({
    files: [],
    working: {},
    applied: {},
  }),

  getters: {
    dirtyMap(state): Record<string, boolean> {
      return Object.fromEntries(
        state.files.map((name) => [
          name,
          (state.working[name] || '') !== (state.applied[name] || ''),
        ]),
      )
    },
  },

  actions: {
    /** 服务器文件清单 + 内容到达：重置 working/applied（与旧 loadYamlFiles+syncApplied 一致）。 */
    syncFromServer(files: string[], contents: Record<string, string>) {
      this.files = files
      for (const name of files) {
        this.working[name] = contents[name] || ''
        this.applied[name] = contents[name] || ''
      }
    },

    /** 单文件 Apply 成功：working 成为新的 applied 基线。 */
    markApplied(name: string) {
      this.applied[name] = this.working[name] || ''
    },
  },
})
